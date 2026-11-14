"""The bench tool's buttons, from its page down to the drive's registers.

The operator presses them at the bench: enable, a speed, STOP, E-STOP, the
fault reset. Each test posts the button's route to the in-process page; the
real bus thread runs the action on the real driver (:class:`ATV320Drive`) over
the in-memory Altivar of the driver's own tests (:class:`FakeBus`), and the test
reads what reached the registers, in order. The writes an action makes are
those between its route and the action's result on the page
(``last_action``): the bus thread runs the queued actions before it polls,
and an unarmed console writes nothing when it polls.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final, cast

import httpx
import pytest

import scripts.bench_console as bench
from scripts.drive_link import BuiltDrive
from src.clock import Clock, ManualClock
from src.geometry import MachineGeometry
from src.local_config import DriveLink, load_local_config
from src.motor.drive import ControlWord, RegisterMap
from src.result import Ok
from src.units import Metres
from tests.test_atv320 import (
    ETA_FAULT,
    ETA_SWITCH_ON_DISABLED,
    ETA_SWITCHED_ON,
    LFT_OVERCURRENT,
    Behave,
    FakeBus,
    connection_exception,
    io_exception,
    seed_bench_limits,
)
from tests.test_atv320 import build_drive as driver_over

WAIT_S: Final[float] = 10.0
"""How long a test waits for the bus thread at most; it is done within a cycle or two."""

LFT_MODBUS_LOSS: Final[int] = 5
"""SLF1, the Modbus loss the bench saw: a fault the console may reset."""

UNLISTED_LFT: Final[int] = 999
"""A fault code no table of the driver's knows."""

TURNING_RPM: Final[int] = 600
"""What RFRD reads on a shaft that turns: a plausible speed, under the nameplate's bound."""


def _link() -> DriveLink:
    loaded = load_local_config(
        {
            "MOTOR_BACKEND": "serial",
            "MOTOR_PORT": "COM-NONE",
            "ECG_SOURCE": "sim",
            "ARM_RADIUS_M": "1.5",
            "UI_PORT": "8090",
        }
    )
    assert isinstance(loaded, Ok), loaded
    link = loaded.value.drive_link
    assert link is not None
    return link


@dataclass(frozen=True, slots=True)
class Bench:
    """The page, the bus thread behind it, and the drive's registers under both."""

    worker: bench.BusWorker
    bus: FakeBus
    page: httpx.AsyncClient

    @property
    def regs(self) -> RegisterMap:
        return self.bus.regs

    def mark(self) -> int:
        """Where the write log stands now: what an action writes comes after it."""
        return len(self.bus.writes())

    def since(self, mark: int) -> list[tuple[int, int | None]]:
        return self.bus.writes()[mark:]

    async def press(self, path: str, body: dict[str, object] | None = None) -> httpx.Response:
        return await self.page.post(path, json=body)

    async def result(self, action: str) -> str:
        """The result of ``action`` as the page shows it, once the bus thread has run it."""
        deadline = time.monotonic() + WAIT_S
        while True:
            shown = self.worker.snap.last_action
            if shown.startswith(action) and not shown.endswith("en cours..."):
                return shown
            assert time.monotonic() < deadline, f"{action} never finished: {shown!r}"
            await asyncio.sleep(0.01)

    async def polled(self) -> None:
        """Wait for one more cycle of the bus thread: an action, then a poll."""
        seen = self.worker.snap.cycles
        deadline = time.monotonic() + WAIT_S
        while self.worker.snap.cycles <= seen:
            assert time.monotonic() < deadline, "the bus thread stopped cycling"
            await asyncio.sleep(0.01)

    async def armed_and_enabled(self) -> None:
        armed = await self.press("/api/arm", {"motor_uncoupled": True})
        assert armed.status_code == HTTPStatus.OK
        enabled = await self.press("/api/enable")
        assert enabled.status_code == HTTPStatus.OK
        result = await self.result("enable")
        assert result.startswith("enable: ok")


def _commands(writes: list[tuple[int, int | None]], cmd: int) -> list[int | None]:
    """The command words written, in order."""
    return [value for address, value in writes if address == cmd]


