"""Manual bench console for the ATV320. The OPERATOR drives it, not the software.

    .venv\\Scripts\\python.exe scripts\\bench_console.py
    -> http://127.0.0.1:8123

Why this exists as a standalone script rather than part of the application:

* It must run on the HOST. Docker Desktop on Windows cannot pass a COM port
  into a container, so nothing inside Docker can ever reach COM3.
* It talks to pymodbus directly instead of importing src/motor/atv320.py,
  because that module is being rewritten right now to fix verified stop-path
  defects. A bench tool should not break because application code is mid-edit.
* It commands NOTHING on its own. Every motion comes from a click. The polling
  loop is read-only until the operator explicitly arms the console.

SCOPE, and this is a limit rather than a disclaimer: this console is for a
BARE, UNCOUPLED motor. It is not for a loaded centrifuge and never for one
with a person in it. The application's own control path, with its safety
supervisor, ramp-stop sequencing and emergency handling, is what that requires.

Everything below is measured on this bench, not assumed:
  address 248   Schneider's point-to-point access address; the drive answers
                on it whatever its configured Add is. Address 1 did NOT answer.
  19200 8E1     factory serial format.
  offset 0      logical register addresses used as-is. At -1 the same reads
                return incoherent values (ETA and ACC both 0x8000).
  HSP @ 3104    reads 500 = 50.0 Hz, matching the keypad.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import math
import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from pymodbus.client import ModbusSerialClient

# --- Register map, offset 0 ----------------------------------------------
CMD = 8501  # write: CiA402 command word
LFRD = 8602  # write: speed setpoint, SIGNED rpm
ETA = 3201  # read: status word
RFRD = 8604  # read: output speed, SIGNED rpm
LCR = 3204  # read: motor current, 0.1 A per count
LFT = 7121  # read: last fault
HSP = 3104  # read: high speed, 0.1 Hz per count

# --- CiA402 command words ------------------------------------------------
W_SHUTDOWN = 6
W_SWITCH_ON = 7
W_ENABLE = 15
W_FAULT_RESET = 128

# --- ETA masks -----------------------------------------------------------
FAULT_BIT = 0x08
STATE_MASK = 0x6F
RUNNING = 0x27
SWITCHED_ON = 0x23
READY = 0x21
SOD_MASK = 0x4F
SOD = 0x40

# --- Machine constants (SEW KA37 DRS71S4 + gearbox) ---------------------
NOMINAL_RPM = 1380
BASE_HZ = 50.0
GEAR_RATIO = 49.79
GRAVITY = 9.80665

#: Keepalive period. Rewriting the setpoint regularly means that if this
#: console dies, writes stop and the drive's own ttO timeout stops the motor.
#: That watchdog is deliberately outside this process.
KEEPALIVE_S = 0.5
POLL_S = 0.4


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


def to_register(value: int) -> int:
    return value & 0xFFFF


@dataclass
class State:
    """Last known drive state, plus what the operator has authorised."""

    connected: bool = False
    armed: bool = False
    enabled: bool = False
    setpoint_rpm: int = 0
    status_word: int | None = None
    drive_state: str = "?"
    output_rpm: int | None = None
    current_a: float | None = None
    fault_code: int | None = None
    hsp_hz: float | None = None
    last_error: str | None = None
    last_action: str = "-"
    updated_at: float = field(default_factory=time.time)

    def derived(self) -> dict[str, float | None]:
        rpm = self.output_rpm
        if rpm is None:
            return {"output_shaft_rpm": None, "hz": None, "g_at_1m": None}
        out = rpm / GEAR_RATIO
        omega = 2.0 * math.pi * out / 60.0
        return {
            "output_shaft_rpm": round(out, 2),
            "hz": round(rpm / NOMINAL_RPM * BASE_HZ, 2),
            "g_at_1m": round(omega * omega * 1.0 / GRAVITY, 4),
        }


class Drive:
    """Thin synchronous Modbus wrapper. All calls run in an executor."""

    def __init__(self, port: str, slave: int, baud: int, parity: str, max_rpm: int) -> None:
        self.port = port
        self.slave = slave
        self.max_rpm = max_rpm
        self._client = ModbusSerialClient(
            port=port,
            baudrate=baud,
            bytesize=8,
            parity=parity,
            stopbits=1,
            timeout=0.6,
            retries=1,
        )
        # RS-485 is half duplex: exactly one transaction may be in flight, and
        # the keepalive task and the operator's clicks both want the bus.
        self._lock = threading.Lock()
        self.state = State()

    def connect(self) -> bool:
        with self._lock:
            ok = self._client.connect()
        self.state.connected = ok
        return ok

    def close(self) -> None:
        with self._lock, contextlib.suppress(Exception):
            self._client.close()

    def _read(self, addr: int) -> int | None:
        with self._lock:
            try:
                rr = self._client.read_holding_registers(address=addr, count=1, slave=self.slave)
            except Exception as exc:
                self.state.last_error = f"read {addr}: {type(exc).__name__}"
                return None
        if rr.isError():
            self.state.last_error = f"read {addr}: no response"
            return None
        return int(rr.registers[0])

    def write_register(self, addr: int, value: int) -> bool:
        with self._lock:
            try:
                rr = self._client.write_register(address=addr, value=value, slave=self.slave)
            except Exception as exc:
                self.state.last_error = f"write {addr}: {type(exc).__name__}"
                return False
        if rr.isError():
            self.state.last_error = f"write {addr}: rejected"
            return False
        return True

    def poll(self) -> State:
        word = self._read(ETA)
        if word is None:
            self.state.connected = False
            self.state.updated_at = time.time()
            return self.state
        self.state.connected = True
        self.state.last_error = None
        self.state.status_word = word
        self.state.drive_state = decode_state(word)
        self.state.enabled = self.state.drive_state == "OPERATION_ENABLED"
        rfrd = self._read(RFRD)
        self.state.output_rpm = signed(rfrd) if rfrd is not None else None
        lcr = self._read(LCR)
        self.state.current_a = round(lcr / 10.0, 1) if lcr is not None else None
        self.state.fault_code = self._read(LFT)
        if self.state.hsp_hz is None:
            hsp = self._read(HSP)
            if hsp is not None:
                self.state.hsp_hz = hsp / 10.0
        self.state.updated_at = time.time()
        return self.state

    # --- operator actions ------------------------------------------------

    def enable(self) -> str:
        """CiA402 6 -> 7 -> 15, verifying the status word between steps."""
        for word, expect in (
            (W_SHUTDOWN, None),
            (W_SWITCH_ON, None),
            (W_ENABLE, "OPERATION_ENABLED"),
        ):
            if not self.write_register(CMD, word):
                return f"echec ecriture CMD={word}"
            time.sleep(0.15)
            got = self._read(ETA)
            if got is None:
                return f"pas de reponse apres CMD={word}"
            if got & FAULT_BIT:
                return f"defaut present (ETA=0x{got:04X}) - acquittez d'abord"
            if expect and decode_state(got) != expect:
                return f"CMD={word} n'a pas mene a {expect} (ETA=0x{got:04X})"
        self.state.enabled = True
        return "ok"

    def set_speed(self, rpm: int) -> str:
        rpm = max(0, min(self.max_rpm, rpm))
        if not self.write_register(LFRD, to_register(rpm)):
            return "echec ecriture LFRD"
        self.state.setpoint_rpm = rpm
        # Write-verify: a wrong register offset would otherwise put a speed
        # into some other live Altivar parameter without anyone noticing.
        echo = self._read(LFRD)
        if echo is not None and signed(echo) != rpm:
            return f"relecture LFRD = {signed(echo)} au lieu de {rpm}"
        return "ok"

    def stop(self) -> str:
        """Zero the reference, let the drive ramp, THEN drop the output stage.

        Writing 6 straight from OPERATION_ENABLED is CiA402 transition 8: it
        removes torque at speed and freewheels. 7 first (transition 5) ramps on
        the drive's own dEC.
        """
        self.write_register(LFRD, 0)
        self.state.setpoint_rpm = 0
        if not self.write_register(CMD, W_SWITCH_ON):
            return "echec ecriture CMD=7"
        deadline = time.time() + 20.0
        while time.time() < deadline:
            rfrd = self._read(RFRD)
            if rfrd is not None and abs(signed(rfrd)) <= 5:
                break
            time.sleep(0.2)
        self.write_register(CMD, W_SHUTDOWN)
        self.state.enabled = False
        return "ok"

    def estop(self) -> str:
        """Fastest stop actually available: zero the reference, keep the ramp.

        Deliberately does NOT remove the run command. Removing it would drop
        the output stage and coast, which the drive's energy budget makes worse
        than letting its own ramp finish.
        """
        self.write_register(LFRD, 0)
        self.state.setpoint_rpm = 0
        self.write_register(CMD, W_SWITCH_ON)
        self.state.armed = False
        return "ok"

    def fault_reset(self) -> str:
        if not self.write_register(CMD, W_FAULT_RESET):
            return "echec ecriture CMD=128"
        time.sleep(0.2)
        self.write_register(CMD, W_SHUTDOWN)
        return "ok"


# --- HTTP layer ----------------------------------------------------------

drive: Drive | None = None


class SpeedBody(BaseModel):
    rpm: int = Field(ge=0, le=1380)


class ArmBody(BaseModel):
    motor_uncoupled: bool


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Start the keepalive alongside the app, and cancel it on shutdown."""
    task = asyncio.create_task(keepalive())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="AnHeart bench console", lifespan=lifespan)


