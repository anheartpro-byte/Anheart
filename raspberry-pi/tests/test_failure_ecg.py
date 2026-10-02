"""BITalino / ECG failures injected into the REAL console, during a programme.

A programme is the only session where the heart rate drives the speed, so every
case here runs one (the short ``failure_short`` profile of the rig, on the
occupied path with programmes enabled) and injects the failure at a chosen
phase. What must follow, every time: no fresh heart rate means ``hr_stale``
(FREEZE at 10 s, REDUCE at 30 s, RAMP_DOWN at 60 s), the session ends on a
``safety_verdict``, the speed never rises while the evidence is missing, the
shaft is left at 0 with the output stage off, and the operator is shown the
rule with its explanation.

Two ECG paths, chosen per case:

* **link-level failures** (disconnect, connect refused, frames that stop) do
  not depend on what the samples contain, so the DSP is replaced by a
  ground-truth reader (the rig's heart rate, one fresh reading a second) to
  reach the injection point in a fraction of the CPU;
* **signal-content failures** (flat line, saturation, 50 Hz mains, NaN or
  garbage values, gapped samples) run the REAL ``SignalTreatment`` DSP inline
  from 12 s before the injection, because the question is precisely whether
  the DSP grades them correctly. Every reading, the ground truth's included,
  then goes through the bridge's independent confirmation (the typed
  ``src/sensors/ecg.py`` processor on the same unbroken window), which is what
  keeps a rate BioSPPy reads from garbage or from a gapped stream away from
  the runtime.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Final

import pytest

from src.bitalino_client import ChannelData, SampleBatch
from src.ecg_pipeline import ECG_CHANNEL, EcgFrame, EcgMetrics, Treatment, treat_ecg
from src.local_panel import LocalPanel
from src.sim.bitalino import SimulatedBitalinoClient
from src.sim.physiology import Physiology, SubjectState
from src.training.runtime import EndReason, RuntimeState
from src.training.types import Phase, SafetyAction, SignalQuality
from src.units import Monotonic, MotorRpm
from tests.test_failure_rig import PROGRAMME_ENV, Rig, make_rig, start_programme

BATCHES_PER_READING: Final[int] = 5
"""The truth reader hands out one fresh reading per second (five 200 ms batches)."""

PHASE_AT: Final[dict[Phase, float]] = {
    Phase.BASELINE: 10.0,
    Phase.WARMUP: 80.0,
    Phase.HOLD: 200.0,
    Phase.COOLDOWN: 300.0,
    Phase.RECOVERY: 360.0,
}
"""Seconds after the start at which each phase of ``failure_short`` is under way."""

FALSE_RATE_BPM: Final[int] = 20
"""A shown rate this far from the subject's true rate is a fabricated one."""

ADC_MID: Final[float] = 512.0
ADC_MAX: Final[float] = 1023.0


@dataclass
class Truth:
    """The last state the console's simulated subject was integrated to."""

    state: SubjectState | None = None


