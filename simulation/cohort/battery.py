"""The cohort battery: every subject x {auto jog ~150 bpm, auto standard 30 min, manual bench}.

For each subject and session:

1. **eligibility, through the real gate.** The programme is resolved with the
   subject's maximum heart rate by :meth:`src.training.plan.ProfileStore.resolve`
   - the call ``src.local_panel`` makes for a dashboard launch - which re-runs
   every cardiac check of the profile against this rider. A zone above 90 % of
   the subject's HRmax must be REFUSED (and is expected to be exactly when
   ``zone_high > 0.9 x HRmax``); an eligible subject must be accepted.
2. **the run**, through :func:`simulation.harness.run_scenario`, with the
   subject's plant (``PhysiologyConfig``), the DIRECT sensor carrying their ECG
   noise, ectopic beats and motion artefact, and their scripted events
   (non-responder, vasovagal collapse). Manual bench sessions carry the ECG
   traits but no scripted event: nobody rides a bench session.
3. **every invariant** of :mod:`simulation.invariants` on every trace, plus the
   cohort's own expectations (:func:`expectations_for`).

A subject-session pair whose failure is a documented ``raspberry-pi/src``
defect carries that defect (:func:`known_defect_for`); the tests mark it
strict xfail. Runs are independent, so :func:`run_cohort` spreads them over
processes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path
from typing import Final

from simulation.cohort.generate import CohortSubject, Condition, load_cohort
from simulation.harness import PREROLL, RunResult, run_scenario
from simulation.invariants import Metrics, Violation, check_expectations, check_invariants, measure
from simulation.scenario import SCENARIO_DIR, SIM_PROFILES_PATH, Scenario, parse_scenario
from simulation.tracefile import JsonValue
from src.geometry import CONFIRMED_GEAR_RATIO
from src.local_config import DEFAULT_MIN_RIDER_AGE
from src.local_panel import rider_age_refusal
from src.result import Err
from src.training.plan import (
    SHIPPED_DEFAULTS_PATH,
    ZONE_CEILING_FRACTION,
    ProfileStore,
    TrainingProfile,
    UnknownProfile,
    profile_to_document,
)
from src.units import UnixMillis, motor_to_output_rpm

JOG_PROFILE: Final[str] = "jog_150_30_min"
STANDARD_PROFILE: Final[str] = "standard_30_min"
AUTO_DURATION_S: Final[float] = 1810.0
MANUAL_DURATION_S: Final[float] = 420.0
MANUAL_TARGET_RPM: Final[float] = 27.0
MANUAL_STOP_S: Final[float] = 200.0
VASOVAGAL_S: Final[float] = 20.0

_ABSENT_STORE: Final[Path] = SCENARIO_DIR / "_no_such_store.json"
"""A store path that does not exist: :class:`ProfileStore` then serves its defaults, read-only."""


@unique
class SessionType(Enum):
    AUTO_JOG = "auto_jog"
    AUTO_STANDARD = "auto_standard"
    MANUAL_BENCH = "manual_bench"


_PROFILE_OF: Final[Mapping[SessionType, tuple[str, Path]]] = {
    SessionType.AUTO_JOG: (JOG_PROFILE, SIM_PROFILES_PATH),
    SessionType.AUTO_STANDARD: (STANDARD_PROFILE, SHIPPED_DEFAULTS_PATH),
}

VASOVAGAL_ONSET_DEFECT: Final[str] = (
    "RESIDUAL (raspberry-pi/src/training/runtime.py vasovagal gate, README finding 1): the "
    "setpoint no longer rises once a collapse is measurable - the controller may not raise it "
    "while the 5-reading heart-rate trend is below -20 bpm/min - but a rise decided on the very "
    "tick a collapse begins, before any fall has reached the sensor, is still walked out "
    "(invariant vasovagal_no_accel). Evidence ({pair}): {evidence}."
)

KNOWN_VASOVAGAL_ONSET: Final[Mapping[tuple[str, SessionType], str]] = {
    ("S07", SessionType.AUTO_JOG): (
        "Measured 2026-10-05: 887 -> 895 motor rpm (+0.16 output rpm) over t=679.0-679.8 s, "
        "decided at t=679.0 s when the scripted collapse starts (true rate 133 bpm, sensor "
        "132); the four rising ticks read true 132 bpm. The next lower sensor reading is "
        "128 bpm at t=681 s; nothing rises after it, and hr_drop fires at t=701 s. "
        "Before the gate: the controller "
        "kept raising the setpoint through the collapse until hr_drop"
    ),
}
"""The committed cohort's pairs that still show :data:`VASOVAGAL_ONSET_DEFECT`, with the numbers.

