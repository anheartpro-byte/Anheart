"""Manual bench console for the ATV320. The OPERATOR drives it, not the software.

    python scripts/bench_console.py                    # MOTOR_PORT / MOTOR_SLAVE_ID from .env
    python scripts/bench_console.py --port ftdi://schneider:rs485/1 --slave 248
    -> http://127.0.0.1:8123

It runs on the HOST: Docker Desktop cannot pass a COM port or a USB device into
a container.

SCOPE: a BARE, UNCOUPLED motor. Not a loaded centrifuge, and never one with a
person in it -- that needs the application's safety supervisor.

## The transport is the application's

The link is opened exactly as the application opens it: ``drive_link.py``
builds :class:`src.motor.atv320.ATV320Drive` on
:func:`src.motor.atv320.serial_master`. For the Schneider USB-RS485 cable
(``ftdi://schneider:rs485/1``, no ``/dev/cu`` on macOS) that is the buffered
FTDI port whose ``in_waiting`` works, so a register costs ~tens of ms instead
of the 2 s pyftdi's own port cost. Every read is parsed and range-checked by the
driver, every speed write is read back, and ``retries`` cannot be set to the
pymodbus 3.7.4 value (0) that never reads a reply body.

## Why this file was rewritten (twice)

The first version displayed fabricated values: late replies were consumed as
the answer to the NEXT request, and 0x0637 = 1591 appeared simultaneously as the
status word, as 1591 rpm and as 159.1 A on a 2.15 A motor. The rules that came
out of that still stand:

1. ONE OWNER. A single dedicated thread owns the drive. Operator actions are
   queued and run on that thread. HTTP handlers only read a snapshot from
   memory, so they cannot contend for the bus, cannot block, and cannot fail.
2. NO LATE REPLY LEAKS FORWARD. With a working ``in_waiting`` pymodbus itself
   discards whatever is waiting before each request (``_send``), and closes the
   port after a failed exchange.
3. NEVER SHOW A VALUE FROM A FAILED TRANSACTION. An unread field becomes None
   and the UI greys it out.
4. REJECT PHYSICALLY IMPOSSIBLE READINGS against the nameplate.

The second rewrite fixed two review findings in the operator actions:

* **enable** writes LFRD = 0 (verified by read-back) BEFORE the start sequence.
  CMD = 15 with a stale non-zero LFRD from an earlier session would start the
  motor at that speed the instant the output stage energises.
* **stop** writes SHUTDOWN (6) only after RFRD has been READ as 0. Writing 6 to a
  turning shaft is CiA402 transition 8: the output stage drops and the machine
  freewheels. If standstill cannot be confirmed the run command is left in
  place (zero reference, the drive keeps ramping, ttO finishes the job) and the
  action is reported as a FAILURE, never as "ok".

And one safety change in the loop: when the driver latches the link down, the
console DISARMS. It then reconnects, but the keepalive does not resume until
the operator arms again -- no automatic resumption of motion, anywhere.

## If the link is unstable, suspect the physical layer first
Partial frames are an electrical symptom, not a software one. Check the common
0 V on RJ45 pin 8, shielded twisted pair earthed at the drive end, and the
RS-485 cable kept away from the motor cable. Measure the link with
``scripts/bench_comm_latency.py`` (read-only) before blaming this console.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Annotated, Literal, assert_never, override

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from scripts.drive_link import (
    BuiltDrive,
    add_link_arguments,
    arg_int,
    arg_text,
    build_drive,
    describe_error,
    link_from_args,
)
from src.clock import RealClock
from src.geometry import CONFIRMED_GEAR_RATIO, MachineGeometry
from src.local_config import DriveLink
from src.motor.drive import ControlWord, DriveState, DriveStatus, decode_speed
from src.result import Err, Ok
from src.units import Metres, Monotonic, MotorRpm, elapsed

# --- Nameplate: SEW-USOCOME KA37 DRS71S4/AL/DH --------------------------
# Read off the motor itself, not from a datasheet:
#   Hz 50            r/min 1380/28        kW 0.37 S1     cos phi 0.70
#   V 220-242 delta / 380-420 star        A 2.15 / 1.24
#   i 49.79          Nm 127               Cl.th 155(F)   IP66   IE1 66.6%
# The motor is wired in DELTA, so the 230 V / 2.15 A column applies.
NOMINAL_RPM = 1380
BASE_HZ = 50.0
GEAR_RATIO = CONFIRMED_GEAR_RATIO
NAMEPLATE_CURRENT_A = 2.15
OUTPUT_TORQUE_NM = 127.0


def _geometry_from_env() -> MachineGeometry | None:
    """The shared machine geometry, if ARM_RADIUS_M is set; else g is shown as unknown.

    Optional here (unlike the local console, where it is required): this is the
    commissioning tool, often run with the motor uncoupled and no arm fitted,
    and an unknown g is honest where a g "at 1 m" was not.
    """
    text = os.getenv("ARM_RADIUS_M", "").strip()
    if not text:
        return None
    try:
        return MachineGeometry(radius=Metres(float(text)))
    except ValueError:
        print(f"ARM_RADIUS_M={text!r} ignore: g affiche inconnu")
        return None


GEOMETRY = _geometry_from_env()

#: Plausibility bounds derived from the nameplate. A reading outside these did
#: not come from the drive's sensors, so it is reported as a transport fault
#: rather than displayed.
MAX_PLAUSIBLE_CURRENT_A = NAMEPLATE_CURRENT_A * 5.0
MAX_PLAUSIBLE_RPM = int(NOMINAL_RPM * 1.3)

CYCLE_S = 0.5

#: How long a stop waits for RFRD to read 0 before reporting failure. The
#: commissioned dEC is 3.0 s; 25 s is far past any healthy ramp and far short of
#: a freewheel.
STOP_WAIT_S = 25.0
STOP_POLL_S = 0.25

#: How long the process waits, at exit, for the worker's own stop sequence.
SHUTDOWN_JOIN_S = STOP_WAIT_S + 10.0


@dataclass
class Snapshot:
    """What the worker last managed to learn. None means "unknown", never 0.

    Mutable and shared on purpose: the worker thread writes whole fields, the
    HTTP handlers only read them. Each assignment is atomic under the GIL, and
    nothing here is read-modify-written from two threads.
    """

    link_ok: bool = False
    armed: bool = False
    setpoint_rpm: int = 0
    status_word: int | None = None
    drive_state: str | None = None
    enabled: bool | None = None
    output_rpm: int | None = None
    current_a: float | None = None
    fault: str | None = None
    hsp_hz: float | None = None
    cycles: int = 0
    read_ok: int = 0
    read_failed: int = 0
    implausible: int = 0
    reconnects: int = 0
    last_error: str | None = None
    last_action: str = "-"
    updated_at: float = field(default_factory=lambda: RealClock().monotonic())

    def error_rate(self) -> float:
        total = self.read_ok + self.read_failed
        return round(100.0 * self.read_failed / total, 1) if total else 0.0

    def derived(self) -> dict[str, float | None]:
        rpm = self.output_rpm
        if rpm is None:
            return {
                "output_shaft_rpm": None,
                "hz": None,
                "gc": None,
                "gr": None,
                "torque_pct": None,
            }
        out = rpm / GEAR_RATIO
        view = None if GEOMETRY is None else GEOMETRY.view(MotorRpm(rpm))
        cur = self.current_a
        return {
            "output_shaft_rpm": round(out, 2),
            "hz": round(rpm / NOMINAL_RPM * BASE_HZ, 2),
            "gc": None if view is None else round(view.g_load, 4),
            "gr": None if view is None else round(view.resultant_g, 4),
            "torque_pct": round(100.0 * cur / NAMEPLATE_CURRENT_A, 1) if cur is not None else None,
        }


Action = Literal["enable", "speed", "stop", "estop", "fault_reset"]


@dataclass(frozen=True, slots=True)
class Command:
    action: Action
    rpm: int = 0


class BusWorker(threading.Thread):
    """The ONLY thing in this process allowed to touch the drive.

    It runs its own asyncio loop, because :class:`ATV320Drive` is asynchronous
    (it pushes every blocking pymodbus call to its private executor). HTTP
    handlers run on uvicorn's loop and never see the drive.
    """

    def __init__(self, link: DriveLink, max_rpm: int) -> None:
        super().__init__(name="modbus-bus", daemon=True)
        self.link = link
        self.max_rpm = max_rpm
        self._clock = RealClock()
        self._built: BuiltDrive = build_drive(link, self._clock)
        self._commands: queue.Queue[Command] = queue.Queue()
        self._stop_requested = threading.Event()
        self.snap = Snapshot()
        self.exit_report: str = "-"

    def submit(self, cmd: Command) -> None:
        self._commands.put(cmd)

    def shutdown(self) -> None:
        self._stop_requested.set()

    @override
    def run(self) -> None:
        asyncio.run(self._main())

    # --- link ------------------------------------------------------------
    async def _connect(self) -> None:
        drive = self._built.drive
        match await drive.open():
            case Ok():
                self.snap.link_ok = True
                self.snap.last_error = None
            case Err(error):
                self.snap.link_ok = False
                self.snap.last_error = f"ouverture: {describe_error(error)}"
                return
        # HSP once per connection: it is the drive's own ceiling, and the
        # operator should see it before sending a speed.
        match await drive.read_limits():
            case Ok(limits):
                self.snap.hsp_hz = limits.high_speed
            case Err(error):
                self.snap.hsp_hz = None
                self.snap.last_error = f"lecture HSP: {describe_error(error)}"

    async def _recover_if_latched(self) -> None:
        """Reconnect a latched link -- after DISARMING, so nothing resumes by itself."""
        if not self._built.drive.link_lost:
            return
        if self.snap.armed:
            self.snap.armed = False
            self.snap.last_action = (
                "DESARME: liaison perdue. Le keepalive s'est arrete, le ttO du variateur "
                "arrete le moteur. Reverifiez puis rearmez."
            )
        self.snap.reconnects += 1
        await self._connect()

    # --- operator actions, executed on THIS thread only -----------------
    async def _zero_setpoint(self) -> str | None:
        """LFRD = 0, verified by read-back. None on success, else why not."""
        self.snap.setpoint_rpm = 0
        match await self._built.drive.write_speed(MotorRpm(0)):
            case Ok():
                return None
            case Err(error):
                return describe_error(error)

    async def _command(self, word: ControlWord) -> str | None:
        match await self._built.drive.write_command(word):
            case Ok():
                return None
            case Err(error):
                return describe_error(error)

    async def _do_enable(self) -> str:
        # LFRD = 0 FIRST. The start sequence ends with CMD = 15, and 15 with a
        # stale non-zero LFRD starts the motor at that speed. ATV320Drive.enable
        # deliberately writes no speed of its own (that is policy), so the
        # caller - this console - owns it.
        failed = await self._zero_setpoint()
        if failed is not None:
            return f"ECHEC: LFRD=0 non confirme ({failed}); activation NON tentee"
        match await self._built.drive.enable():
            case Ok():
                return "ok (LFRD=0 puis 6 -> 7 -> 15, chaque etape verifiee sur ETA)"
            case Err(error):
                return f"ECHEC activation: {describe_error(error)}"

    async def _do_speed(self, rpm: int) -> str:
        rpm = max(0, min(self.max_rpm, rpm))
        match await self._built.drive.write_speed(MotorRpm(rpm)):
            case Ok():
                self.snap.setpoint_rpm = rpm
                return "ok (relu sur LFRD)"
            case Err(error):
                return f"ECHEC consigne {rpm}: {describe_error(error)}"

    async def _await_standstill(self) -> tuple[bool, str]:
        """Poll RFRD until it READS 0. (confirmed, what was last seen)."""
        drive = self._built.drive
        rfrd = self.link.registers.rfrd
        started = self._clock.monotonic()
        last = "RFRD jamais lu"
        while elapsed(started, self._clock.monotonic()) < STOP_WAIT_S:
            match await drive.read_register(rfrd):
                case Ok(raw):
                    rpm = decode_speed(raw)
                    if rpm == 0:
                        return True, "RFRD=0"
                    last = f"RFRD={rpm} tr/min"
                case Err(error):
                    last = f"RFRD illisible ({describe_error(error)})"
            await asyncio.sleep(STOP_POLL_S)
        return False, last

    async def _do_stop(self) -> str:
        """Zero the reference, remove the run command, and ONLY on a read RFRD = 0, write 6.

        SWITCH_ON (7) out of OPERATION_ENABLED is CiA402 transition 5, which on
        this drive ramps on dEC. SHUTDOWN (6) out of OPERATION_ENABLED is
        transition 8 and freewheels - so it is written only once the shaft has
        been MEASURED stopped, never on a timer.
        """
        zero_failed = await self._zero_setpoint()
        run_failed = await self._command(ControlWord.SWITCH_ON)
        confirmed, last = await self._await_standstill()
        if not confirmed:
            return (
                f"ECHEC: arret NON confirme apres {STOP_WAIT_S:.0f} s ({last}). CMD=6 NON "
                "envoye (ce serait une roue libre). "
                + ("LFRD=0 ok" if zero_failed is None else f"LFRD=0 ECHEC: {zero_failed}")
                + ", "
                + ("CMD=7 ok" if run_failed is None else f"CMD=7 ECHEC: {run_failed}")
                + ". Verifiez le variateur."
            )
        shutdown_failed = await self._command(ControlWord.SHUTDOWN)
        if shutdown_failed is not None:
            return f"arret confirme ({last}) mais CMD=6 ECHEC: {shutdown_failed}"
        return f"ok ({last}, puis CMD=6)"

    async def _do_estop(self) -> str:
        """Fastest stop actually available: zero the reference, KEEP the ramp.

        Deliberately does not write 6: dropping the output stage would coast,
        and letting the drive's own ramp finish is faster.
        """
        self.snap.armed = False
        zero_failed = await self._zero_setpoint()
        run_failed = await self._command(ControlWord.SWITCH_ON)
        if zero_failed is None and run_failed is None:
            return "ok (LFRD=0 relu, CMD=7: rampe dEC)"
        return (
            "ECHEC partiel: "
            + ("LFRD=0 ok" if zero_failed is None else f"LFRD=0 ECHEC: {zero_failed}")
            + ", "
            + ("CMD=7 ok" if run_failed is None else f"CMD=7 ECHEC: {run_failed}")
        )

    async def _do_fault_reset(self) -> str:
        reset_failed = await self._command(ControlWord.FAULT_RESET)
        if reset_failed is not None:
            return f"ECHEC CMD=128: {reset_failed}"
        await asyncio.sleep(0.2)
        shutdown_failed = await self._command(ControlWord.SHUTDOWN)
        if shutdown_failed is not None:
            return f"CMD=128 ok, CMD=6 ECHEC: {shutdown_failed}"
        return "ok"

    async def _execute(self, cmd: Command) -> None:
        self.snap.last_action = f"{cmd.action}: en cours..."
        result = await self._dispatch(cmd)
        label = cmd.action + (f" {cmd.rpm} tr/min" if cmd.action == "speed" else "")
        self.snap.last_action = f"{label}: {result}"

    async def _dispatch(self, cmd: Command) -> str:
        match cmd.action:
            case "enable":
                return await self._do_enable()
            case "speed":
                return await self._do_speed(cmd.rpm)
            case "stop":
                return await self._do_stop()
            case "estop":
                return await self._do_estop()
            case "fault_reset":
                return await self._do_fault_reset()
            case _ as unreachable:
                assert_never(unreachable)

    # --- the cycle -------------------------------------------------------
    def _plausible_rpm(self, rpm: MotorRpm) -> int | None:
        if abs(rpm) > MAX_PLAUSIBLE_RPM:
            self.snap.implausible += 1
            self.snap.last_error = f"vitesse {rpm} tr/min impossible (plaque {NOMINAL_RPM})"
            return None
        return rpm

    def _plausible_current(self, amps: float) -> float | None:
        if amps > MAX_PLAUSIBLE_CURRENT_A:
            self.snap.implausible += 1
            self.snap.last_error = f"courant {amps} A impossible (plaque {NAMEPLATE_CURRENT_A} A)"
            return None
        return round(amps, 1)

    def _clear_live_fields(self) -> None:
        """Everything derived from the status word becomes unknown.

        Reporting a stale state as current is what made the old console lie.
        """
        self.snap.link_ok = False
        self.snap.status_word = None
        self.snap.drive_state = None
        self.snap.enabled = None
        self.snap.output_rpm = None
        self.snap.current_a = None
        self.snap.fault = None

    def _show(self, status: DriveStatus) -> None:
        self.snap.link_ok = True
        self.snap.last_error = None
        self.snap.status_word = status.status_word
        self.snap.drive_state = status.state.name
        self.snap.enabled = status.state is DriveState.OPERATION_ENABLED
        self.snap.output_rpm = self._plausible_rpm(status.output_rpm)
        self.snap.current_a = self._plausible_current(status.current)
        if not status.fault_present:
            self.snap.fault = "aucun"
        elif status.fault is not None:
            self.snap.fault = status.fault.name
        else:
            self.snap.fault = "defaut (LFT non lu)"

    async def _poll(self) -> None:
        match await self._built.drive.read_status():
            case Ok(status):
                self.snap.read_ok += 1
                self._show(status)
            case Err(error):
                self.snap.read_failed += 1
                self._clear_live_fields()
                self.snap.last_error = describe_error(error)
                return
        # Keepalive: refresh the setpoint so the drive's ttO timer is fed. If
        # this console dies, writes stop and the drive stops the motor by
        # itself -- a watchdog outside this process.
        if self.snap.armed and self.snap.enabled:
            match await self._built.drive.write_speed(MotorRpm(self.snap.setpoint_rpm)):
                case Ok():
                    pass
                case Err(error):
                    self.snap.last_error = f"keepalive: {describe_error(error)}"

    async def _main(self) -> None:
        await self._connect()
        while not self._stop_requested.is_set():
            started: Monotonic = self._clock.monotonic()
            while True:
                try:
                    cmd = self._commands.get_nowait()
                except queue.Empty:
                    break
                await self._execute(cmd)
            await self._recover_if_latched()
            if not self._built.drive.link_lost:
                await self._poll()
            self.snap.cycles += 1
            self.snap.updated_at = self._clock.monotonic()
            spent = elapsed(started, self._clock.monotonic())
            await asyncio.sleep(max(0.05, CYCLE_S - spent))
        # The driver's own stop: LFRD = 0, RFRD polled to standstill, only then
        # 7 and 6 - and if standstill is not confirmed it leaves the run command
        # and says so. Then the port is released.
        match await self._built.drive.close():
            case Ok():
                self.exit_report = "arret confirme, port libere"
            case Err(error):
                self.exit_report = f"ATTENTION: {describe_error(error)}"


worker: BusWorker | None = None


def need_worker() -> BusWorker:
    if worker is None:
        raise HTTPException(503, "bus non demarre")
    return worker


# Request bodies as annotated scalars rather than pydantic models: a BaseModel
# subclass drags pydantic's explicit Any into this file under mypy strict.
SpeedRpm = Annotated[int, Body(embed=True, ge=0, le=NOMINAL_RPM)]
MotorUncoupled = Annotated[bool, Body(embed=True)]


app = FastAPI(title="AnHeart bench console")


@app.get("/api/state")
async def api_state() -> JSONResponse:
    """Pure memory read: never touches the bus, so it cannot block or fail."""
    w = need_worker()
    s = w.snap
    return JSONResponse(
        {
            "link_ok": s.link_ok,
            "armed": s.armed,
            "setpoint_rpm": s.setpoint_rpm,
            "status_word": s.status_word,
            "drive_state": s.drive_state,
            "enabled": s.enabled,
            "output_rpm": s.output_rpm,
            "current_a": s.current_a,
            "fault": s.fault,
            "hsp_hz": s.hsp_hz,
            "derived": s.derived(),
            "nameplate": {
                "rpm": NOMINAL_RPM,
                "current_a": NAMEPLATE_CURRENT_A,
                "gear_ratio": GEAR_RATIO,
                "torque_nm": OUTPUT_TORQUE_NM,
            },
            "link": {
                "cycles": s.cycles,
                "read_ok": s.read_ok,
                "read_failed": s.read_failed,
                "implausible": s.implausible,
                "reconnects": s.reconnects,
                "error_rate_pct": s.error_rate(),
            },
            "last_error": s.last_error,
            "last_action": s.last_action,
            "age_s": round(RealClock().monotonic() - s.updated_at, 2),
            "max_rpm": w.max_rpm,
        }
    )


@app.post("/api/arm")
async def api_arm(motor_uncoupled: MotorUncoupled) -> JSONResponse:
    w = need_worker()
    w.snap.armed = motor_uncoupled
    w.snap.last_action = "arme par l'operateur" if w.snap.armed else "desarme"
    return JSONResponse({"armed": w.snap.armed})


@app.post("/api/enable")
async def api_enable() -> JSONResponse:
    w = need_worker()
    if not w.snap.armed:
        raise HTTPException(409, "console non armee")
    w.submit(Command("enable"))
    return JSONResponse({"queued": "enable"})


@app.post("/api/speed")
async def api_speed(rpm: SpeedRpm) -> JSONResponse:
    w = need_worker()
    if not w.snap.armed:
        raise HTTPException(409, "console non armee")
    w.submit(Command("speed", rpm))
    return JSONResponse({"queued": "speed", "rpm": rpm})


@app.post("/api/stop")
async def api_stop() -> JSONResponse:
    need_worker().submit(Command("stop"))
    return JSONResponse({"queued": "stop"})


@app.post("/api/estop")
async def api_estop() -> JSONResponse:
    w = need_worker()
    # Latch immediately and synchronously, before the queue is even drained:
    # the operator's stop must not wait on a cycle stuck on a bad link.
    w.snap.armed = False
    w.submit(Command("estop"))
    return JSONResponse({"queued": "estop", "armed": False})


@app.post("/api/fault-reset")
async def api_fault_reset() -> JSONResponse:
    need_worker().submit(Command("fault_reset"))
    return JSONResponse({"queued": "fault_reset"})


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(PAGE)


PAGE = """
<!doctype html><meta charset=utf-8><title>Banc ATV320</title>
<style>
 body{background:#12141a;color:#e7e9ee;font:15px/1.5 system-ui,sans-serif;margin:0;padding:18px}
 h1{font-size:17px;margin:0 0 4px;color:#9aa4b2}
 .np{font-size:12px;color:#6b7280;margin-bottom:12px}
 .warn{background:#3a2409;border:1px solid #7a5210;color:#ffd28a;padding:10px 12px;border-radius:8px;margin-bottom:12px}
 .bad{background:#3a0f0f;border:1px solid #7a1010;color:#ffb0b0;padding:10px 12px;border-radius:8px;margin-bottom:12px;display:none}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
 .c{background:#1b1f27;border:1px solid #2a303c;border-radius:8px;padding:10px 12px}
 .k{font-size:11px;color:#8b94a3;text-transform:uppercase;letter-spacing:.5px}
 .v{font-size:22px;font-variant-numeric:tabular-nums;margin-top:2px}
 .unk{color:#6b7280}
 button{font:600 15px system-ui;border:0;border-radius:8px;padding:12px 18px;cursor:pointer;margin:0 8px 8px 0}
 .go{background:#1f6f43;color:#fff}.go:disabled{background:#2a303c;color:#6b7280;cursor:not-allowed}
 .stop{background:#8a5a12;color:#fff}
 .estop{background:#a11d1d;color:#fff;font-size:19px;padding:20px 30px}
 input[type=range]{width:100%}
 .arm{background:#1b1f27;border:1px solid #2a303c;border-radius:8px;padding:12px;margin-bottom:12px}
</style>
<h1>Banc ATV320 &mdash; pilotage manuel</h1>
<div class=np id=np>-</div>
<div class=warn><b>Moteur d&eacute;saccoupl&eacute; uniquement.</b> Pas de centrifugeuse charg&eacute;e, jamais avec une personne dedans.</div>
<div class=bad id=linkbad></div>

<div class=arm>
 <label><input type=checkbox id=unc> Je confirme que le moteur est <b>d&eacute;saccoupl&eacute;</b> et la zone d&eacute;gag&eacute;e</label>
 <div style="margin-top:10px"><button class=go id=arm disabled>Armer</button>
 <span id=armstate style="color:#8b94a3"></span></div>
</div>

<div class=grid>
 <div class=c><div class=k>&Eacute;tat variateur</div><div class=v id=st>-</div></div>
 <div class=c><div class=k>Vitesse moteur</div><div class=v id=rpm>-</div></div>
 <div class=c><div class=k>Arbre sortie</div><div class=v id=out>-</div></div>
 <div class=c><div class=k>Fr&eacute;quence</div><div class=v id=hz>-</div></div>
 <div class=c><div class=k>Courant</div><div class=v id=amp>-</div></div>
 <div class=c><div class=k>Charge</div><div class=v id=tq>-</div></div>
 <div class=c><div class=k>D&eacute;faut</div><div class=v id=flt>-</div></div>
 <div class=c><div class=k>ETA</div><div class=v id=eta>-</div></div>
 <div class=c><div class=k>Consigne</div><div class=v id=sp>-</div></div>
 <div class=c><div class=k>Qualit&eacute; liaison</div><div class=v id=lq>-</div></div>
</div>

<div class=c style="margin-bottom:12px">
 <div class=k>Consigne &mdash; <span id=maxr></span></div>
 <input type=range id=sl min=0 max=300 step=5 value=0 disabled>
 <div><span class=v id=slv>0</span> tr/min moteur</div>
 <button class=go id=send disabled>Envoyer</button>
</div>
<div>
 <button class=go id=en disabled>1. Activer (LFRD=0, 6&rarr;7&rarr;15)</button>
 <button class=stop id=stp>2. Arr&ecirc;t en rampe</button>
 <button class=go id=fr>Acquitter d&eacute;faut</button>
</div>
<div style="margin-top:16px"><button class=estop id=es>ARR&Ecirc;T D'URGENCE</button></div>
<p style="color:#8b94a3" id=act>-</p>

<script>
const $=i=>document.getElementById(i);
$('unc').onchange=()=>{$('arm').disabled=!$('unc').checked};
$('arm').onclick=async()=>{
 const r=await(await fetch('/api/arm',{method:'POST',headers:{'content-type':'application/json'},
   body:JSON.stringify({motor_uncoupled:$('unc').checked})})).json();
 $('armstate').textContent=r.armed?'ARMEE':'non armee';
 for(const id of ['en','sl','send']) $(id).disabled=!r.armed;
};
$('sl').oninput=()=>$('slv').textContent=$('sl').value;
$('send').onclick=()=>fetch('/api/speed',{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({rpm:+$('sl').value})});
$('en').onclick =()=>fetch('/api/enable',{method:'POST'});
$('stp').onclick=()=>fetch('/api/stop',{method:'POST'});
$('fr').onclick =()=>fetch('/api/fault-reset',{method:'POST'});
$('es').onclick =()=>{fetch('/api/estop',{method:'POST'});
 $('armstate').textContent='desarme par arret d urgence'; $('unc').checked=false; $('arm').disabled=true;
 for(const id of ['en','sl','send']) $(id).disabled=true;};

// An unread field shows "?" in grey. It is NEVER filled with a stale or
// guessed value: a plausible wrong number is worse than no number here.
const set=(id,val)=>{const e=$(id);
 if(val===null||val===undefined){e.textContent='?';e.className='v unk';}
 else {e.textContent=val;e.className='v';}};

async function tick(){
 try{
  const s=await(await fetch('/api/state')).json();
  const n=s.nameplate;
  $('np').textContent='SEW KA37 DRS71S4 - '+n.rpm+' tr/min a 50 Hz, '+n.current_a+' A, i='+n.gear_ratio+', '+n.torque_nm+' Nm';
  set('st', s.drive_state); set('rpm', s.output_rpm===null?null:s.output_rpm+' tr/min');
  set('out', s.derived.output_shaft_rpm===null?null:s.derived.output_shaft_rpm+' tr/min');
  set('hz',  s.derived.hz===null?null:s.derived.hz+' Hz');
  set('amp', s.current_a===null?null:s.current_a+' A');
  set('tq',  s.derived.torque_pct===null?null:s.derived.torque_pct+'%');
  set('flt', s.fault);
  set('eta', s.status_word===null?null:'0x'+s.status_word.toString(16).padStart(4,'0'));
  set('sp',  s.setpoint_rpm+' tr/min');
  set('lq',  s.link.error_rate_pct+'% err');
  $('maxr').textContent='plafond '+s.max_rpm+' tr/min'; $('sl').max=s.max_rpm;
  const b=$('linkbad');
  if(!s.link_ok){ b.style.display='block';
    b.innerHTML='<b>LIAISON MODBUS PERDUE.</b> '+(s.last_error||'')+
      ' &mdash; '+s.link.read_failed+' lectures echouees, '+s.link.implausible+
      ' valeurs impossibles rejetees, '+s.link.reconnects+' reconnexions.'+
      ' Les champs ? ne sont PAS connus. Verifiez la masse (broche 8), le blindage,'+
      ' et l eloignement du cable moteur.';
  } else if(s.link.error_rate_pct>5){ b.style.display='block';
    b.innerHTML='<b>Liaison degradee :</b> '+s.link.error_rate_pct+'% d echecs, '+
      s.link.implausible+' valeurs impossibles rejetees.';
  } else b.style.display='none';
  $('act').textContent='derniere action: '+s.last_action+(s.last_error?(' | '+s.last_error):'');
 }catch(e){ set('st',null); $('linkbad').style.display='block';
   $('linkbad').innerHTML='<b>CONSOLE INJOIGNABLE.</b>'; }
}
setInterval(tick,500); tick();
</script>
"""


def main() -> int:
    global worker  # noqa: PLW0603
    ap = argparse.ArgumentParser(description="Console manuelle ATV320")
    add_link_arguments(ap)
    ap.add_argument("--max-rpm", default=os.getenv("MOTOR_MAX_RPM", "300"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--http-port", default="8123")
    args = ap.parse_args()
    try:
        max_rpm = arg_int(args, "max_rpm")
        host = arg_text(args, "host")
        http_port = arg_int(args, "http_port")
        link = link_from_args(args)
    except ValueError as exc:
        print(f"configuration refusee: {exc}")
        return 2
    if not 0 <= max_rpm <= NOMINAL_RPM:
        ap.error(f"--max-rpm={max_rpm} doit etre dans 0..{NOMINAL_RPM}")

    worker = BusWorker(link, max_rpm)
    worker.start()
    print(f"lien: {link.describe()}")
    print(f"plaque: {NOMINAL_RPM} tr/min a {BASE_HZ} Hz, {NAMEPLATE_CURRENT_A} A, i={GEAR_RATIO}")
    print(f"rejet des lectures > {MAX_PLAUSIBLE_CURRENT_A} A ou > {MAX_PLAUSIBLE_RPM} tr/min")
    time.sleep(1.5)
    s = worker.snap
    if s.link_ok and s.status_word is not None:
        print(f"variateur OK: {s.drive_state}, ETA=0x{s.status_word:04X}, HSP={s.hsp_hz} Hz")
    else:
        print(f"liaison NON etablie: {s.last_error}")
        print("La console demarre quand meme et affichera l'etat reel de la liaison.")
    print(f"\n  ->  http://{host}:{http_port}\n")
    print("Rien ne tourne avant que VOUS armiez et cliquiez.")
    try:
        uvicorn.run(app, host=host, port=http_port, log_level="warning")
    finally:
        # The worker's exit runs the driver's stop sequence; wait for it rather
        # than letting a daemon thread die mid-stop with the process.
        worker.shutdown()
        worker.join(timeout=SHUTDOWN_JOIN_S)
        print(f"sortie: {worker.exit_report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
