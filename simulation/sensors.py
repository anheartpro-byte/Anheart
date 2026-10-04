"""The DIRECT heart-rate sensor model: ground truth, delivered the way the DSP delivers it.

For long sessions the real BioSPPy path costs too much (about 50 ms of CPU per
simulated second), so this stands in for "simulated BITalino + SignalTreatment
+ EcgBridge" with the same OUTPUT contract, which is all the runtime sees:

* one reading every ``period`` (the DSP refreshes about once a second),
* a sequence number that advances on every fresh reading and does NOT advance
  when the DSP re-emits its previous metrics (``ecg_repeat_seq``),
* a rate only when the grade is ``GOOD`` (``src.ecg_pipeline`` never passes a
  rate with any other grade), rounded to whole bpm, inside the plausible band,
* nothing at all while the link is down or the electrodes are off.

It invents nothing the scenario did not ask for: optional Gaussian noise, the
ectopic-beat artefacts and the motion artefact (noise growing with the g-load)
all come from one seeded generator, so a run is reproducible bit for bit.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import final

from src.training.hr_control import MAX_PLAUSIBLE_BPM, MIN_PLAUSIBLE_BPM
from src.training.types import SignalQuality
from src.units import Bpm, Monotonic, Seconds


@dataclass(frozen=True, slots=True)
class Reading:
    """What the DSP hands the runtime: ``observe_ecg(now, seq, quality, bpm)``."""

    seq: int
    quality: SignalQuality
    bpm: Bpm | None


@final
class DirectSensor:
    """Mutable, owned by the harness loop. Time is handed in, never read."""

    __slots__ = (
        "_disconnected",
        "_dropout_until",
        "_ectopic_bpm",
        "_ectopic_rate",
        "_gap",
        "_last",
        "_motion_noise",
        "_next_at",
        "_noise",
        "_period",
        "_quality",
        "_quality_until",
        "_repeat_until",
        "_rng",
        "_seq",
        "_stopped",
        "_value",
        "_value_until",
    )

    def __init__(
        self,
        *,
        start: Monotonic,
        period: Seconds,
        noise_bpm: float,
        seed: int,
        ectopic_rate: float = 0.0,
        ectopic_bpm: float = 0.0,
        motion_noise_bpm_per_g: float = 0.0,
    ) -> None:
        self._period: Seconds = period
        self._noise: float = noise_bpm
        self._ectopic_rate: float = ectopic_rate
        self._ectopic_bpm: float = ectopic_bpm
        self._motion_noise: float = motion_noise_bpm_per_g
        self._gap: int = 0
        self._stopped: bool = False
        self._rng: random.Random = random.Random(seed)  # noqa: S311  # simulation noise, not crypto
        self._next_at: Monotonic = start
        self._seq: int = 0
        self._last: Reading | None = None
        self._disconnected: bool = False
        self._dropout_until: Monotonic | None = None
        self._repeat_until: Monotonic | None = None
        self._quality: SignalQuality = SignalQuality.GOOD
        self._quality_until: Monotonic | None = None
        self._value: Bpm = Bpm(0)
        self._value_until: Monotonic | None = None

    # -- scenario hooks ---------------------------------------------------

    def disconnect(self) -> None:
        """The link is gone for good: no reading ever again."""
        self._disconnected = True

    def stop_silently(self) -> None:
        """No reading ever again, while the link goes on reporting itself up."""
        self._stopped = True

    def skip(self, gap: int) -> None:
        """The next fresh reading's sequence number jumps by ``gap`` (lost frames)."""
        self._gap += gap

    def dropout(self, until: Monotonic) -> None:
        """No reading until ``until``."""
        self._dropout_until = until

    def repeat(self, until: Monotonic) -> None:
        """Re-emit the previous reading, same seq, until ``until``."""
        self._repeat_until = until

    def grade(self, quality: SignalQuality, until: Monotonic) -> None:
        """Grade every reading ``quality`` until ``until``."""
        self._quality = quality
        self._quality_until = until

    def force(self, bpm: Bpm, until: Monotonic) -> None:
        """Report ``bpm`` (GOOD) until ``until``, whatever the heart does."""
        self._value = bpm
        self._value_until = until

    @property
    def connected(self) -> bool:
        """Whether the link is up (a dropout is a gap, not a disconnect)."""
        return not self._disconnected

    # -- the reading ------------------------------------------------------

    def sample(self, now: Monotonic, truth: Bpm, g_load: float = 0.0) -> Reading | None:
        """The reading due at ``now``, or ``None`` when none is due or none can be made.

        ``g_load`` (at the reference radius) scales the motion artefact.
        """
        if self._disconnected or self._stopped or now < self._next_at:
            return None
        self._next_at = Monotonic(self._next_at + self._period)
        if self._dropout_until is not None and now < self._dropout_until:
            return None
        last = self._last
        if last is not None and self._repeat_until is not None and now < self._repeat_until:
            return last
        self._seq += 1 + self._gap
        self._gap = 0
        reading = Reading(self._seq, *self._graded(now, truth, g_load))
        self._last = reading
        return reading

    def _graded(
        self, now: Monotonic, truth: Bpm, g_load: float
    ) -> tuple[SignalQuality, Bpm | None]:
        if self._value_until is not None and now < self._value_until:
            return (SignalQuality.GOOD, self._value)
        quality = SignalQuality.GOOD
        if self._quality_until is not None and now < self._quality_until:
            quality = self._quality
        if not quality.is_trustworthy:
            return (quality, None)
        bpm = round(truth + self._artefact(g_load))
        if bpm < MIN_PLAUSIBLE_BPM or bpm > MAX_PLAUSIBLE_BPM:
            return (quality, None)
        return (quality, Bpm(bpm))

    def _artefact(self, g_load: float) -> float:
        """Measurement noise, motion artefact and ectopic beats, in bpm.

        The generator is drawn only for the features a scenario enabled, so a
        scenario without them reproduces exactly the sequence it always had.
        """
        sigma = self._noise + self._motion_noise * abs(g_load)
        offset = self._rng.gauss(0.0, sigma) if sigma > 0.0 else 0.0
        if self._ectopic_rate > 0.0 and self._rng.random() < self._ectopic_rate:
            offset += self._ectopic_bpm if self._rng.random() < 0.5 else -self._ectopic_bpm  # noqa: PLR2004  # a fair coin
        return offset