def _references(writes: list[tuple[int, int | None]], lfrd: int) -> set[int | None]:
    """Every speed reference written: a keepalive in flight may add its own, never another."""
    return {value for address, value in writes if address == lfrd}


def _zeroed_before(writes: list[tuple[int, int | None]], regs: RegisterMap, word: int) -> bool:
    """Whether LFRD = 0 was written before the first ``word`` to CMD."""
    return writes.index((regs.lfrd, 0)) < writes.index((regs.cmd, word))


@asynccontextmanager
async def started(
    monkeypatch: pytest.MonkeyPatch, prepare: Callable[[FakeBus], None] | None = None
) -> AsyncGenerator[Bench]:
    """The bench over a fresh in-memory drive; ``prepare`` shapes that drive before the
    bus thread first opens it."""
    clock = ManualClock()
    regs = RegisterMap()
    bus = FakeBus(clock, regs)
    for address in (regs.lfrd, regs.rfrd, regs.lcr, regs.lft, regs.cmd):
        bus.registers[address] = 0
    bus.registers[regs.eta] = ETA_SWITCH_ON_DISABLED
    seed_bench_limits(bus)
    if prepare is not None:
        prepare(bus)
    built = BuiltDrive(drive=driver_over(clock, bus), master=bus)

    def build(_link: DriveLink, _clock: Clock) -> BuiltDrive:
        return built

    monkeypatch.setattr(bench, "build_drive", build)
    monkeypatch.setattr(bench, "LEAVING", threading.Event())
    worker = bench.BusWorker(_link(), max_rpm=300)
    monkeypatch.setattr(bench, "worker", worker)
    page = httpx.AsyncClient(transport=httpx.ASGITransport(app=bench.app), base_url="http://bench")
    worker.start()
    rig = Bench(worker=worker, bus=bus, page=page)
    try:
        await rig.polled()
        yield rig
    finally:
        await page.aclose()
        worker.shutdown()
        worker.join(WAIT_S)
        assert not worker.is_alive()


@pytest.fixture
async def rig(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Bench]:
    async with started(monkeypatch) as bench_rig:
        yield bench_rig


# =========================================================================
# Enable and speed
# =========================================================================


async def test_enable_zeroes_the_reference_then_runs_the_start_sequence(rig: Bench) -> None:
    mark = rig.mark()
    await rig.armed_and_enabled()
    lfrd, cmd = rig.regs.lfrd, rig.regs.cmd
    assert rig.since(mark)[:4] == [
        (lfrd, 0),
        (cmd, ControlWord.SHUTDOWN),
        (cmd, ControlWord.SWITCH_ON),
        (cmd, ControlWord.ENABLE_OPERATION),
    ]
    await rig.polled()
    snap = rig.worker.snap
    assert (snap.enabled, snap.drive_state) == (True, "OPERATION_ENABLED")
    # Armed and enabled, every poll feeds the drive's ttO with the setpoint: still 0.
    assert set(rig.since(mark)[4:]) == {(lfrd, 0)}


async def test_enable_is_not_tried_when_the_zero_reference_is_not_confirmed(rig: Bench) -> None:
    armed = await rig.press("/api/arm", {"motor_uncoupled": True})
    assert armed.status_code == HTTPStatus.OK
    mark = rig.mark()
    rig.bus.script_writes.append(io_exception())
    enabled = await rig.press("/api/enable")
    assert enabled.status_code == HTTPStatus.OK
    shown = await rig.result("enable")
    assert "activation NON tentee" in shown, shown
    assert [address for address, _value in rig.since(mark)] == [rig.regs.lfrd]


async def test_a_speed_is_bounded_by_the_tool_s_ceiling_and_kept_alive(rig: Bench) -> None:
    await rig.armed_and_enabled()
    asked = await rig.press("/api/speed", {"rpm": 450})
    assert asked.status_code == HTTPStatus.OK
    result = await rig.result("speed")
    assert result == "speed 450 tr/min: ok (relu sur LFRD)"
    assert rig.worker.snap.setpoint_rpm == 300, "clamped to --max-rpm"
    mark = rig.mark()
    await rig.polled()
    await rig.polled()
    kept_alive = rig.since(mark)
    assert kept_alive, "the keepalive"
    assert set(kept_alive) == {(rig.regs.lfrd, 300)}
    await rig.press("/api/stop")
    await rig.result("stop")


