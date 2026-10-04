"""The synthetic cohort: 30 fake subjects aged 10 to 50, generated from one seed.

    python -m simulation.cohort.generate            # rewrite simulation/cohort/cohort.json
    python -m simulation.cohort.generate --check    # exit 1 if the committed file differs

Each subject is a person-shaped set of numbers mapped onto the ONLY knobs the
production plant has (``src.sim.physiology.PhysiologyConfig``) plus the DIRECT
sensor's artefact model (``simulation.sensors``) and, for a few, a scripted
event of the plant (``ScriptedEvent``). Nothing here adds physiology: a trait
that the plant cannot express is not invented.

How the numbers are drawn (every draw from one ``random.Random(SEED)``, in a
fixed order, so the file is reproducible bit for bit):

* **age**: five children and adolescents (10 to 15, the under-16 case the
  battery checks the machine against; the first is always exactly 10), then 25
  adults (16 to 50);
* **HRmax**: Tanaka ``208 - 0.7 x age`` (the estimate ``src.training.plan``
  uses), plus an individual deviation ~N(0, 7) bpm clipped to +/- 15, because
  the estimate's own population SD is about 10 bpm;
* **resting HR** by fitness (low 78, average 68, high 58, athlete 50) ~N(0, 4),
  +3 for women, +2 per year under 16;
* **gain** ``k_g`` (bpm per g at the reference radius; the plant's default is
  110) by fitness, x N(1, 0.08); **fatigue** likewise (low 0.15 .. athlete 0);
* **time constants** ``tau_up`` ~N(30, 4) s, ``tau_down`` = 1.8 x tau_up x
  N(1, 0.1) (athletes recover 15 % faster); **drift** by fitness x N(1, 0.15)
  over ~N(600, 60) s;
* **ECG noise** |N(1, 0.4)| bpm;
* **special conditions**, eleven of them, placed by ``rng.sample``: slow and
  fast responders (the plant's tau), a non-responder (the plant's
  ``nonresponder`` event), vasovagal-prone (a ``vasovagal_drop`` in HOLD),
  ectopic beats (occasional +/- 25 bpm outliers in the reading) and high
  motion artefact (6 bpm of noise per g).
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path
from typing import Final

from simulation.jsondoc import Reader, load_object
from src.result import Err, Ok, Result
from src.sim.physiology import PhysiologyConfig
from src.training.plan import TANAKA_INTERCEPT, TANAKA_SLOPE
from src.units import Bpm, Seconds

COHORT_PATH: Final[Path] = Path(__file__).resolve().parent / "cohort.json"
SEED: Final[int] = 20_260_930
SIZE: Final[int] = 30
MINORS: Final[int] = 5
MIN_AGE: Final[int] = 10
ADULT_AGE: Final[int] = 16
MAX_AGE: Final[int] = 50
SCHEMA: Final[int] = 1

HRMAX_SD: Final[float] = 7.0
HRMAX_CLIP: Final[float] = 15.0

VASOVAGAL_SESSION_S: Final[tuple[float, float]] = (650.0, 1000.0)
"""A vasovagal-prone subject's collapse lands in this window of session time: HOLD of
both 30-minute programmes (their HOLD runs 480..1260 s)."""


@unique
class Sex(Enum):
    FEMALE = "female"
    MALE = "male"


@unique
class Fitness(Enum):
    LOW = "low"
    AVERAGE = "average"
    HIGH = "high"
    ATHLETE = "athlete"


@unique
class Condition(Enum):
    """The one special trait a subject carries, if any."""

    NONE = "none"
    SLOW_RESPONDER = "slow_responder"
    FAST_RESPONDER = "fast_responder"
    NONRESPONDER = "nonresponder"
    VASOVAGAL_PRONE = "vasovagal_prone"
    ECTOPIC = "ectopic"
    MOTION_ARTEFACT = "motion_artefact"


SPECIAL: Final[tuple[Condition, ...]] = (
    Condition.SLOW_RESPONDER,
    Condition.SLOW_RESPONDER,
    Condition.FAST_RESPONDER,
    Condition.FAST_RESPONDER,
    Condition.NONRESPONDER,
    Condition.VASOVAGAL_PRONE,
    Condition.VASOVAGAL_PRONE,
    Condition.ECTOPIC,
    Condition.ECTOPIC,
    Condition.MOTION_ARTEFACT,
    Condition.MOTION_ARTEFACT,
)

_REST: Final[Mapping[Fitness, float]] = {
    Fitness.LOW: 78.0,
    Fitness.AVERAGE: 68.0,
    Fitness.HIGH: 58.0,
    Fitness.ATHLETE: 50.0,
}
_GAIN: Final[Mapping[Fitness, float]] = {
    Fitness.LOW: 130.0,
    Fitness.AVERAGE: 110.0,
    Fitness.HIGH: 95.0,
    Fitness.ATHLETE: 85.0,
}
_FATIGUE: Final[Mapping[Fitness, float]] = {
    Fitness.LOW: 0.15,
    Fitness.AVERAGE: 0.05,
    Fitness.HIGH: 0.0,
    Fitness.ATHLETE: 0.0,
}
_DRIFT: Final[Mapping[Fitness, float]] = {
    Fitness.LOW: 14.0,
    Fitness.AVERAGE: 10.0,
    Fitness.HIGH: 7.0,
    Fitness.ATHLETE: 5.0,
}
_FITNESS_WEIGHTS: Final[tuple[tuple[Fitness, float], ...]] = (
    (Fitness.LOW, 0.25),
    (Fitness.AVERAGE, 0.40),
    (Fitness.HIGH, 0.25),
)
"""Cumulative draw; whatever is left (10 %) is ``ATHLETE``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CohortSubject:
    """One fake person, and the plant/sensor numbers that stand for them."""

    subject_id: str
    age_years: int
    sex: Sex
    fitness: Fitness
    condition: Condition
    hr_rest: Bpm
    hr_max_tanaka: Bpm
    """``208 - 0.7 x age``, rounded: what the machine would estimate from the age."""

    hr_max: Bpm
    """The subject's true maximum (the estimate plus their deviation)."""

    k_g: float
    tau_up: Seconds
    tau_down: Seconds
    drift_max: float
    tau_drift: Seconds
    fatigue: float
    ecg_noise_bpm: float
    ectopic_rate: float
    ectopic_bpm: float
    motion_noise_bpm_per_g: float
    vasovagal_at_s: float | None
    """Session time of the scripted collapse (vasovagal-prone subjects only)."""

    @property
    def minor(self) -> bool:
        """Under 16."""
        return self.age_years < ADULT_AGE

    def physiology(self) -> PhysiologyConfig:
        """The subject as the production plant knows people."""
        return PhysiologyConfig(
            hr_rest=self.hr_rest,
            hr_max=self.hr_max,
            k_g=self.k_g,
            tau_up=self.tau_up,
            tau_down=self.tau_down,
            drift_max=self.drift_max,
            tau_drift=self.tau_drift,
            fatigue=self.fatigue,
        )

    def to_json(self) -> Mapping[str, str | int | float | None]:
        return {
            "subject_id": self.subject_id,
            "age_years": self.age_years,
            "sex": self.sex.value,
            "fitness": self.fitness.value,
            "condition": self.condition.value,
            "hr_rest": int(self.hr_rest),
            "hr_max_tanaka": int(self.hr_max_tanaka),
            "hr_max": int(self.hr_max),
            "k_g": self.k_g,
            "tau_up": float(self.tau_up),
            "tau_down": float(self.tau_down),
            "drift_max": self.drift_max,
            "tau_drift": float(self.tau_drift),
            "fatigue": self.fatigue,
            "ecg_noise_bpm": self.ecg_noise_bpm,
            "ectopic_rate": self.ectopic_rate,
            "ectopic_bpm": self.ectopic_bpm,
            "motion_noise_bpm_per_g": self.motion_noise_bpm_per_g,
            "vasovagal_at_s": self.vasovagal_at_s,
        }