def need_drive() -> Drive:
    if drive is None:
        raise HTTPException(503, "driver non initialise")
    return drive


async def in_thread[T](fn: Callable[..., T], *a: object) -> T:
    """pymodbus blocks; never run it on the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *a))


@app.get("/api/state")
async def api_state() -> JSONResponse:
    d = need_drive()
    st = await in_thread(d.poll)
    payload = asdict(st)
    payload["derived"] = st.derived()
    payload["max_rpm"] = d.max_rpm
    payload["age_s"] = round(time.time() - st.updated_at, 2)
    return JSONResponse(payload)


@app.post("/api/arm")
async def api_arm(body: ArmBody) -> JSONResponse:
    d = need_drive()
    if not body.motor_uncoupled:
        d.state.armed = False
        return JSONResponse({"armed": False, "detail": "attestation requise"})
    d.state.armed = True
    d.state.last_action = "arme par l'operateur"
    return JSONResponse({"armed": True})


@app.post("/api/enable")
async def api_enable() -> JSONResponse:
    d = need_drive()
    if not d.state.armed:
        raise HTTPException(409, "console non armee")
    result = await in_thread(d.enable)
    d.state.last_action = f"enable: {result}"
    return JSONResponse({"result": result})


@app.post("/api/speed")
async def api_speed(body: SpeedBody) -> JSONResponse:
    d = need_drive()
    if not d.state.armed:
        raise HTTPException(409, "console non armee")
    result = await in_thread(d.set_speed, body.rpm)
    d.state.last_action = f"consigne {body.rpm} tr/min: {result}"
    return JSONResponse({"result": result, "setpoint_rpm": d.state.setpoint_rpm})


@app.post("/api/stop")
async def api_stop() -> JSONResponse:
    d = need_drive()
    result = await in_thread(d.stop)
    d.state.last_action = f"stop: {result}"
    return JSONResponse({"result": result})


@app.post("/api/estop")
async def api_estop() -> JSONResponse:
    d = need_drive()
    result = await in_thread(d.estop)
    d.state.last_action = "ARRET D'URGENCE"
    return JSONResponse({"result": result})


@app.post("/api/fault-reset")
async def api_fault_reset() -> JSONResponse:
    d = need_drive()
    result = await in_thread(d.fault_reset)
    d.state.last_action = f"acquittement defaut: {result}"
    return JSONResponse({"result": result})


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(PAGE)


async def keepalive() -> None:
    """Rewrite the setpoint while armed and enabled.

    Two purposes. It keeps the drive's ttO timer fed, and it means that if this
    console stops, writes stop and the drive stops the motor by itself.
    """
    while True:
        await asyncio.sleep(KEEPALIVE_S)
        d = drive
        if d is not None and d.state.armed and d.state.enabled:
            with contextlib.suppress(Exception):
                await in_thread(d.write_register, LFRD, to_register(d.state.setpoint_rpm))


PAGE = """
<!doctype html><meta charset=utf-8><title>Banc ATV320</title>
<style>
 body{background:#12141a;color:#e7e9ee;font:15px/1.5 system-ui,sans-serif;margin:0;padding:18px}
 h1{font-size:17px;margin:0 0 14px;color:#9aa4b2;font-weight:600}
 .warn{background:#3a2409;border:1px solid #7a5210;color:#ffd28a;padding:10px 12px;border-radius:8px;margin-bottom:14px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
 .c{background:#1b1f27;border:1px solid #2a303c;border-radius:8px;padding:10px 12px}
 .k{font-size:11px;color:#8b94a3;text-transform:uppercase;letter-spacing:.5px}
 .v{font-size:22px;font-variant-numeric:tabular-nums;margin-top:2px}
 .stale{opacity:.4;text-decoration:line-through}
 button{font:600 15px system-ui;border:0;border-radius:8px;padding:12px 18px;cursor:pointer;margin:0 8px 8px 0}
 .go{background:#1f6f43;color:#fff}.go:disabled{background:#2a303c;color:#6b7280;cursor:not-allowed}
 .stop{background:#8a5a12;color:#fff}
 .estop{background:#a11d1d;color:#fff;font-size:19px;padding:20px 30px}
 input[type=range]{width:100%}
 .arm{background:#1b1f27;border:1px solid #2a303c;border-radius:8px;padding:12px;margin-bottom:14px}
 code{color:#8fb8ff}
</style>
<h1>Banc ATV320 &mdash; pilotage manuel</h1>
<div class=warn><b>Moteur d&eacute;saccoupl&eacute; uniquement.</b> Cette console est faite pour un
moteur nu sur un banc. Pas pour une centrifugeuse charg&eacute;e, et jamais avec une personne dedans.</div>

<div class=arm>
 <label><input type=checkbox id=unc> Je confirme que le moteur est <b>d&eacute;saccoupl&eacute;</b> et la zone d&eacute;gag&eacute;e</label>
 <div style="margin-top:10px"><button class=go id=arm disabled>Armer la console</button>
 <span id=armstate style="color:#8b94a3"></span></div>
</div>

<div class=grid>
 <div class=c><div class=k>&Eacute;tat variateur</div><div class=v id=st>-</div></div>
 <div class=c><div class=k>Vitesse mesur&eacute;e</div><div class=v id=rpm>-</div></div>
 <div class=c><div class=k>Arbre de sortie</div><div class=v id=out>-</div></div>
 <div class=c><div class=k>Fr&eacute;quence</div><div class=v id=hz>-</div></div>
 <div class=c><div class=k>Courant</div><div class=v id=amp>-</div></div>
 <div class=c><div class=k>D&eacute;faut (LFT)</div><div class=v id=flt>-</div></div>
 <div class=c><div class=k>ETA</div><div class=v id=eta>-</div></div>
 <div class=c><div class=k>Consigne</div><div class=v id=sp>-</div></div>
</div>

<div class=c style="margin-bottom:14px">
 <div class=k>Consigne de vitesse &mdash; <span id=maxr></span></div>
 <input type=range id=sl min=0 max=300 step=5 value=0 disabled>
 <div><span class=v id=slv>0</span> tr/min moteur</div>
 <button class=go id=send disabled>Envoyer la consigne</button>
</div>

<div>
 <button class=go id=en disabled>1. Activer (CiA402 6&rarr;7&rarr;15)</button>
 <button class=stop id=stp>2. Arr&ecirc;t en rampe</button>
 <button class=go id=fr>Acquitter d&eacute;faut</button>
</div>
<div style="margin-top:18px"><button class=estop id=es>ARR&Ecirc;T D'URGENCE</button></div>
<p style="color:#8b94a3" id=act>-</p>

<script>
const $=i=>document.getElementById(i);
let armed=false;
$('unc').onchange=()=>{$('arm').disabled=!$('unc').checked};
$('arm').onclick=async()=>{
  const r=await(await fetch('/api/arm',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({motor_uncoupled:$('unc').checked})})).json();
  armed=r.armed; $('armstate').textContent=armed?'console ARMEE':'non armee';
  $('en').disabled=!armed; $('sl').disabled=!armed; $('send').disabled=!armed;
};
$('sl').oninput=()=>$('slv').textContent=$('sl').value;
$('send').onclick=async()=>{await fetch('/api/speed',{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({rpm:+$('sl').value})})};
$('en').onclick =()=>fetch('/api/enable',{method:'POST'});
$('stp').onclick=()=>fetch('/api/stop',{method:'POST'});
$('es').onclick =()=>{fetch('/api/estop',{method:'POST'});armed=false;$('armstate').textContent='desarme par arret d urgence';
  $('en').disabled=true;$('sl').disabled=true;$('send').disabled=true;$('unc').checked=false;$('arm').disabled=true;};
$('fr').onclick =()=>fetch('/api/fault-reset',{method:'POST'});

async function tick(){
 try{
  const s=await(await fetch('/api/state')).json();
  const stale=!s.connected||s.age_s>3;
  const set=(id,val)=>{const e=$(id);e.textContent=val;e.className='v'+(stale?' stale':'')};
  set('st', s.connected? s.drive_state : 'HORS LIGNE');
  set('rpm', s.output_rpm==null?'-':s.output_rpm+' tr/min');
  set('out', s.derived.output_shaft_rpm==null?'-':s.derived.output_shaft_rpm+' tr/min');
  set('hz',  s.derived.hz==null?'-':s.derived.hz+' Hz');
  set('amp', s.current_a==null?'-':s.current_a+' A');
  set('flt', s.fault_code==null?'-':(s.fault_code===0?'aucun':s.fault_code));
  set('eta', s.status_word==null?'-':'0x'+s.status_word.toString(16).padStart(4,'0'));
  set('sp',  s.setpoint_rpm+' tr/min');
  $('maxr').textContent='plafond logiciel '+s.max_rpm+' tr/min';
  $('sl').max=s.max_rpm;
  $('act').textContent=(s.last_error?('erreur: '+s.last_error+' | '):'')+'derniere action: '+s.last_action;
 }catch(e){ $('st').textContent='CONSOLE INJOIGNABLE'; }
}
setInterval(tick,500); tick();
</script>
"""


def main() -> int:
    global drive  # noqa: PLW0603
    ap = argparse.ArgumentParser(description="Console manuelle ATV320")
    ap.add_argument("--port", default=os.getenv("MOTOR_PORT", "COM3"))
    ap.add_argument("--slave", type=int, default=int(os.getenv("MOTOR_SLAVE_ID", "248")))
    ap.add_argument("--baud", type=int, default=int(os.getenv("MODBUS_BAUDRATE", "19200")))
    ap.add_argument("--parity", default=os.getenv("MODBUS_PARITY", "E"))
    ap.add_argument("--max-rpm", type=int, default=int(os.getenv("MOTOR_MAX_RPM", "300")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--http-port", type=int, default=8123)
    args = ap.parse_args()

    drive = Drive(args.port, args.slave, args.baud, args.parity, args.max_rpm)
    print(f"ouverture {args.port} @ {args.baud} 8{args.parity}1, esclave {args.slave}")
    if not drive.connect():
        print("ECHEC: le port ne s'ouvre pas. SoMove le tient-il encore ?")
        return 1
    st = drive.poll()
    if not st.connected:
        print("port ouvert mais le variateur ne repond pas.")
    else:
        print(
            f"variateur OK: {st.drive_state}, ETA=0x{st.status_word:04X}, "
            f"HSP={st.hsp_hz} Hz, defaut={st.fault_code}"
        )
    print(f"\n  ->  http://{args.host}:{args.http_port}\n")
    print("Rien ne tournera avant que VOUS armiez la console et cliquiez.")
    uvicorn.run(app, host=args.host, port=args.http_port, log_level="warning")
    drive.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