async def test_enable_is_refused_on_a_drive_already_enabled_and_turning(rig: Bench) -> None:
    """R2 of ANH-218: the sequence's 6 would drop the output stage of a running drive, then 7
    and 15 re-power a turning shaft. Nothing is written, and the console stays armed."""
    await rig.armed_and_enabled()
    rig.bus.registers[rig.regs.rfrd] = TURNING_RPM
    await rig.polled()
    mark = rig.mark()
    rig.worker.snap.last_action = "-"  # the first enable's result is not this one's
    again = await rig.press("/api/enable")
    assert again.status_code == HTTPStatus.OK
    shown = await rig.result("enable")
    assert shown == (
        "enable: ECHEC: activation refusee (variateur deja active (OPERATION_ENABLED)); "
        "rien n'est ecrit"
    )
    assert _commands(rig.since(mark), rig.regs.cmd) == []
    assert rig.worker.snap.armed is True
    rig.bus.registers[rig.regs.rfrd] = 0


async def test_enable_is_refused_on_a_shaft_still_turning_with_its_stage_off(rig: Bench) -> None:
    armed = await rig.press("/api/arm", {"motor_uncoupled": True})
    assert armed.status_code == HTTPStatus.OK
    rig.bus.registers[rig.regs.rfrd] = 300  # coasting: SWITCH_ON_DISABLED, RFRD 300
    await rig.polled()
    mark = rig.mark()
    await rig.press("/api/enable")
    shown = await rig.result("enable")
    assert "activation refusee (arbre non lu a l'arret: RFRD=300 tr/min)" in shown, shown
    assert rig.since(mark) == [], "not even LFRD"
    rig.bus.registers[rig.regs.rfrd] = 0


async def test_enable_is_refused_without_a_status(rig: Bench) -> None:
    armed = await rig.press("/api/arm", {"motor_uncoupled": True})
    assert armed.status_code == HTTPStatus.OK
    await rig.polled()
    mark = rig.mark()
    rig.bus.sticky_reads = io_exception()
    await rig.press("/api/enable")
    shown = await rig.result("enable")
    rig.bus.sticky_reads = Behave.NORMALLY
    assert "activation refusee (etat illisible: " in shown, shown
    assert rig.since(mark) == []


# =========================================================================
# STOP, E-STOP, the fault reset
# =========================================================================


async def test_stop_ramps_and_writes_6_only_once_the_shaft_reads_still(rig: Bench) -> None:
    await rig.armed_and_enabled()
    mark = rig.mark()
    stopped = await rig.press("/api/stop")
    assert stopped.status_code == HTTPStatus.OK
    result = await rig.result("stop")
    assert result == "stop: ok (RFRD=0, puis CMD=6)"
    written = rig.since(mark)
    assert _commands(written, rig.regs.cmd) == [ControlWord.SWITCH_ON, ControlWord.SHUTDOWN]
    assert _references(written, rig.regs.lfrd) == {0}
    assert _zeroed_before(written, rig.regs, ControlWord.SWITCH_ON)