The false ``hr_drop`` pairs this battery used to carry (the programme's own
COOLDOWN on 13 subjects, one ectopic reading in BASELINE on S20 and S27, the
motion-artefact subjects S06 and S09) are fixed in ``src/training/safety.py``:
the fall is confirmed on medians of fresh readings and judged against what the
load being removed explains. They are ordinary passing pairs now.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class Eligibility:
    """What the real gate said about this subject and programme."""

    profile: TrainingProfile | None
    """The programme fitted to the subject, or ``None`` when refused."""

    refusal: str | None
    expected_refusal: bool
    """Under ``MIN_RIDER_AGE``, or ``zone_high > 0.9 x HRmax``: when it must refuse."""


def _store(defaults: Path) -> ProfileStore:
    store = ProfileStore(_ABSENT_STORE, defaults)
    loaded = store.load()
    if isinstance(loaded, Err):
        raise ValueError(f"profile store unusable: {loaded.error}")
    return store


def base_profile(profile_id: str, defaults: Path) -> TrainingProfile:
    """A profile as its store holds it. Raises ``ValueError`` when it is not there."""
    found = _store(defaults).get(profile_id)
    if isinstance(found, Err):
        raise ValueError(f"{profile_id}: {found.error}")
    return found.value


def eligibility(subject: CohortSubject, session: SessionType) -> Eligibility | None:
    """The gate's answer for an auto session; ``None`` for a manual one (no programme)."""
    spec = _PROFILE_OF.get(session)
    if spec is None:
        return None
    profile_id, defaults = spec
    store = _store(defaults)
    too_young = rider_age_refusal(subject.age_years, DEFAULT_MIN_RIDER_AGE)
    expected = too_young is not None or base_profile(profile_id, defaults).zone_high_bpm > (
        ZONE_CEILING_FRACTION * subject.hr_max
    )
    if too_young is not None:
        # The console's own age gate (src.local_panel), before any profile is resolved.
        return Eligibility(profile=None, refusal=too_young, expected_refusal=expected)
    resolved = store.resolve(profile_id, at=UnixMillis(0), subject_hr_max=subject.hr_max)
    if not isinstance(resolved, Err):
        return Eligibility(profile=resolved.value.profile, refusal=None, expected_refusal=expected)
    error = resolved.error
    if isinstance(error, UnknownProfile):
        raise ValueError(f"{profile_id} vanished from its store")
    return Eligibility(profile=None, refusal=error.detail, expected_refusal=expected)


def _subject_doc(subject: CohortSubject) -> Mapping[str, JsonValue]:
    return {
        "hr_rest": int(subject.hr_rest),
        "hr_max": int(subject.hr_max),
        "k_g": subject.k_g,
        "tau_up": float(subject.tau_up),
        "tau_down": float(subject.tau_down),
        "drift_max": subject.drift_max,
        "tau_drift": float(subject.tau_drift),
        "fatigue": subject.fatigue,
    }


