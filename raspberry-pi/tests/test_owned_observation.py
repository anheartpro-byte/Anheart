"""Real SDK observation and native cable ownership on a synthetic serial port."""

from __future__ import annotations

import gc
from pathlib import Path

import pytest
from pymodbus.pdu import ModbusPDU

from src.clock import RealClock
from src.motor.atv320 import ModbusMaster, SerialSettings, serial_master
from src.motor.drive_process_lock import DriveLease, DriveOwnershipError, retry_failed_drive_closes
from src.motor.ftdi_link import BufferedFtdiPort
from src.motor.observation import ExchangeKind, ExchangeLog
from src.units import Seconds
from tests.test_drive_process_lock import run_contender
from tests.test_ftdi_link import DRIVE_ADDRESS, ETA_ADDRESS, FakeAltivar, crc16


def serial_with_log(
    observe: bool, patch: pytest.MonkeyPatch
) -> tuple[ModbusMaster, FakeAltivar, ExchangeLog, list[str]]:
    clock = RealClock()
    chip = FakeAltivar({ETA_ADDRESS: 64})
    port = BufferedFtdiPort(chip, timeout=Seconds(0.01), clock=clock)
    opens: list[str] = []

    def open_serial(url: str, **_settings: float | str) -> BufferedFtdiPort:
        opens.append(url)
        return port

    patch.setattr("serial.serial_for_url", open_serial)
    log = ExchangeLog(clock)
    master = serial_master(
        SerialSettings(port="COM-SYNTHETIC", timeout=Seconds(0.01)),
        clock,
        exchange_log=log if observe else None,
    )
    return master, chip, log, opens


@pytest.mark.parametrize("observe", [False, True])
def test_serial_sdk_observation_never_bypasses_cable_ownership(
    observe: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: another owner holds the lease before this serial client can open.
    master, chip, log, opens = serial_with_log(observe, monkeypatch)
    lease = DriveLease.claim()
    try:
        with pytest.raises(DriveOwnershipError):
            master.connect()
        assert opens == []
    finally:
        lease.close()
    # When: the client opens and the actual SDK completes a synthetic RTU read.
    try:
        assert master.connect()
        contender = run_contender(tmp_path)
        assert contender.returncode == 3, contender.stdout + contender.stderr
        assert not (tmp_path / "attempt-opened").exists()
        reply = master.read_holding_registers(ETA_ADDRESS, count=1, slave=DRIVE_ADDRESS)
        assert isinstance(reply, ModbusPDU)
        assert reply.registers == [64]
        # Then: enabled capture matches actual bytes; disabled capture stays empty.
        if observe:
            sends = [entry for entry in log.entries if entry.kind is ExchangeKind.SEND]
            assert [entry.raw_hex for entry in sends] == [wire.hex() for wire in chip.requests]
            chunks: list[bytes] = []
            for entry in log.entries:
                if entry.kind is ExchangeKind.RECEIVE_CHUNK:
                    assert entry.raw_hex is not None
                    chunks.append(bytes.fromhex(entry.raw_hex))
            body = bytes((DRIVE_ADDRESS, 3, 2, 0, 64))
            assert b"".join(chunks) == body + crc16(body)
            assert all(entry.ok for entry in log.entries)
        else:
            assert log.entries == ()
    finally:
        master.close()
    assert chip.close_calls == 1
    assert run_contender(tmp_path).returncode == 0


@pytest.mark.parametrize("observe", [False, True])
def test_serial_failed_close_survives_dropped_observer_until_actual_cleanup(
    observe: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a successfully opened serial handle whose close then fails.
    master, chip, _, _ = serial_with_log(observe, monkeypatch)
    assert master.connect()

    def fail_close() -> None:
        raise OSError("synthetic serial close failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(chip, "close", fail_close)
            try:
                master.close()
            except OSError:
                assert chip.close_calls == 0
            else:
                pytest.fail("the synthetic serial handle must reject close")
            # When: neither caller nor exception keeps the client alive.
            del master
            gc.collect()
            contender = run_contender(tmp_path)
            # Then: the unresolved handle keeps the independent process out.
            assert contender.returncode == 3, contender.stdout + contender.stderr
            assert not (tmp_path / "attempt-opened").exists()
        retry_failed_drive_closes()
        assert chip.close_calls == 1
        assert run_contender(tmp_path).returncode == 0
    finally:
        retry_failed_drive_closes()
