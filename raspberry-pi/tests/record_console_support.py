"""The REAL console, recording: the failure rig of ``test_failure_rig`` plus a session journal.

The journal runs WITHOUT its thread here: the test calls :meth:`RecordedRig.drain`
when it wants the disk to catch up, so what is on disk at any line of a test is
exact. The thread has its own tests (``test_record_journal``,
``test_record_tick_isolation``, ``test_record_abrupt_stop``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from src.clock import ManualClock
from src.control_surface import EventKind
from src.ecg_pipeline import TreatFunction
from src.local_panel import CloudTransportFactory
from src.record.journal import Journal
from src.record.reader import Recording, read
from src.record.session import SessionRecorder
from src.result import Ok
from src.training.runtime import RuntimeState
from src.units import Monotonic, UnixMillis
from tests.test_failure_rig import BENCH_ENV, OPERATOR, TICK, Rig, Wrapped, attest, make_rig


@dataclass
class RecordedRig:
    """The rig, its journal, and the records directory."""

    rig: Rig
    journal: Journal
    clock: ManualClock

    @property
    def root(self) -> Path:
        return self.journal.root

    @property
    def recorder(self) -> SessionRecorder:
        recorder = self.rig.panel.recorder
        if recorder is None:
            raise AssertionError("the console was built without a recorder")
        return recorder

    async def tick(self, seconds: float, *, drain: bool = True) -> None:
        """Tick the console as its loop does (sensors once a second), then let the disk catch up."""
        for index in range(max(1, round(seconds / TICK))):
            await self.rig.tick(float(TICK))
            if index % 5 == 0:
                await self.rig.panel.sensor_step()
            if drain:
                self.journal.drain()

    def drain(self) -> None:
        self.journal.drain()

    def degraded(self) -> bool:
        """Read through a call: a checker keeps a property's narrowing across an ``await``."""
        return self.recorder.degraded

    def state(self) -> RuntimeState:
        """The runtime's state, read afresh for the same reason."""
        return self.rig.panel.runtime.state

    def records(self) -> list[Path]:
        """Every record directory, oldest first (the names start with the UTC start)."""
        return sorted(path for path in self.root.iterdir() if not path.name.startswith("."))

    def only_record(self) -> Path:
        records = self.records()
        if len(records) != 1:
            raise AssertionError(f"expected one record, found {[p.name for p in records]}")
        return records[0]

    def recording(self) -> Recording:
        loaded = read(self.only_record())
        if isinstance(loaded, Ok):
            return loaded.value
        raise AssertionError(f"the record cannot be read: {loaded.error}")

    def said(self) -> list[str]:
        """What the console told the operator about the record."""
        return [e.detail for e in self.rig.events if e.kind is EventKind.RECORDING]

    def everything_on_disk(self) -> bytes:
        return b"".join(
            path.read_bytes() for path in sorted(self.root.rglob("*")) if path.is_file()
        ) + "\n".join(path.name for path in self.root.rglob("*")).encode("utf-8")


def recorded_rig(
    tmp_path: Path,
    *,
    env: Mapping[str, str] = BENCH_ENV,
    wrap: type[Wrapped] | None = None,
    treat: TreatFunction | None = None,
    dsp: bool = False,
    transport: CloudTransportFactory | None = None,
) -> RecordedRig:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock)
    rig, _ = make_rig(
        tmp_path,
        env=env,
        wrap=wrap,
        treat=treat,
        dsp=dsp,
        clock=clock,
        transport=transport,
        journal=journal,
    )
    return RecordedRig(rig=rig, journal=journal, clock=clock)


async def start_bench(recorded: RecordedRig, session: httpx.AsyncClient) -> None:
    """Attest and arm a bench manual session; the record opens on the next tick."""
    await attest(session)
    started = await session.post(
        "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
    )
    if started.status_code != 202:
        raise AssertionError(started.text)
    await recorded.tick(0.2)


async def set_target(session: httpx.AsyncClient, output_rpm: float) -> None:
    target = await session.post(
        "/api/manual/target", json={"output_rpm": output_rpm, "operator": OPERATOR}
    )
    if target.status_code != 202:
        raise AssertionError(target.text)


async def stop(recorded: RecordedRig, session: httpx.AsyncClient, seconds: float = 30.0) -> None:
    """The operator's STOP, then enough ticks for the runtime to finish."""
    ended = await session.post(
        "/api/session/stop", json={"operator": OPERATOR, "reason": "operator pressed STOP"}
    )
    if ended.status_code != 202:
        raise AssertionError(ended.text)
    await recorded.tick(seconds)
