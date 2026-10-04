from __future__ import annotations

import asyncio
import threading
from typing import override

import pytest

from src.clock import ManualClock
from src.motor.atv320 import ATV320Drive
from src.motor.drive import BadResponse, CommTimeout, ControlWord, EmergencyStopOutcome, RegisterMap
from src.result import Err, Ok, err_of
from src.units import MotorRpm, RegisterAddress, Seconds
from tests.test_atv320 import (
    Behave,
    FakeBus,
    build_drive,
    connection_exception,
    parameter_exception,
)
from tests.test_atv320 import bus as _bus
from tests.test_atv320 import clock as _clock
from tests.test_atv320 import drive as _drive
from tests.test_atv320 import make_bus as _make_bus

bus = _bus
clock = _clock
drive = _drive
make_bus = _make_bus


class HeldRead(threading.Event):
    """Mutable event that holds the fake read until the racing stop has completed."""

    def __init__(self) -> None:
        super().__init__()
        self.release: threading.Event = threading.Event()

    @override
    def set(self) -> None:
        super().set()
        assert self.release.wait(5.0)


async def test_failed_eta_acquisitions_accumulate_despite_local_connect_success(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    # Given
    bus.sticky_reads = parameter_exception()
    bus.latency = Seconds(0.1)
    # When
    errors = [err_of(await drive.open()) for _ in range(3)]
    # Then
    assert isinstance(errors[0], BadResponse)
    assert isinstance(errors[1], BadResponse)
    assert isinstance(errors[2], CommTimeout)
    assert errors[2].after == pytest.approx(0.4)
    assert drive.link_lost
    assert drive.acquisition_evidence.possible_frames == 3
    assert not drive.acquisition_evidence.address_proven
    assert bus.writes() == []


@pytest.mark.parametrize("raises", [False, True])
async def test_local_connect_failure_counts_once_without_possible_frames(
    drive: ATV320Drive, bus: FakeBus, raises: bool
) -> None:
    # Given
    bus.port_opens = False
    bus.raise_on_connect = connection_exception() if raises else None
    bus.latency = Seconds(0.1)
    # When
    errors = [err_of(await drive.open()) for _ in range(3)]
    # Then
    assert isinstance(errors[2], CommTimeout)
    assert errors[2].after == pytest.approx(0.2)
    assert drive.link_lost
    assert drive.acquisition_evidence.possible_frames == 0
    assert not drive.acquisition_evidence.address_proven
    assert bus.log == []


@pytest.mark.parametrize("previous_proof", [False, True])
async def test_failed_reopen_retains_prior_latch_and_address_proof(
    drive: ATV320Drive, bus: FakeBus, previous_proof: bool
) -> None:
    # Given
    if previous_proof:
        assert isinstance(await drive.open(), Ok)
    drive.emergency_disable_blocking(drive.emergency_budget)
    before = drive.acquisition_evidence
    bus.sticky_reads = parameter_exception()
    # When
    outcome = await drive.open()
    # Then
    assert isinstance(err_of(outcome), BadResponse)
    assert drive.link_lost
    assert drive.acquisition_evidence.address_proven is previous_proof
    assert drive.acquisition_evidence.possible_frames == before.possible_frames + 1


async def test_valid_eta_reacquires_a_latched_link_without_writing(
    clock: ManualClock, bus: FakeBus
) -> None:
    # Given
    drive = build_drive(clock, bus, failure_threshold=1)
    bus.script_reads.append(parameter_exception())
    assert isinstance(await drive.open(), Err)
    was_lost = drive.link_lost
    assert was_lost
    # When
    outcome = await drive.open()
    # Then
    assert isinstance(outcome, Ok)
    assert not drive.link_lost
    assert drive.acquisition_evidence.address_proven
    assert drive.acquisition_evidence.possible_frames == 2
    assert bus.reads() == [RegisterMap().eta] * 2
    assert bus.writes() == []


@pytest.mark.parametrize("raw_eta", [-1, 65536])
async def test_out_of_range_eta_cannot_establish_address_proof(
    drive: ATV320Drive, bus: FakeBus, raw_eta: int
) -> None:
    # Given
    bus.registers[RegisterMap().eta] = raw_eta
    # When
    outcome = await drive.open()
    # Then
    assert isinstance(outcome, Err)
    assert not drive.acquisition_evidence.address_proven
    assert drive.acquisition_evidence.possible_frames == 1


async def test_successful_non_acquisition_exchanges_do_not_prove_addressing(
    drive: ATV320Drive,
) -> None:
    # Given
    before = drive.acquisition_evidence
    # When
    assert isinstance(await drive.write_speed(MotorRpm(0)), Ok)
    assert isinstance(await drive.read_register(RegisterMap().eta), Ok)
    # Then
    assert not drive.acquisition_evidence.address_proven
    assert drive.acquisition_evidence.possible_frames == 3
    assert before.possible_frames == 0
    assert not before.address_proven


async def test_partial_status_then_transport_refusal_retains_possible_traffic(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    # Given
    assert isinstance(await drive.open(), Ok)
    bus.script_reads.extend([Behave.NORMALLY, connection_exception()])
    # When
    outcome = await drive.read_status()
    # Then
    assert isinstance(outcome, Err)
    assert drive.acquisition_evidence.address_proven
    assert drive.acquisition_evidence.possible_frames == 3


@pytest.mark.parametrize("emergency", [False, True])
async def test_failed_write_dispatch_is_counted_without_proving_addressing(
    drive: ATV320Drive, bus: FakeBus, emergency: bool
) -> None:
    # Given
    bus.sticky_writes = connection_exception()
    # When
    if emergency:
        assert (
            drive.emergency_disable_blocking(drive.emergency_budget)
            is EmergencyStopOutcome.NOTHING_SENT
        )
    else:
        assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Err)
    # Then
    assert drive.acquisition_evidence.possible_frames == 1
    assert not drive.acquisition_evidence.address_proven


async def test_close_and_reopen_preserve_proof_and_monotone_frame_count(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    # Given
    assert isinstance(await drive.open(), Ok)
    # When
    assert isinstance(await drive.close(), Ok)
    closed = drive.acquisition_evidence
    assert isinstance(await drive.open(), Ok)
    # Then
    assert closed.address_proven
    assert closed.possible_frames > 1
    assert drive.acquisition_evidence.possible_frames == closed.possible_frames + 1
    assert drive.acquisition_evidence.possible_frames == len(bus.log)
    assert drive.acquisition_evidence.address_proven


@pytest.mark.parametrize("acquiring", [False, True])
async def test_inflight_read_and_emergency_count_both_requests_and_keep_emergency_latch(
    drive: ATV320Drive, bus: FakeBus, acquiring: bool
) -> None:
    # Given
    entered = HeldRead()
    bus.entered_read = entered
    operation = drive.open() if acquiring else drive.read_register(RegisterMap().eta)
    reading = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(entered.wait, 5.0)
        before = drive.acquisition_evidence
        # When
        stopped = await asyncio.to_thread(drive.emergency_disable_blocking, drive.emergency_budget)
    finally:
        entered.release.set()
        await reading
    # Then
    assert stopped is EmergencyStopOutcome.ACKNOWLEDGED
    assert before.possible_frames == 1
    assert drive.acquisition_evidence.possible_frames == 2
    assert drive.acquisition_evidence.address_proven is acquiring
    assert drive.link_lost


@pytest.mark.parametrize("after_connect", [False, True])
async def test_acquisition_rejects_an_invalid_internal_result(
    drive: ATV320Drive, monkeypatch: pytest.MonkeyPatch, after_connect: bool
) -> None:
    # Given
    def unexpected_connect(_self: ATV320Drive) -> None:
        return None

    async def unexpected_read(_self: ATV320Drive, _address: RegisterAddress) -> None:
        return None

    if after_connect:
        monkeypatch.setattr(ATV320Drive, "_read", unexpected_read)
    else:
        monkeypatch.setattr(ATV320Drive, "_blocking_connect", unexpected_connect)
    # When / Then
    with pytest.raises(AssertionError):
        await drive.open()