class Dsp:
    """The treat function: ground truth until ``real_from``, the REAL DSP after.

    Mutable, owned by the one test. ``real_from`` is a batch count; ``None``
    keeps the ground-truth reader for the whole run.
    """

    def __init__(self, truth: Truth) -> None:
        self.truth: Truth = truth
        self.real: bool = False
        self.batches: int = 0
        self.seq: int = 0

    async def __call__(self, treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
        self.batches += 1
        if self.real:
            frame = treat_ecg(treatment, batch)
            if frame is None:
                return None
            # The DSP numbers its own results from 1; carried past the truth
            # reader's last seq, or the runtime would rightly ignore them as old.
            metrics = replace(frame.metrics, seq=frame.metrics.seq + self.seq)
            return replace(frame, metrics=metrics)
        state = self.truth.state
        if state is None or self.batches % BATCHES_PER_READING != 0:
            return EcgFrame(
                millivolts=(),
                metrics=EcgMetrics(seq=self.seq, quality=SignalQuality.GOOD, bpm=None),
            )
        self.seq += 1
        return EcgFrame(
            millivolts=(),
            metrics=EcgMetrics(seq=self.seq, quality=SignalQuality.GOOD, bpm=state.heart_rate),
        )


type Corruption = Callable[[Sequence[float], int], list[float]]
"""Rewrites one batch's ECG column; the int is the index of the batch since injection."""


@dataclass
class Feed:
    """What the monkeypatched ``read_samples`` does to the samples."""

    silent: bool = False
    gapped: bool = False
    lost: int = 0
    corrupt: Corruption | None = None
    since: int = 0


@dataclass
class Programme:
    rig: Rig
    dsp: Dsp
    feed: Feed

    @property
    def panel(self) -> LocalPanel:
        return self.rig.panel

    @property
    def client(self) -> SimulatedBitalinoClient:
        client = self.panel.ecg.client
        if not isinstance(client, SimulatedBitalinoClient):
            raise TypeError("the console is not on the simulated BITalino")
        return client


def programme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Programme:
    """The console on the short programme, the ECG hooks in place."""
    truth = Truth()
    original_advance = Physiology.advance

    def advance(self: Physiology, now: Monotonic, motor_rpm: MotorRpm) -> SubjectState:
        state = original_advance(self, now, motor_rpm)
        truth.state = state
        return state

    monkeypatch.setattr(Physiology, "advance", advance)
    dsp = Dsp(truth)
    feed = Feed()
    original_read = SimulatedBitalinoClient.read_samples

    async def read_samples(self: SimulatedBitalinoClient, count: int = 1000) -> SampleBatch | None:
        if feed.silent:
            return None
        batch = await original_read(self, count)
        if batch is not None and feed.gapped:
            # Every other block LOST on the air: read, then discarded. (Reading the
            # next block in its place, as this once did, found none ready at the
            # rig's 200 ms step and so lost EVERY block: a silent stream, not gaps.)
            feed.lost += 1
            if feed.lost % 2:
                return None
        corrupt = feed.corrupt
        if batch is None or corrupt is None:
            return batch
        feed.since += 1
        channels = [
            ChannelData(channel=ch.channel, values=corrupt(ch.values, feed.since))
            if ch.channel == ECG_CHANNEL
            else ch
            for ch in batch.channels
        ]
        return SampleBatch(timestamp=batch.timestamp, channels=channels)

    monkeypatch.setattr(SimulatedBitalinoClient, "read_samples", read_samples)
    rig, _ = make_rig(tmp_path, env=PROGRAMME_ENV, treat=dsp)
    return Programme(rig=rig, dsp=dsp, feed=feed)


async def judged_end(run: Programme, *, injected_at: float) -> None:
    """Every assertion a lost heart rate must satisfy, from the injection on."""
    rig = run.rig
    runtime = rig.panel.runtime
    await rig.tick(90.0)
    after = [s for s in rig.snapshots if float(s.elapsed) >= injected_at]
    truth = run.dsp.truth.state
    assert truth is not None
    false_rates = [
        (int(s.live_bpm), round(float(s.elapsed), 1))
        for s in after
        if float(s.elapsed) >= injected_at + 3.0
        and s.live_bpm is not None
        and abs(int(s.live_bpm) - int(truth.heart_rate)) > FALSE_RATE_BPM
    ]
    assert not false_rates, f"false GOOD rates (true ~{truth.heart_rate}): {false_rates[:6]}"
    rules = {s.safety.rule for s in after if s.safety is not None}
    assert "hr_stale" in rules, rules
    # While the evidence is missing, the speed never rises (FREEZE or above stands).
    for before, now in pairwise(after):
        if now.safety is not None and now.safety.action >= SafetyAction.FREEZE:
            assert now.setpoint.motor_rpm <= before.setpoint.motor_rpm, (before, now)
    assert runtime.end_reason is EndReason.SAFETY_VERDICT, runtime.end_reason
    assert runtime.state in {RuntimeState.ENDING, RuntimeState.FINISHED}
    stale = next(s.safety for s in after if s.safety is not None and s.safety.rule == "hr_stale")
    assert stale.detail.strip(), "the operator is told nothing about the heart rate"
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def run_to(run: Programme, at: float, *, real_dsp: bool) -> None:
    rig = run.rig
    async with rig.http() as session:
        await rig.tick(10.0)
        await start_programme(session)
    lead = 12.0 if real_dsp else 0.0
    await rig.tick(at - lead)
    run.dsp.real = real_dsp
    if lead:
        await rig.tick(lead)


def turning(run: Programme) -> int:
    return int(run.panel.runtime.snapshot().measured.motor_rpm)


# =========================================================================
# Link-level: disconnect at every phase, connect refused, frames stopping
# =========================================================================


@pytest.mark.parametrize("phase", list(PHASE_AT), ids=[p.value for p in PHASE_AT])
async def test_a_permanent_disconnect_at_every_phase_ends_on_hr_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: Phase
) -> None:
    run = programme(tmp_path, monkeypatch)
    at = PHASE_AT[phase]
    await run_to(run, at, real_dsp=False)
    assert run.panel.runtime.snapshot().phase is phase
    run.client.inject_connect_failure()
    await run.client.inject_disconnect()
    await run.rig.tick(5.0)
    status = run.rig.panel.reporter.panel_status().ecg
    assert not status.acquiring
    assert status.last_error is not None, "the operator is not told the BITalino is gone"
    if phase in {Phase.COOLDOWN, Phase.RECOVERY}:
        # Already ending on the programme's own terms: the loss must not stop the
        # ending, and the programme may still complete; nothing may speed up.
        await run.rig.tick(90.0)
        assert run.panel.runtime.state in {RuntimeState.ENDING, RuntimeState.FINISHED}
        await run.rig.panel.close()
        assert await run.rig.left_stopped() == ""
        return
    await judged_end(run, injected_at=at)


async def test_a_transient_disconnect_reconnects_and_the_session_carries_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = programme(tmp_path, monkeypatch)
    await run_to(run, PHASE_AT[Phase.WARMUP], real_dsp=False)
    await run.client.inject_disconnect()
    await run.rig.tick(8.0)
    status = run.rig.panel.reporter.panel_status().ecg
    assert status.acquiring, "the link was not re-established"
    assert status.connect_attempts >= 1
    await run.rig.tick(20.0)
    runtime = run.panel.runtime
    assert runtime.state is RuntimeState.RUNNING
    assert runtime.snapshot().live_bpm is not None
    await run.rig.panel.close()
    assert await run.rig.left_stopped() == ""