async def test_a_stop_on_a_shaft_that_still_turns_never_writes_6(
    rig: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    await rig.armed_and_enabled()
    monkeypatch.setattr(bench, "STOP_WAIT_S", 0.6)
    rig.bus.registers[rig.regs.rfrd] = TURNING_RPM
    mark = rig.mark()
    await rig.press("/api/stop")
    shown = await rig.result("stop")
    assert shown.startswith("stop: ECHEC: arret NON confirme"), shown
    assert "CMD=6 NON envoye" in shown
    assert f"RFRD={TURNING_RPM} tr/min" in shown
    written = rig.since(mark)
    assert _commands(written, rig.regs.cmd) == [ControlWord.SWITCH_ON], "never 6"
    assert _references(written, rig.regs.lfrd) == {0}
    assert _zeroed_before(written, rig.regs, ControlWord.SWITCH_ON)
    rig.bus.registers[rig.regs.rfrd] = 0


async def test_estop_disarms_zeroes_the_reference_and_keeps_the_dec_ramp(rig: Bench) -> None:
    await rig.armed_and_enabled()
    rig.bus.registers[rig.regs.rfrd] = TURNING_RPM
    mark = rig.mark()
    answered = await rig.press("/api/estop")
    assert answered.status_code == HTTPStatus.OK
    assert answered.json() == {"queued": "estop", "armed": False}
    assert rig.worker.snap.armed is False, "disarmed at once, before the bus thread runs it"
    result = await rig.result("estop")
    assert result == "estop: ok (LFRD=0 relu, CMD=7: rampe dEC)"
    written = rig.since(mark)
    assert _commands(written, rig.regs.cmd) == [ControlWord.SWITCH_ON], "never 6"
    assert _references(written, rig.regs.lfrd) == {0}
    assert _zeroed_before(written, rig.regs, ControlWord.SWITCH_ON)
    # Disarmed: no keepalive after it.
    await rig.polled()
    after = rig.mark()
    await rig.polled()
    await rig.polled()
    assert rig.since(after) == []
    rig.bus.registers[rig.regs.rfrd] = 0


async def test_the_fault_reset_is_refused_on_a_turning_enabled_drive(rig: Bench) -> None:
    """128 then 6 would drop the output stage: a freewheel. Nothing is written."""
    await rig.armed_and_enabled()
    rig.bus.registers[rig.regs.rfrd] = TURNING_RPM
    mark = rig.mark()
    answered = await rig.press("/api/fault-reset")
    assert answered.status_code == HTTPStatus.OK
    shown = await rig.result("fault_reset")
    assert shown == (
        "fault_reset: ECHEC: reset refuse (aucun defaut a acquitter (OPERATION_ENABLED)); "
        "rien n'est ecrit"
    )
    assert _commands(rig.since(mark), rig.regs.cmd) == []
    assert rig.worker.snap.armed is True, "a refused reset changes nothing"
    rig.bus.registers[rig.regs.rfrd] = 0


async def test_the_fault_reset_is_refused_in_fault_while_the_shaft_still_turns(
    rig: Bench,
) -> None:
    rig.bus.registers[rig.regs.eta] = ETA_FAULT
    rig.bus.registers[rig.regs.lft] = LFT_OVERCURRENT
    rig.bus.registers[rig.regs.rfrd] = 300
    mark = rig.mark()
    await rig.press("/api/fault-reset")
    shown = await rig.result("fault_reset")
    assert "reset refuse (arbre non lu a l'arret: RFRD=300 tr/min)" in shown, shown
    assert _commands(rig.since(mark), rig.regs.cmd) == []
    rig.bus.registers[rig.regs.rfrd] = 0


async def test_the_fault_reset_is_refused_without_a_status(rig: Bench) -> None:
    rig.bus.registers[rig.regs.eta] = ETA_FAULT
    rig.bus.registers[rig.regs.lft] = LFT_OVERCURRENT
    await rig.polled()
    mark = rig.mark()
    rig.bus.sticky_reads = io_exception()
    await rig.press("/api/fault-reset")
    shown = await rig.result("fault_reset")
    rig.bus.sticky_reads = Behave.NORMALLY
    assert "reset refuse (etat illisible: " in shown, shown
    assert _commands(rig.since(mark), rig.regs.cmd) == []


async def test_the_fault_reset_writes_128_then_6_on_a_drive_in_fault(rig: Bench) -> None:
    rig.bus.registers[rig.regs.eta] = ETA_FAULT
    rig.bus.registers[rig.regs.lft] = LFT_MODBUS_LOSS
    await rig.polled()
    assert rig.worker.snap.fault == "MODBUS_COMM_LOSS"
    mark = rig.mark()
    answered = await rig.press("/api/fault-reset")
    assert answered.status_code == HTTPStatus.OK
    result = await rig.result("fault_reset")
    assert result == "fault_reset: ok"
    cmd = rig.regs.cmd
    assert rig.since(mark) == [(cmd, ControlWord.FAULT_RESET), (cmd, ControlWord.SHUTDOWN)]


async def test_the_fault_reset_is_refused_on_a_drive_not_in_fault_even_with_its_stage_off(
    rig: Bench,
) -> None:
    """Not enabled is not enough: the console's rule asks for a fault."""
    rig.bus.registers[rig.regs.eta] = ETA_SWITCHED_ON
    await rig.polled()
    mark = rig.mark()
    await rig.press("/api/fault-reset")
    shown = await rig.result("fault_reset")
    assert "reset refuse (aucun defaut a acquitter (SWITCHED_ON))" in shown, shown
    assert _commands(rig.since(mark), rig.regs.cmd) == []


async def test_the_fault_reset_is_refused_on_a_fault_the_console_may_not_reset(
    rig: Bench,
) -> None:
    """OCF, an overcurrent: named, the shaft read at rest, and still not resettable here."""
    rig.bus.registers[rig.regs.eta] = ETA_FAULT
    rig.bus.registers[rig.regs.lft] = LFT_OVERCURRENT
    await rig.polled()
    mark = rig.mark()
    await rig.press("/api/fault-reset")
    shown = await rig.result("fault_reset")
    assert "reset refuse (defaut OCF non rearmable depuis la console)" in shown, shown
    assert _commands(rig.since(mark), rig.regs.cmd) == []


# =========================================================================
# The bus thread's poll: what the page is shown, and what it is not
# =========================================================================


async def _until(predicate: Callable[[], bool], why: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not predicate():
        assert time.monotonic() < deadline, why
        await asyncio.sleep(0.01)


async def test_a_fault_is_shown_by_name_an_unknown_code_as_unknown_and_none_as_none(
    rig: Bench,
) -> None:
    await rig.polled()
    assert rig.worker.snap.fault == "aucun"
    rig.bus.registers[rig.regs.eta] = ETA_FAULT
    rig.bus.registers[rig.regs.lft] = LFT_OVERCURRENT
    await rig.polled()
    await rig.polled()
    assert (rig.worker.snap.drive_state, rig.worker.snap.fault) == ("FAULT", "OVERCURRENT")
    rig.bus.registers[rig.regs.lft] = UNLISTED_LFT
    await rig.polled()
    await rig.polled()
    assert rig.worker.snap.fault == "UNKNOWN", "never a plausible wrong name"


async def test_an_impossible_speed_or_current_is_never_shown(rig: Bench) -> None:
    rig.bus.registers[rig.regs.rfrd] = 5000
    rig.bus.registers[rig.regs.lcr] = 200
    await rig.polled()
    await rig.polled()
    snap = rig.worker.snap
    assert (snap.output_rpm, snap.current_a) == (None, None)
    assert snap.implausible >= 2
    assert snap.last_error == "courant 20.0 A impossible (plaque 2.15 A)"
    rig.bus.registers[rig.regs.rfrd] = 0
    rig.bus.registers[rig.regs.lcr] = 12
    await rig.polled()
    await rig.polled()
    assert (rig.worker.snap.output_rpm, rig.worker.snap.current_a) == (0, 1.2)


async def test_a_failed_read_clears_every_live_field(rig: Bench) -> None:
    await rig.polled()
    assert rig.worker.snap.drive_state == "SWITCH_ON_DISABLED"
    failed = rig.worker.snap.read_failed
    rig.bus.script_reads.append(io_exception())
    await _until(lambda: rig.worker.snap.read_failed > failed, "no failed read counted")
    snap = rig.worker.snap
    assert (snap.link_ok, snap.status_word, snap.drive_state, snap.enabled) == (
        False,
        None,
        None,
        None,
    )
    assert (snap.output_rpm, snap.current_a, snap.fault) == (None, None, None)
    await _until(lambda: rig.worker.snap.link_ok, "the next read did not bring it back")


async def test_a_keepalive_that_fails_is_said(rig: Bench) -> None:
    await rig.armed_and_enabled()
    await rig.polled()
    rig.bus.script_writes.append(io_exception())
    await _until(
        lambda: (rig.worker.snap.last_error or "").startswith("keepalive: "),
        "the failed keepalive was never said",
    )


async def test_a_lost_link_disarms_and_reconnects_without_resuming(rig: Bench) -> None:
    await rig.armed_and_enabled()
    rig.bus.sticky_reads = io_exception()
    await _until(lambda: not rig.worker.snap.armed, "a lost link never disarmed")
    assert rig.worker.snap.last_action.startswith("DESARME: liaison perdue.")
    rig.bus.sticky_reads = Behave.NORMALLY
    await _until(lambda: rig.worker.snap.reconnects >= 1, "never reconnected")
    await _until(lambda: rig.worker.snap.link_ok, "the link never came back")
    mark = rig.mark()
    await rig.polled()
    await rig.polled()
    assert rig.since(mark) == [], "the keepalive resumed by itself"


# =========================================================================
# What the page reads: GET /api/state
# =========================================================================

NO_REPLY: Final[str] = "pas de reponse du variateur (apres 0.000 s)"
"""What the page says of a request the drive did not answer, on this rig's clock."""


def test_the_link_error_rate_is_the_share_of_failed_reads() -> None:
    assert bench.Snapshot().error_rate() == 0.0, "no read yet: no rate"
    assert bench.Snapshot(read_ok=3, read_failed=1).error_rate() == 25.0
    assert bench.Snapshot(read_ok=2, read_failed=1).error_rate() == 33.3


def test_the_derived_values_are_computed_from_the_speed_and_current_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bench, "GEOMETRY", None)
    unknown = dict.fromkeys(("output_shaft_rpm", "hz", "gc", "gr", "torque_pct"))
    assert bench.Snapshot().derived() == unknown, "no speed read: nothing derived"
    assert bench.Snapshot(output_rpm=690, current_a=1.2).derived() == {
        "output_shaft_rpm": round(690 / 49.79, 2),
        "hz": 25.0,
        "gc": None,
        "gr": None,
        "torque_pct": 55.8,
    }
    assert bench.Snapshot(output_rpm=690).derived()["torque_pct"] is None
    # With the arm's radius, the g loads: omega^2 r / g at the arm, then with gravity.
    monkeypatch.setattr(bench, "GEOMETRY", MachineGeometry(radius=Metres(1.5)))
    omega = 2 * math.pi * (690 / 49.79) / 60
    load = omega**2 * 1.5 / 9.80665
    derived = bench.Snapshot(output_rpm=690, current_a=1.2).derived()
    assert derived["gc"] == round(load, 4)
    assert derived["gr"] == round(math.hypot(load, 1.0), 4)


async def test_the_page_reads_the_state_the_bus_thread_last_saw(
    rig: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bench, "GEOMETRY", None)
    rig.bus.registers[rig.regs.rfrd] = 690
    rig.bus.registers[rig.regs.lcr] = 12
    await rig.polled()
    await rig.polled()
    answered = await rig.page.get("/api/state")
    assert answered.status_code == HTTPStatus.OK
    state = cast(dict[str, object], answered.json())
    snap = rig.worker.snap
    assert (state["link_ok"], state["armed"], state["drive_state"], state["enabled"]) == (
        True,
        False,
        "SWITCH_ON_DISABLED",
        False,
    )
    assert (state["output_rpm"], state["current_a"], state["fault"], state["hsp_hz"]) == (
        690,
        1.2,
        "aucun",
        50.0,
    )
    assert state["derived"] == bench.Snapshot(output_rpm=690, current_a=1.2).derived()
    assert state["nameplate"] == {
        "rpm": bench.NOMINAL_RPM,
        "current_a": bench.NAMEPLATE_CURRENT_A,
        "gear_ratio": bench.GEAR_RATIO,
        "torque_nm": bench.OUTPUT_TORQUE_NM,
    }
    link = cast(dict[str, object], state["link"])
    assert (link["read_failed"], link["error_rate_pct"], link["implausible"]) == (0, 0.0, 0)
    assert isinstance(link["read_ok"], int)
    assert 0 < link["read_ok"] <= snap.read_ok
    assert state["max_rpm"] == 300
    rig.bus.registers[rig.regs.rfrd] = 0


async def test_the_page_itself_is_served(rig: Bench) -> None:
    answered = await rig.page.get("/")
    assert answered.status_code == HTTPStatus.OK
    assert "Acquitter d&eacute;faut" in answered.text


# =========================================================================
# What the page says when a drive call fails, and that nothing follows it
# =========================================================================


async def test_a_port_that_will_not_open_keeps_its_reason_until_it_opens_and_hsp_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator at the bench must know why the drive is not there: the reason stays on
    the page whatever the polls read, until an open and its HSP read succeed."""

    def dead(bus: FakeBus) -> None:
        bus.raise_on_connect = connection_exception()
        bus.sticky_reads = io_exception()

    async with started(monkeypatch, dead) as broken:
        for _ in range(4):
            await broken.polled()
        snap = broken.worker.snap
        assert snap.link_ok is False
        assert snap.hsp_hz is None, "HSP never read"
        why = snap.last_link_error
        assert why is not None
        assert why.startswith("ouverture: "), why
        state = cast(dict[str, object], (await broken.page.get("/api/state")).json())
        assert state["last_link_error"] == why
        assert broken.bus.writes() == []
        # The port comes up: the next open succeeds, HSP is read, the reason goes.
        broken.bus.raise_on_connect = None
        broken.bus.sticky_reads = Behave.NORMALLY
        await _until(lambda: broken.worker.snap.hsp_hz == 50.0, "HSP never read once open")
        await broken.polled()
        assert broken.worker.snap.last_link_error is None
        assert broken.worker.snap.link_ok is True
        assert broken.bus.writes() == []


async def test_an_open_that_fails_while_reads_go_through_is_tried_again_at_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The open refused while the drive still answers (a port the library reopens by
    itself): the bus thread opens it again at rest, and reads HSP once it does."""

    def refused(bus: FakeBus) -> None:
        bus.port_opens = False

    async with started(monkeypatch, refused) as halfway:
        await halfway.polled()
        await halfway.polled()
        snap = halfway.worker.snap
        assert snap.hsp_hz is None
        why = snap.last_link_error
        assert why is not None
        assert why.startswith("ouverture: "), why
        assert snap.link_ok is True, "the polls do answer"
        halfway.bus.port_opens = True
        await _until(lambda: halfway.worker.snap.hsp_hz == 50.0, "never opened again")
        assert halfway.worker.snap.last_link_error is None


async def test_a_ceiling_that_cannot_be_read_keeps_its_reason_on_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_hsp(bus: FakeBus) -> None:
        del bus.registers[bus.regs.hsp]

    async with started(monkeypatch, no_hsp) as unread:
        for _ in range(3):
            await unread.polled()
        snap = unread.worker.snap
        assert snap.hsp_hz is None, "shown as unknown, never guessed"
        assert snap.link_ok is True
        why = snap.last_link_error
        assert why is not None
        assert why.startswith("lecture HSP: "), why
        assert snap.last_error is None, "the polls succeed; the reason is kept apart"
        assert unread.bus.writes() == []


async def test_a_start_sequence_that_stalls_says_so_and_writes_no_15(rig: Bench) -> None:
    rig.bus.obeys = frozenset({ControlWord.SHUTDOWN, ControlWord.ENABLE_OPERATION})
    armed = await rig.press("/api/arm", {"motor_uncoupled": True})
    assert armed.status_code == HTTPStatus.OK
    mark = rig.mark()
    await rig.press("/api/enable")
    shown = await rig.result("enable")
    assert shown == "enable: ECHEC activation: etat READY au lieu de SWITCHED_ON"
    await rig.polled()
    lfrd, cmd = rig.regs.lfrd, rig.regs.cmd
    assert rig.since(mark) == [
        (lfrd, 0),
        (cmd, ControlWord.SHUTDOWN),
        (cmd, ControlWord.SWITCH_ON),
    ], "no 15 after the step that stalled, and no keepalive"


async def test_a_speed_the_drive_does_not_take_is_said_and_never_kept_alive(rig: Bench) -> None:
    armed = await rig.press("/api/arm", {"motor_uncoupled": True})
    assert armed.status_code == HTTPStatus.OK
    rig.bus.script_writes.append(io_exception())
    mark = rig.mark()
    await rig.press("/api/speed", {"rpm": 60})
    shown = await rig.result("speed")
    assert shown == f"speed 60 tr/min: ECHEC consigne 60: {NO_REPLY}"
    assert rig.worker.snap.setpoint_rpm == 0
    await rig.polled()
    assert rig.since(mark) == [(rig.regs.lfrd, 60)], "the one attempt, nothing after it"


async def test_a_stop_whose_shaft_cannot_be_read_never_writes_6(
    rig: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bench, "STOP_WAIT_S", 0.5)
    monkeypatch.setattr(bench, "STOP_POLL_S", 1.0)  # one read in the wait
    rfrd = rig.regs.rfrd
    del rig.bus.registers[rfrd]
    mark = rig.mark()
    await rig.press("/api/stop")
    shown = await rig.result("stop")
    rig.bus.registers[rfrd] = 0
    assert shown.startswith("stop: ECHEC: arret NON confirme apres 0 s (RFRD illisible ("), shown
    assert shown.endswith(
        "CMD=6 NON envoye (ce serait une roue libre). LFRD=0 ok, CMD=7 ok. Verifiez le variateur."
    ), shown
    written = rig.since(mark)
    assert _commands(written, rig.regs.cmd) == [ControlWord.SWITCH_ON]


async def test_a_confirmed_stop_whose_6_fails_says_so(rig: Bench) -> None:
    rig.bus.script_writes.extend((Behave.NORMALLY, Behave.NORMALLY, io_exception()))
    mark = rig.mark()
    await rig.press("/api/stop")
    shown = await rig.result("stop")
    assert shown == f"stop: arret confirme (RFRD=0) mais CMD=6 ECHEC: {NO_REPLY}"
    await rig.polled()
    lfrd, cmd = rig.regs.lfrd, rig.regs.cmd
    assert rig.since(mark) == [
        (lfrd, 0),
        (cmd, ControlWord.SWITCH_ON),
        (cmd, ControlWord.SHUTDOWN),
    ], "the 6 tried once, nothing after it"


@pytest.mark.parametrize(
    ("scripted", "said"),
    [
        pytest.param(
            (io_exception(),), f"LFRD=0 ECHEC: {NO_REPLY}, CMD=7 ok", id="the zero refused"
        ),
        pytest.param(
            (Behave.NORMALLY, io_exception()),
            f"LFRD=0 ok, CMD=7 ECHEC: {NO_REPLY}",
            id="the ramp word refused",
        ),
    ],
)
async def test_an_estop_half_done_says_which_half(
    rig: Bench, scripted: tuple[object, ...], said: str
) -> None:
    rig.bus.script_writes.extend(scripted)
    mark = rig.mark()
    await rig.press("/api/estop")
    shown = await rig.result("estop")
    assert shown == f"estop: ECHEC partiel: {said}"
    await rig.polled()
    assert rig.since(mark) == [(rig.regs.lfrd, 0), (rig.regs.cmd, ControlWord.SWITCH_ON)]


@pytest.mark.parametrize(
    ("scripted", "said", "words"),
    [
        pytest.param(
            (io_exception(),),
            f"ECHEC CMD=128: {NO_REPLY}",
            [ControlWord.FAULT_RESET],
            id="the reset refused",
        ),
        pytest.param(
            (Behave.NORMALLY, io_exception()),
            f"CMD=128 ok, CMD=6 ECHEC: {NO_REPLY}",
            [ControlWord.FAULT_RESET, ControlWord.SHUTDOWN],
            id="the 6 after it refused",
        ),
    ],
)
async def test_a_fault_reset_half_done_says_which_half(
    rig: Bench, scripted: tuple[object, ...], said: str, words: list[ControlWord]
) -> None:
    rig.bus.registers[rig.regs.eta] = ETA_FAULT
    rig.bus.registers[rig.regs.lft] = LFT_MODBUS_LOSS
    await rig.polled()
    rig.bus.script_writes.extend(scripted)
    mark = rig.mark()
    await rig.press("/api/fault-reset")
    shown = await rig.result("fault_reset")
    assert shown == f"fault_reset: {said}"
    await rig.polled()
    assert _commands(rig.since(mark), rig.regs.cmd) == words
