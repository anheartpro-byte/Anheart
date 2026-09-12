"""Manual bench console for the ATV320. The OPERATOR drives it, not the software.

    .venv\\Scripts\\python.exe scripts\\bench_console.py
    -> http://127.0.0.1:8123

It runs on the HOST: Docker Desktop on Windows cannot pass a COM port into a
container. It talks to pymodbus directly rather than importing
src/motor/atv320.py, so a bench tool does not break while application code is
being rewritten.

SCOPE: a BARE, UNCOUPLED motor. Not a loaded centrifuge, and never one with a
person in it -- that needs the application's safety supervisor.

## Why this file was rewritten

The first version displayed fabricated values. It polled five registers twice a
second while a keepalive wrote twice a second, roughly 14 transactions per
second on a 19200 baud bus. Transactions timed out, and because nothing flushed
the serial buffer afterwards, each late reply was consumed as the answer to the
NEXT request. Readings desynchronised by one transaction, which showed up
unmistakably: 0x0637 = 1591 appeared simultaneously as the status word, as
1591 rpm and as 159.1 A -- on a motor whose nameplate says 2.15 A.

Four rules came out of that, and they are the design of this file:

1. ONE OWNER. A single dedicated thread owns the serial client. Nothing else
   touches it, ever. Operator actions are queued and run on that thread. HTTP
   handlers only read a snapshot from memory, so they cannot contend for the
   bus, cannot block, and cannot fail.
2. FLUSH AND RECONNECT ON EVERY FAILURE, so a late reply can never be mistaken
   for the next answer.
3. NEVER SHOW A VALUE FROM A FAILED TRANSACTION. An unread field becomes None
   and the UI greys it out. A plausible wrong number is far worse than no
   number on a screen whose whole job is to say what the machine is doing.
4. REJECT PHYSICALLY IMPOSSIBLE READINGS. The nameplate bounds what the
   hardware can report, so a value outside them is a transport fault, not a
   measurement. This is what would have caught the 159 A instantly.

## Measured facts, not assumptions
  address 248   Schneider point-to-point access address. Address 1 did not answer.
  19200 8E1     factory serial format.
  offset 0      logical addresses used as-is; at -1 reads return 0x8000 garbage.
  HSP @ 3104    reads 500 = 50.0 Hz, matching the keypad.

## If the link is unstable, suspect the physical layer first
Partial frames ("expected at least 4 bytes, 2 received") are an electrical
symptom, not a software one. A running drive is a strong EMI source, so check:
the common/0 V on RJ45 pin 8 actually connected, shielded twisted pair with the
shield earthed at the drive end, the RS-485 cable kept away from the motor
cable, and the FTDI latency timer lowered from its 16 ms default.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from pymodbus.client import ModbusSerialClient

# --- Register map, offset 0 ----------------------------------------------
CMD = 8501  # W: CiA402 command word
LFRD = 8602  # W: speed setpoint, SIGNED rpm
ETA = 3201  # R: status word
RFRD = 8604  # R: output speed, SIGNED rpm
LCR = 3204  # R: motor current, 0.1 A per count
LFT = 7121  # R: last fault
HSP = 3104  # R: high speed, 0.1 Hz per count

W_SHUTDOWN, W_SWITCH_ON, W_ENABLE, W_FAULT_RESET = 6, 7, 15, 128

FAULT_BIT = 0x08
STATE_MASK, RUNNING, SWITCHED_ON, READY = 0x6F, 0x27, 0x23, 0x21
SOD_MASK, SOD = 0x4F, 0x40

# --- Nameplate: SEW-USOCOME KA37 DRS71S4/AL/DH --------------------------
# Read off the motor itself, not from a datasheet:
#   Hz 50            r/min 1380/28        kW 0.37 S1     cos phi 0.70
#   V 220-242 delta / 380-420 star        A 2.15 / 1.24
#   i 49.79          Nm 127               Cl.th 155(F)   IP66   IE1 66.6%
#   Inverter duty VPWM, 20.189 kg, gearbox oil CLP HC-68-NSF-H1 0.50 l
# The motor is wired in DELTA, so the 230 V / 2.15 A column applies.
NOMINAL_RPM = 1380
BASE_HZ = 50.0
GEAR_RATIO = 49.79
NAMEPLATE_CURRENT_A = 2.15
OUTPUT_TORQUE_NM = 127.0
GRAVITY = 9.80665

#: Plausibility bounds derived from the nameplate. A reading outside these did
#: not come from the drive's sensors, so it is reported as a transport fault
#: rather than displayed. 5x nameplate current is already well past anything
#: the 0.37 kW drive can deliver; 1.3x nominal speed allows for field weakening
#: while still rejecting a desynchronised frame.
MAX_PLAUSIBLE_CURRENT_A = NAMEPLATE_CURRENT_A * 5.0
MAX_PLAUSIBLE_RPM = int(NOMINAL_RPM * 1.3)

CYCLE_S = 0.5
#: Bounded retries inside one cycle, so a bad link cannot stall the loop.
READ_ATTEMPTS = 2


def decode_state(word: int) -> str:
    """Name the CiA402 state. Fault wins: the drive can report both at once."""
    if word & FAULT_BIT:
        return "FAULT"
    if word & SOD_MASK == SOD:
        return "SWITCH_ON_DISABLED"
    masked = word & STATE_MASK
    if masked == RUNNING:
        return "OPERATION_ENABLED"
    if masked == SWITCHED_ON:
        return "SWITCHED_ON"
    if masked == READY:
        return "READY_TO_SWITCH_ON"
    return "NOT_READY"


def signed(raw: int) -> int:
    return raw - 0x10000 if raw > 0x7FFF else raw


@dataclass
class Snapshot:
    """What the worker last managed to learn. None means "unknown", never 0."""

    link_ok: bool = False
    armed: bool = False
    setpoint_rpm: int = 0
    status_word: int | None = None
    drive_state: str | None = None
    enabled: bool | None = None
    output_rpm: int | None = None
    current_a: float | None = None
    fault_code: int | None = None
    hsp_hz: float | None = None
    cycles: int = 0
    read_ok: int = 0
    read_failed: int = 0
    implausible: int = 0
    reconnects: int = 0
    last_error: str | None = None
    last_action: str = "-"
    updated_at: float = field(default_factory=time.time)

    def error_rate(self) -> float:
        total = self.read_ok + self.read_failed
        return round(100.0 * self.read_failed / total, 1) if total else 0.0

    def derived(self) -> dict[str, float | None]:
        rpm = self.output_rpm
        if rpm is None:
            return {"output_shaft_rpm": None, "hz": None, "g_at_1m": None, "torque_pct": None}
        out = rpm / GEAR_RATIO
        omega = 2.0 * math.pi * out / 60.0
        cur = self.current_a
        return {
            "output_shaft_rpm": round(out, 2),
            "hz": round(rpm / NOMINAL_RPM * BASE_HZ, 2),
            "g_at_1m": round(omega * omega / GRAVITY, 4),
            "torque_pct": round(100.0 * cur / NAMEPLATE_CURRENT_A, 1) if cur is not None else None,
        }


Action = Literal["enable", "speed", "stop", "estop", "fault_reset"]


@dataclass(frozen=True)
class Command:
    action: Action
    rpm: int = 0


class BusWorker(threading.Thread):
    """The ONLY thing in this process allowed to touch the serial port."""

    def __init__(self, port: str, slave: int, baud: int, parity: str, max_rpm: int) -> None:
        super().__init__(name="modbus-bus", daemon=True)
        self.port, self.slave, self.max_rpm = port, slave, max_rpm
        self._baud, self._parity = baud, parity
        self._client: ModbusSerialClient | None = None
        self._commands: queue.Queue[Command] = queue.Queue()
        self._stop = threading.Event()
        self.snap = Snapshot()

    def submit(self, cmd: Command) -> None:
        self._commands.put(cmd)

    def shutdown(self) -> None:
        self._stop.set()

    # --- serial plumbing -------------------------------------------------
    def _open(self) -> bool:
        self._client = ModbusSerialClient(
            port=self.port,
            baudrate=self._baud,
            bytesize=8,
            parity=self._parity,
            stopbits=1,
            timeout=0.5,
            retries=0,
        )
        return bool(self._client.connect())

    def _recover(self, why: str) -> None:
        """Drain the buffers and reopen, so a late reply cannot leak forward.

        This is the fix for the desynchronisation that made the first version of
        this console display invented numbers.
        """
        self.snap.last_error = why
        self.snap.reconnects += 1
        self.snap.link_ok = False
        if self._client is not None:
            sock = getattr(self._client, "socket", None)
            if sock is not None:
                with contextlib.suppress(Exception):
                    sock.reset_input_buffer()
                with contextlib.suppress(Exception):
                    sock.reset_output_buffer()
            with contextlib.suppress(Exception):
                self._client.close()
        time.sleep(0.1)
        with contextlib.suppress(Exception):
            self._open()

    def _read(self, addr: int) -> int | None:
        """One register, or None. Never returns a value from a failed exchange."""
        for _ in range(READ_ATTEMPTS):
            if self._client is None:
                self._recover("client ferme")
                continue
            try:
                rr = self._client.read_holding_registers(address=addr, count=1, slave=self.slave)
            except Exception as exc:
                self._recover(f"read {addr}: {type(exc).__name__}")
                continue
            if rr.isError():
                self._recover(f"read {addr}: {rr}"[:120])
                continue
            regs = getattr(rr, "registers", None)
            if not regs:
                # An error response carries no registers; the first version
                # indexed it blindly and turned that into an HTTP 500.
                self._recover(f"read {addr}: reponse sans registre")
                continue
            self.snap.read_ok += 1
            return int(regs[0])
        self.snap.read_failed += 1
        return None

    def _write(self, addr: int, value: int) -> bool:
        if self._client is None:
            self._recover("client ferme")
            return False
        try:
            rr = self._client.write_register(address=addr, value=value & 0xFFFF, slave=self.slave)
        except Exception as exc:
            self._recover(f"write {addr}: {type(exc).__name__}")
            return False
        if rr.isError():
            self._recover(f"write {addr}: rejete")
            return False
        return True

    # --- operator actions, executed on THIS thread only -----------------
    def _do_enable(self) -> str:
        for word, expect in (
            (W_SHUTDOWN, None),
            (W_SWITCH_ON, None),
            (W_ENABLE, "OPERATION_ENABLED"),
        ):
            if not self._write(CMD, word):
                return f"echec CMD={word}"
            time.sleep(0.15)
            got = self._read(ETA)
            if got is None:
                return f"pas de reponse apres CMD={word}"
            if got & FAULT_BIT:
                return f"defaut present (ETA=0x{got:04X}), acquittez d'abord"
            if expect is not None and decode_state(got) != expect:
                return f"CMD={word} n'a pas mene a {expect} (ETA=0x{got:04X})"
        return "ok"

    def _do_speed(self, rpm: int) -> str:
        rpm = max(0, min(self.max_rpm, rpm))
        if not self._write(LFRD, rpm):
            return "echec ecriture LFRD"
        self.snap.setpoint_rpm = rpm
        echo = self._read(LFRD)
        if echo is None:
            return f"consigne {rpm} envoyee mais relecture impossible"
        if signed(echo) != rpm:
            # Guards against a wrong register offset silently writing a speed
            # into some other live Altivar parameter.
            return f"relecture {signed(echo)} != {rpm} demande"
        return "ok"

    def _do_stop(self) -> str:
        """Zero the reference, let the drive ramp on dEC, THEN drop the stage.

        Writing 6 straight from OPERATION_ENABLED is CiA402 transition 8: it
        removes torque at speed and freewheels.
        """
        self._write(LFRD, 0)
        self.snap.setpoint_rpm = 0
        if not self._write(CMD, W_SWITCH_ON):
            return "echec CMD=7"
        deadline = time.time() + 25.0
        while time.time() < deadline:
            rfrd = self._read(RFRD)
            if rfrd is not None and abs(signed(rfrd)) <= 5:
                break
            time.sleep(0.25)
        self._write(CMD, W_SHUTDOWN)
        return "ok"

    def _do_estop(self) -> str:
        """Fastest stop actually available: zero the reference, KEEP the ramp.

        Deliberately does not remove the run command: dropping the output stage
        would coast, and this drive's DC bus absorbs only a few per cent of the
        rotating energy, so letting its own ramp finish is faster.
        """
        self._write(LFRD, 0)
        self.snap.setpoint_rpm = 0
        self._write(CMD, W_SWITCH_ON)
        self.snap.armed = False
        return "ok"

    def _do_fault_reset(self) -> str:
        if not self._write(CMD, W_FAULT_RESET):
            return "echec CMD=128"
        time.sleep(0.2)
        self._write(CMD, W_SHUTDOWN)
        return "ok"

    def _execute(self, cmd: Command) -> None:
        handlers = {
            "enable": self._do_enable,
            "stop": self._do_stop,
            "estop": self._do_estop,
            "fault_reset": self._do_fault_reset,
        }
        result = self._do_speed(cmd.rpm) if cmd.action == "speed" else handlers[cmd.action]()
        label = cmd.action + (f" {cmd.rpm} tr/min" if cmd.action == "speed" else "")
        self.snap.last_action = f"{label}: {result}"

    def _plausible_rpm(self, raw: int | None) -> int | None:
        if raw is None:
            return None
        rpm = signed(raw)
        if abs(rpm) > MAX_PLAUSIBLE_RPM:
            self.snap.implausible += 1
            self._recover(f"vitesse {rpm} tr/min impossible (plaque {NOMINAL_RPM})")
            return None
        return rpm

    def _plausible_current(self, raw: int | None) -> float | None:
        if raw is None:
            return None
        amps = raw / 10.0
        if amps > MAX_PLAUSIBLE_CURRENT_A:
            self.snap.implausible += 1
            self._recover(f"courant {amps} A impossible (plaque {NAMEPLATE_CURRENT_A} A)")
            return None
        return round(amps, 1)

    # --- the cycle -------------------------------------------------------
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

    def run(self) -> None:
        if not self._open():
            self.snap.last_error = "le port ne s'ouvre pas (SoMove le tient ?)"
        while not self._stop.is_set():
            started = time.time()

            while True:
                try:
                    self._execute(self._commands.get_nowait())
                except queue.Empty:
                    break

            word = self._read(ETA)
            if word is None:
                self._clear_live_fields()
            else:
                self.snap.link_ok = True
                self.snap.last_error = None
                self.snap.status_word = word
                self.snap.drive_state = decode_state(word)
                self.snap.enabled = self.snap.drive_state == "OPERATION_ENABLED"
                self.snap.output_rpm = self._plausible_rpm(self._read(RFRD))
                self.snap.current_a = self._plausible_current(self._read(LCR))
                if word & FAULT_BIT or self.snap.fault_code is None:
                    self.snap.fault_code = self._read(LFT)
                if self.snap.hsp_hz is None:
                    hsp = self._read(HSP)
                    if hsp is not None:
                        self.snap.hsp_hz = hsp / 10.0
                # Keepalive: refresh the setpoint so the drive's ttO timer is
                # fed. If this console dies, writes stop and the drive stops the
                # motor by itself -- a watchdog outside this process.
                if self.snap.armed and self.snap.enabled:
                    self._write(LFRD, self.snap.setpoint_rpm)

            self.snap.cycles += 1
            self.snap.updated_at = time.time()
            time.sleep(max(0.05, CYCLE_S - (time.time() - started)))

        with contextlib.suppress(Exception):
            if self._client is not None:
                self._client.close()


worker: BusWorker | None = None


def need_worker() -> BusWorker:
    if worker is None:
        raise HTTPException(503, "bus non demarre")
    return worker


class SpeedBody(BaseModel):
    rpm: int = Field(ge=0, le=1380)


class ArmBody(BaseModel):
    motor_uncoupled: bool


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
            "fault_code": s.fault_code,
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
            "age_s": round(time.time() - s.updated_at, 2),
            "max_rpm": w.max_rpm,
        }
    )


@app.post("/api/arm")
async def api_arm(body: ArmBody) -> JSONResponse:
    w = need_worker()
    w.snap.armed = bool(body.motor_uncoupled)
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
async def api_speed(body: SpeedBody) -> JSONResponse:
    w = need_worker()
    if not w.snap.armed:
        raise HTTPException(409, "console non armee")
    w.submit(Command("speed", body.rpm))
    return JSONResponse({"queued": "speed", "rpm": body.rpm})


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
 <div class=c><div class=k>D&eacute;faut LFT</div><div class=v id=flt>-</div></div>
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
 <button class=go id=en disabled>1. Activer (6&rarr;7&rarr;15)</button>
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
  set('flt', s.fault_code===null?null:(s.fault_code===0?'aucun':s.fault_code));
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
    ap.add_argument("--port", default=os.getenv("MOTOR_PORT", "COM3"))
    ap.add_argument("--slave", type=int, default=int(os.getenv("MOTOR_SLAVE_ID", "248")))
    ap.add_argument("--baud", type=int, default=int(os.getenv("MODBUS_BAUDRATE", "19200")))
    ap.add_argument("--parity", default=os.getenv("MODBUS_PARITY", "E"))
    ap.add_argument("--max-rpm", type=int, default=int(os.getenv("MOTOR_MAX_RPM", "300")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--http-port", type=int, default=8123)
    args = ap.parse_args()

    worker = BusWorker(args.port, args.slave, args.baud, args.parity, args.max_rpm)
    worker.start()
    print(f"bus: {args.port} @ {args.baud} 8{args.parity}1, esclave {args.slave}")
    print(f"plaque: {NOMINAL_RPM} tr/min a {BASE_HZ} Hz, {NAMEPLATE_CURRENT_A} A, i={GEAR_RATIO}")
    print(f"rejet des lectures > {MAX_PLAUSIBLE_CURRENT_A} A ou > {MAX_PLAUSIBLE_RPM} tr/min")
    time.sleep(1.5)
    s = worker.snap
    if s.link_ok and s.status_word is not None:
        print(f"variateur OK: {s.drive_state}, ETA=0x{s.status_word:04X}, HSP={s.hsp_hz} Hz")
    else:
        print(f"liaison NON etablie: {s.last_error}")
        print("La console demarre quand meme et affichera l'etat reel de la liaison.")
    print(f"\n  ->  http://{args.host}:{args.http_port}\n")
    print("Rien ne tourne avant que VOUS armiez et cliquiez.")
    try:
        uvicorn.run(app, host=args.host, port=args.http_port, log_level="warning")
    finally:
        worker.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