def _ecg_doc(subject: CohortSubject, seed: int) -> Mapping[str, JsonValue]:
    return {
        "mode": "direct",
        "noise_bpm": subject.ecg_noise_bpm,
        "seed": seed,
        "ectopic_rate": subject.ectopic_rate,
        "ectopic_bpm": subject.ectopic_bpm,
        "motion_noise_bpm_per_g": subject.motion_noise_bpm_per_g,
    }


def _events(subject: CohortSubject) -> list[JsonValue]:
    """The plant's scripted events for this subject, timed on the plant's clock (pre-roll first)."""
    events: list[JsonValue] = []
    if subject.condition is Condition.NONRESPONDER:
        events.append({"event": "nonresponder", "at_s": 0.0, "duration_s": 4000.0})
    at = subject.vasovagal_at_s
    if subject.condition is Condition.VASOVAGAL_PRONE and at is not None:
        start = at + float(PREROLL)
        events.append({"event": "vasovagal_drop", "at_s": start, "duration_s": VASOVAGAL_S})
    return events


def document_for(
    subject: CohortSubject, session: SessionType, profile: TrainingProfile | None
) -> Mapping[str, JsonValue]:
    """The scenario document of one subject-session run (same schema as scenarios/*.json)."""
    seed = int(subject.subject_id[1:])
    common: dict[str, JsonValue] = {
        "name": f"cohort_{subject.subject_id}_{session.value}",
        "description": f"{subject.subject_id} ({subject.age_years} y, {subject.sex.value}, "
        f"{subject.fitness.value}, {subject.condition.value}) - {session.value}",
        "tags": ["cohort", session.value, subject.condition.value],
        "subject": _subject_doc(subject),
        "ecg": _ecg_doc(subject, seed),
    }
    if session is SessionType.MANUAL_BENCH or profile is None:
        return {
            **common,
            "kind": "manual",
            "duration_s": MANUAL_DURATION_S,
            "actions": [
                {"at_s": 2.0, "do": "manual_target", "output_rpm": MANUAL_TARGET_RPM},
                {"at_s": MANUAL_STOP_S, "do": "operator_stop"},
            ],
            "expect": {
                "end_reason": "operator_stop",
                "reaches_output_rpm": MANUAL_TARGET_RPM,
                "max_output_rpm": MANUAL_TARGET_RPM + 0.05,
            },
        }
    return {
        **common,
        "kind": "auto",
        "duration_s": AUTO_DURATION_S,
        "profile": dict(profile_to_document(profile)),
        "subject_events": _events(subject),
        "expect": expectations_for(subject, profile),
    }


def expectations_for(subject: CohortSubject, profile: TrainingProfile) -> dict[str, JsonValue]:
    """What an auto run of this subject must show, beyond the invariants.

    Always: never above the profile's ceiling. A vasovagal-prone subject must
    see ``hr_drop`` fire; any other subject must see neither ``hr_drop`` (the
    presyncope rule) nor ``hr_critical`` (no cohort plant reaches it).
    """
    ceiling = motor_to_output_rpm(profile.max_rpm, CONFIRMED_GEAR_RATIO)
    expect: dict[str, JsonValue] = {"max_output_rpm": round(float(ceiling) + 0.05, 3)}
    if subject.condition is Condition.VASOVAGAL_PRONE:
        expect["rules"] = ["hr_drop"]
    else:
        # hr_drop is the presyncope rule: nobody here is fainting.
        expect["forbid_rules"] = ["hr_drop", "hr_critical"]
    return expect


