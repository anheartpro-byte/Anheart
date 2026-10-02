"""Property tests: the invariants hold for scenarios nobody wrote down.

Each example is a whole closed-loop run through the real runtime, so the
example counts are modest; the value is in the shapes hypothesis finds that a
hand-written battery does not (a target inside the min-run gap, a stop in the
middle of a ramp, a fault on the first tick of HOLD, a subject whose response
is twice as slow as the controller was tuned for).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from simulation.invariants import check_invariants
from simulation.scenario import (
    SCENARIO_DIR,
    Action,
    BitalinoDisconnect,
    CommsLoss,
    EcgSpec,
    EcgValue,
    EmergencyStop,
    Expectation,
    InjectDriveFault,
    ManualTarget,
    OperatorStop,
    RemoteStop,
    Scenario,
    Shutdown,
    TickException,
)
from simulation.sensors import DirectSensor
from simulation.tests.conftest import load, run
from src.motor.drive import DriveFault
from src.sim.physiology import PhysiologyConfig
from src.training.types import SignalQuality
from src.units import Bpm, Monotonic, OutputRpm, Seconds

RUNS: Final = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    derandomize=True,
)

MANUAL_BASE: Final[Scenario] = load(SCENARIO_DIR / "manual_27_rpm.json")
SHORT_JOG: Final[Scenario] = load(SCENARIO_DIR / "auto_jog_150_dsp.json")
"""The short (13 min) jog programme; replaced onto the DIRECT sensor for speed."""


def _short_jog(
    *, subject: PhysiologyConfig | None = None, actions: tuple[Action, ...] = ()
) -> Scenario:
    return replace(
        SHORT_JOG,
        ecg=EcgSpec(),
        known_defect=None,
        subject=SHORT_JOG.subject if subject is None else subject,
        actions=actions,
    )


def _assert_sound(scenario: Scenario) -> None:
    result = run(scenario)
    violations = check_invariants(result)
    assert not violations, "\n".join(str(violation) for violation in violations[:10])


targets = st.lists(
    st.tuples(
        st.floats(min_value=1.0, max_value=220.0),
        st.one_of(
            st.floats(min_value=0.0, max_value=30.0),
            st.sampled_from([0.0, 0.5, 1.1, 1.2, 27.0, 27.716, 27.8, 32.0]),
        ),
    ),
    min_size=1,
    max_size=6,
)


@RUNS
@given(targets)
def test_any_sequence_of_manual_targets_keeps_every_invariant(
    plan: list[tuple[float, float]],
) -> None:
    """Refused or accepted, any operator input keeps the setpoint legal and the arm smooth."""
    actions: list[Action] = [
        ManualTarget(Seconds(at), OutputRpm(rpm), Expectation.ANY) for at, rpm in plan
    ]
    actions.append(OperatorStop(Seconds(230.0)))
    scenario = replace(
        MANUAL_BASE,
        duration=Seconds(260.0),
        actions=tuple(sorted(actions, key=lambda action: action.at)),
    )
    _assert_sound(scenario)


endings = st.sampled_from(
    [
        "estop",
        "operator_stop",
        "remote_stop",
        "drive_fault",
        "comms_loss",
        "shutdown",
        "tick_exception",
    ]
)


def _ending(kind: str, at: Seconds, lost: float) -> Action:
    actions: dict[str, Action] = {
        "estop": EmergencyStop(at),
        "operator_stop": OperatorStop(at),
        "remote_stop": RemoteStop(at),
        "drive_fault": InjectDriveFault(at, DriveFault.OVERCURRENT),
        "comms_loss": CommsLoss(at, Seconds(lost)),
        "shutdown": Shutdown(at),
        "tick_exception": TickException(at),
    }
    return actions[kind]


@RUNS
@given(endings, st.floats(min_value=3.0, max_value=200.0), st.floats(min_value=0.2, max_value=40.0))
def test_every_ending_at_any_moment_of_a_manual_session_stops_the_motor(
    kind: str, at: float, lost: float
) -> None:
    """Mid-climb, at speed or mid-descent: whatever ends the session, the shaft stops."""
    scenario = replace(
        MANUAL_BASE,
        duration=Seconds(260.0),
        teardown=Seconds(90.0),
        actions=(MANUAL_BASE.actions[0], _ending(kind, Seconds(at), lost)),
    )
    _assert_sound(scenario)


subjects = st.builds(
    PhysiologyConfig,
    hr_rest=st.integers(min_value=50, max_value=90).map(Bpm),
    hr_max=st.integers(min_value=170, max_value=200).map(Bpm),
    k_g=st.floats(min_value=60.0, max_value=170.0),
    tau_up=st.floats(min_value=10.0, max_value=70.0).map(Seconds),
    tau_down=st.floats(min_value=20.0, max_value=120.0).map(Seconds),
    drift_max=st.floats(min_value=0.0, max_value=25.0),
    tau_drift=st.floats(min_value=200.0, max_value=900.0).map(Seconds),
    fatigue=st.floats(min_value=0.0, max_value=0.3),
)


@RUNS
@given(subjects)
def test_any_plausible_subject_keeps_every_invariant_in_a_programme(
    subject: PhysiologyConfig,
) -> None:
    """Fit, unfit, fast, slow, drifting: the programme stays in its domain and ends stopped."""
    _assert_sound(_short_jog(subject=subject))


readings = st.lists(
    st.tuples(
        st.floats(min_value=100.0, max_value=700.0),
        st.integers(min_value=40, max_value=200),
        st.floats(min_value=1.0, max_value=40.0),
    ),
    min_size=1,
    max_size=5,
)


@RUNS
@given(readings, st.booleans())
def test_any_heart_rate_the_sensor_reports_keeps_every_invariant(
    plan: list[tuple[float, int, float]], disconnect: bool
) -> None:
    """Whatever rate the ECG claims, verdicts dominate, the domain holds, the motor stops."""
    actions: list[Action] = [
        EcgValue(Seconds(at), Bpm(bpm), Seconds(duration)) for at, bpm, duration in plan
    ]
    if disconnect:
        actions.append(BitalinoDisconnect(Seconds(max(at for at, _, _ in plan))))
    _assert_sound(_short_jog(actions=tuple(sorted(actions, key=lambda action: action.at))))


@settings(max_examples=200, deadline=None, derandomize=True)
@given(
    st.lists(st.floats(min_value=40.0, max_value=200.0), min_size=5, max_size=80),
    st.floats(min_value=0.0, max_value=10.0),
    st.integers(min_value=0, max_value=2**31),
)
def test_the_direct_sensor_never_fabricates_evidence(
    truths: list[float], noise: float, seed: int
) -> None:
    """Seq only ever advances; a rate only with GOOD quality; never outside the plausible band."""
    sensor = DirectSensor(start=Monotonic(0.0), period=Seconds(1.0), noise_bpm=noise, seed=seed)
    sensor.grade(SignalQuality.NOISY, Monotonic(10.0))
    last = 0
    for step, truth in enumerate(truths):
        reading = sensor.sample(Monotonic(float(step)), Bpm(round(truth)))
        assert reading is not None
        assert reading.seq > last
        last = reading.seq
        if reading.quality is not SignalQuality.GOOD:
            assert reading.bpm is None
        if reading.bpm is not None:
            assert 25 <= reading.bpm <= 240