def tanaka(age_years: int) -> Bpm:
    """``208 - 0.7 x age``, the estimate ``src.training.plan.hr_max_from_age`` computes."""
    return Bpm(round(TANAKA_INTERCEPT - TANAKA_SLOPE * age_years))


def _r(value: float, digits: int = 3) -> float:
    return round(value, digits)


def _fitness(rng: random.Random) -> Fitness:
    draw = rng.random()
    total = 0.0
    for fitness, weight in _FITNESS_WEIGHTS:
        total += weight
        if draw < total:
            return fitness
    return Fitness.ATHLETE


def _subject(rng: random.Random, index: int, age: int, condition: Condition) -> CohortSubject:
    sex = Sex.FEMALE if rng.random() < 0.5 else Sex.MALE  # noqa: PLR2004  # a fair coin
    fitness = _fitness(rng)
    estimate = tanaka(age)
    deviation = max(-HRMAX_CLIP, min(HRMAX_CLIP, rng.gauss(0.0, HRMAX_SD)))
    hr_max = Bpm(round(estimate + deviation))
    child = 2.0 * max(0, ADULT_AGE - age)
    rest = _REST[fitness] + rng.gauss(0.0, 4.0) + (3.0 if sex is Sex.FEMALE else 0.0) + child
    k_g = _GAIN[fitness] * rng.gauss(1.0, 0.08)
    tau_up = min(45.0, max(20.0, rng.gauss(30.0, 4.0)))
    recovery = 0.85 if fitness is Fitness.ATHLETE else 1.0
    tau_down = 1.8 * tau_up * rng.gauss(1.0, 0.1) * recovery
    drift = _DRIFT[fitness] * max(0.3, rng.gauss(1.0, 0.15))
    tau_drift = rng.gauss(600.0, 60.0)
    fatigue = max(0.0, _FATIGUE[fitness] + rng.gauss(0.0, 0.02))
    noise = abs(rng.gauss(1.0, 0.4))
    vasovagal = rng.uniform(*VASOVAGAL_SESSION_S)
    match condition:
        case Condition.SLOW_RESPONDER:
            tau_up, tau_down = 60.0, 110.0
        case Condition.FAST_RESPONDER:
            tau_up, tau_down = 12.0, 25.0
        case _:
            pass
    return CohortSubject(
        subject_id=f"S{index + 1:02d}",
        age_years=age,
        sex=sex,
        fitness=fitness,
        condition=condition,
        hr_rest=Bpm(round(min(95.0, max(45.0, rest)))),
        hr_max_tanaka=estimate,
        hr_max=hr_max,
        k_g=_r(k_g, 1),
        tau_up=Seconds(_r(tau_up, 1)),
        tau_down=Seconds(_r(tau_down, 1)),
        drift_max=_r(drift, 2),
        tau_drift=Seconds(_r(tau_drift, 0)),
        fatigue=_r(fatigue),
        ecg_noise_bpm=_r(noise, 2),
        ectopic_rate=0.08 if condition is Condition.ECTOPIC else 0.0,
        ectopic_bpm=25.0 if condition is Condition.ECTOPIC else 0.0,
        motion_noise_bpm_per_g=6.0 if condition is Condition.MOTION_ARTEFACT else 0.0,
        vasovagal_at_s=_r(vasovagal, 0) if condition is Condition.VASOVAGAL_PRONE else None,
    )