async def test_a_bitalino_that_never_connects_never_lets_the_machine_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = programme(tmp_path, monkeypatch)
    run.client.inject_connect_failure()
    await run.client.inject_disconnect()
    async with run.rig.http() as session:
        await run.rig.tick(10.0)
        await start_programme(session)
    await run.rig.tick(150.0)
    assert max(s.setpoint.motor_rpm for s in run.rig.snapshots) == 0
    status = run.rig.panel.reporter.panel_status().ecg
    assert status.last_error is not None
    assert "BITalino" in status.last_error
    rules = {s.safety.rule for s in run.rig.snapshots if s.safety is not None}
    assert "hr_stale" in rules, rules
    await run.rig.panel.close()
    assert await run.rig.left_stopped() == ""


async def test_frames_that_stop_silently_while_acquiring_end_on_hr_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = programme(tmp_path, monkeypatch)
    at = PHASE_AT[Phase.WARMUP]
    await run_to(run, at, real_dsp=False)
    assert turning(run) > 0
    run.feed.silent = True
    await judged_end(run, injected_at=at)


# =========================================================================
# Signal content, through the REAL DSP
# =========================================================================


def _flat(values: Sequence[float], _n: int) -> list[float]:
    return [ADC_MID] * len(values)


def _saturated(values: Sequence[float], _n: int) -> list[float]:
    return [ADC_MAX] * len(values)


def _mains(values: Sequence[float], n: int) -> list[float]:
    # 50 Hz at 1 kHz, 400 counts peak: the hum swamps a ~70-count R wave.
    start = n * len(values)
    return [
        min(ADC_MAX, max(0.0, ADC_MID + 400.0 * math.sin(2 * math.pi * 50.0 * (start + i) / 1000)))
        for i in range(len(values))
    ]


def _nan(values: Sequence[float], _n: int) -> list[float]:
    return [math.nan if i % 7 == 0 else v for i, v in enumerate(values)]


def _garbage(values: Sequence[float], n: int) -> list[float]:
    # A deterministic byte-salad, as corrupted frames that slipped a CRC would give.
    return [float((i * 7919 + n * 104729) % 1024) for i in range(len(values))]


CORRUPTIONS: Final[dict[str, Corruption]] = {
    "flat_line_electrodes_off": _flat,
    "saturated": _saturated,
    "mains_50hz": _mains,
    "nan_values": _nan,
    "corrupted_garbage": _garbage,
}


# ``corrupted_garbage`` was a strict xfail: the legacy grader passes a uniform
# 0..1023 byte salad as GOOD and BioSPPy reads 115-146 bpm from it (true 70-71),
# which ended the session on false hr_rate / hr_drop 'presyncope' verdicts.
# src/ecg_pipeline.py now withholds any rate the typed processor does not confirm
# on the same unbroken window, so the rate goes stale instead, as for the others.


@pytest.mark.parametrize("name", list(CORRUPTIONS))
async def test_a_signal_the_dsp_cannot_read_ends_on_hr_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    run = programme(tmp_path, monkeypatch)
    at = PHASE_AT[Phase.WARMUP]
    await run_to(run, at, real_dsp=True)
    assert run.panel.runtime.snapshot().live_bpm is not None, "the real DSP never locked on"
    run.feed.corrupt = CORRUPTIONS[name]
    await judged_end(run, injected_at=at)


async def test_gapped_samples_never_yield_a_false_good_heart_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half the samples lost: no rate may be read from windows stitched across the losses."""
    run = programme(tmp_path, monkeypatch)
    at = PHASE_AT[Phase.WARMUP]
    await run_to(run, at, real_dsp=True)
    run.feed.gapped = True
    await run.rig.tick(40.0)
    truth = run.dsp.truth.state
    assert truth is not None
    shown = [
        (int(s.live_bpm), float(s.elapsed))
        for s in run.rig.snapshots
        if float(s.elapsed) >= at + 10.0 and s.live_bpm is not None
    ]
    wrong = [(bpm, t) for bpm, t in shown if abs(bpm - int(truth.heart_rate)) > 20]
    assert not wrong, f"false GOOD heart rates shown (true ~{truth.heart_rate}): {wrong[:5]}"
    # Stronger than "not wrong": every batch that arrives follows a lost one, so
    # no window is ever unbroken and no rate is confirmed at all. The bridge
    # counted the gaps (about one per 400 ms), and the rate went stale.
    assert not shown, f"a rate from a stitched window reached the runtime: {shown[:5]}"
    assert run.panel.reporter.panel_status().ecg.bridge.gaps >= 80
    rules = {s.safety.rule for s in run.rig.snapshots if s.safety is not None}
    assert "hr_stale" in rules, rules
    await run.rig.panel.close()
    assert await run.rig.left_stopped() == ""