def known_defect_for(subject: CohortSubject, session: SessionType) -> str | None:
    """The documented defect this pair is expected to show, if any."""
    if session is not SessionType.MANUAL_BENCH and subject.age_years < DEFAULT_MIN_RIDER_AGE:
        return None  # refused by the console's age gate: nothing runs, nothing to show
    evidence = KNOWN_VASOVAGAL_ONSET.get((subject.subject_id, session))
    if evidence is not None:
        pair = f"{subject.subject_id} {session.value}"
        return VASOVAGAL_ONSET_DEFECT.format(pair=pair, evidence=evidence)
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class CohortOutcome:
    """One subject-session pair, judged."""

    subject: CohortSubject
    session: SessionType
    eligibility: Eligibility | None
    metrics: Metrics | None
    """``None`` when the programme was (rightly or wrongly) refused and nothing ran."""

    end: str
    violations: tuple[str, ...]
    known_defect: str | None

    @property
    def status(self) -> str:
        """PASS, FAIL, XFAIL (fails as documented) or FIXED? (documented defect that passed)."""
        if self.known_defect is None:
            return "PASS" if not self.violations else "FAIL"
        return "XFAIL" if self.violations else "FIXED?"


def scenario_for(document: Mapping[str, JsonValue]) -> Scenario:
    parsed = parse_scenario(json.dumps(document), where=str(document.get("name")))
    if isinstance(parsed, Err):
        raise ValueError(parsed.error.detail)
    return parsed.value


def _eligibility_violations(gate: Eligibility | None) -> list[str]:
    if gate is None:
        return []
    refused = gate.refusal is not None
    if refused and not gate.expected_refusal:
        return [f"[eligibility] refused although the zone fits: {gate.refusal}"]
    if gate.expected_refusal and not refused:
        return ["[eligibility] accepted although the zone is above 90 % of HRmax"]
    return []


def judge(result: RunResult) -> tuple[Violation, ...]:
    """Every invariant and the run's expectations."""
    return (*check_invariants(result), *check_expectations(result))


def run_subject(
    subject: CohortSubject, session: SessionType, *, keep: Callable[[RunResult], None] | None = None
) -> CohortOutcome:
    """Gate, run and judge one pair. ``keep`` receives the full run (for reports and viewers)."""
    gate = eligibility(subject, session)
    violations = _eligibility_violations(gate)
    defect = known_defect_for(subject, session)
    if gate is not None and gate.profile is None:
        return CohortOutcome(
            subject=subject,
            session=session,
            eligibility=gate,
            metrics=None,
            end="refused",
            violations=tuple(violations),
            known_defect=None,
        )
    profile = None if gate is None else gate.profile
    result = asyncio.run(run_scenario(scenario_for(document_for(subject, session, profile))))
    if keep is not None:
        keep(result)
    violations += [str(violation) for violation in judge(result)]
    metrics = measure(result)
    return CohortOutcome(
        subject=subject,
        session=session,
        eligibility=gate,
        metrics=metrics,
        end=result.start_refusal or metrics.end_reason or "-",
        violations=tuple(violations),
        known_defect=defect,
    )


def _worker(pair: tuple[CohortSubject, SessionType]) -> CohortOutcome:
    logging.disable(logging.CRITICAL)
    return run_subject(*pair)


def pairs(
    subjects: Sequence[CohortSubject], sessions: Sequence[SessionType] = tuple(SessionType)
) -> list[tuple[CohortSubject, SessionType]]:
    return [(subject, session) for subject in subjects for session in sessions]


def run_cohort(
    subjects: Sequence[CohortSubject] | None = None,
    sessions: Sequence[SessionType] = tuple(SessionType),
    *,
    workers: int = 1,
) -> tuple[CohortOutcome, ...]:
    """Every pair, over ``workers`` processes (1: in this process)."""
    work = pairs(load_cohort() if subjects is None else subjects, sessions)
    if workers <= 1:
        return tuple(_worker(pair) for pair in work)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return tuple(pool.map(_worker, work, chunksize=2))


def minor_is_refused_or_capped(subject: CohortSubject) -> bool:
    """Whether the machine refuses this rider the jog, asked of the real gate.

    "Capped" is kept in the name for the finding it answers; since the fix every
    rider under ``MIN_RIDER_AGE`` is plainly refused by the console's age gate,
    so no capped case is left to measure.
    """
    gate = eligibility(subject, SessionType.AUTO_JOG)
    return gate is None or gate.profile is None