def generate(seed: int = SEED, size: int = SIZE) -> tuple[CohortSubject, ...]:
    """The cohort. Same seed, same cohort, bit for bit."""
    rng = random.Random(seed)  # noqa: S311  # synthetic data, not crypto
    ages = [rng.randint(MIN_AGE, ADULT_AGE - 1) for _ in range(MINORS)]
    ages += [rng.randint(ADULT_AGE, MAX_AGE) for _ in range(size - MINORS)]
    ages[0] = MIN_AGE  # the youngest age the brief asks about is always in the cohort
    conditions = [Condition.NONE] * size
    for index, condition in zip(rng.sample(range(size), len(SPECIAL)), SPECIAL, strict=True):
        conditions[index] = condition
    return tuple(
        _subject(rng, index, age, condition)
        for index, (age, condition) in enumerate(zip(ages, conditions, strict=True))
    )


def dumps(subjects: Sequence[CohortSubject]) -> str:
    """The committed file's exact text."""
    document = {
        "schema": SCHEMA,
        "seed": SEED,
        "_comment": "Synthetic subjects for simulation only. Regenerate with "
        "`python -m simulation.cohort.generate`; see its docstring for the model.",
        "subjects": [subject.to_json() for subject in subjects],
    }
    return json.dumps(document, indent=2, allow_nan=False) + "\n"


@dataclass(frozen=True, slots=True)
class CohortInvalid:
    detail: str


def _enum[E: Enum](reader: Reader, key: str, kind: type[E]) -> E | None:
    text = reader.text(key)
    try:
        return kind(text)
    except ValueError:
        reader.note(f"'{key}' = {text!r} is not a {kind.__name__}")
        return None


def _parse_subject(entry: Mapping[str, object], where: str) -> CohortSubject | str:
    reader = Reader(entry, where)
    sex = _enum(reader, "sex", Sex)
    fitness = _enum(reader, "fitness", Fitness)
    condition = _enum(reader, "condition", Condition)
    vasovagal = reader.optional_number("vasovagal_at_s")
    numbers = {
        key: reader.number(key)
        for key in (
            "k_g",
            "tau_up",
            "tau_down",
            "drift_max",
            "tau_drift",
            "fatigue",
            "ecg_noise_bpm",
            "ectopic_rate",
            "ectopic_bpm",
            "motion_noise_bpm_per_g",
        )
    }
    subject_id = reader.text("subject_id")
    age = reader.integer("age_years")
    rest, estimate, hr_max = (reader.integer(key) for key in ("hr_rest", "hr_max_tanaka", "hr_max"))
    if reader.problems or sex is None or fitness is None or condition is None:
        return "; ".join(reader.problems)
    return CohortSubject(
        subject_id=subject_id,
        age_years=age,
        sex=sex,
        fitness=fitness,
        condition=condition,
        hr_rest=Bpm(rest),
        hr_max_tanaka=Bpm(estimate),
        hr_max=Bpm(hr_max),
        k_g=numbers["k_g"],
        tau_up=Seconds(numbers["tau_up"]),
        tau_down=Seconds(numbers["tau_down"]),
        drift_max=numbers["drift_max"],
        tau_drift=Seconds(numbers["tau_drift"]),
        fatigue=numbers["fatigue"],
        ecg_noise_bpm=numbers["ecg_noise_bpm"],
        ectopic_rate=numbers["ectopic_rate"],
        ectopic_bpm=numbers["ectopic_bpm"],
        motion_noise_bpm_per_g=numbers["motion_noise_bpm_per_g"],
        vasovagal_at_s=vasovagal,
    )


def parse_cohort(raw: str) -> Result[tuple[CohortSubject, ...], CohortInvalid]:
    """The committed cohort file, strictly."""
    document = load_object(raw)
    if isinstance(document, str):
        return Err(CohortInvalid(document))
    reader = Reader(document, "cohort")
    subjects: list[CohortSubject] = []
    for index, entry in enumerate(reader.objects("subjects")):
        parsed = _parse_subject(entry, f"subjects[{index}]")
        if isinstance(parsed, str):
            return Err(CohortInvalid(parsed))
        subjects.append(parsed)
    if reader.problems or not subjects:
        return Err(CohortInvalid("; ".join(reader.problems) or "no subjects"))
    return Ok(tuple(subjects))


def load_cohort(path: Path = COHORT_PATH) -> tuple[CohortSubject, ...]:
    """The committed cohort. Raises ``ValueError`` when it is missing or malformed."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    parsed = parse_cohort(raw)
    if isinstance(parsed, Err):
        raise ValueError(f"{path.name}: {parsed.error.detail}")
    return parsed.value


class _Args(argparse.Namespace):
    check: bool = False
    out: Path = COHORT_PATH


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulation.cohort.generate")
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    parser.add_argument("--out", type=Path, default=COHORT_PATH)
    args = parser.parse_args(argv, namespace=_Args())
    text = dumps(generate())
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        return 0 if current == text else 1
    args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
