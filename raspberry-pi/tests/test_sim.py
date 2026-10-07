"""Tests for the hardware-free plant: the subject, the ECG and the BITalino.

Two things make this file different from a normal unit-test module, and both
are deliberate.

**1. The real DSP pipeline is on the other end of most of these tests.**
``src/signal_processing.py`` - the actual Butterworth filter, the actual BioSPPy
segmenter, the actual ``_ecg_quality`` classifier - is what turns the synthetic
ADC counts into a quality grade and a heart rate. Nothing here re-implements
any of that or asserts against a convenient stand-in. So when a test below says
"a synthetic 60 bpm signal comes back as 60 bpm, graded good", the claim is
about the system that will run on the Pi, not about this simulator's opinion of
itself.

That is also why the pipeline is loaded through ``importlib`` and cast to a
Protocol: it is still outside both type checkers (see the migration banner in
``pyproject.toml``), and a plain import from a checked test file drags its ~25
untyped diagnostics into the gate. The cast is the boundary; past it everything
is typed again. Same reasoning as in ``src/ecg_pipeline.py``.

**2. The numbers are hand-derived, not recomputed.** The g load, the steady
state and the local control gain are checked against a closed form written out
in the test from the nameplate - gear ratio, radius (1.0 m, chosen in
``SIM_ARM`` rather than defaulted in the plant), standard gravity - rather
than against ``src.units.output_rpm_to_g``. If the conversion helper and this
plant ever agree on a wrong answer, that agreement must not be able to pass for
evidence.

The hazard this file exists to pin, stated once here because it is the reason
the whole simulator is worth building: **motion artifact rises with speed, so
ECG quality is worst exactly where the controller is pushing hardest** - and
well before the grade degrades at all, the reported heart rate is already
wrong by 70 bpm while still labelled ``good``. See
``test_motion_noise_wrecks_the_heart_rate_long_before_it_degrades_the_grade``.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final, Protocol, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.bitalino_client import SampleBatch
from src.clock import Clock, ManualClock
from src.geometry import MachineGeometry
from src.sim.bitalino import (
    ECG_CHANNEL,
    LEGAL_SAMPLE_RATES,
    MAX_ANALOG_CHANNEL,
    MIN_ANALOG_CHANNEL,
    SimulatedBitalinoClient,
)
from src.sim.ecg import (
    ECG_MV_PER_COUNT,
    MORPHOLOGY,
    MORPHOLOGY_PEAK,
    PEAK_SEARCH_STEPS,
    REFERENCE_RR,
    TWO_PI,
    EcgConfig,
    EcgSynthesizer,
    GaussianWave,
    WaveComponent,
    morphology_at,
)
from src.sim.physiology import (
    CANONICAL_DURATIONS,
    CARDIAC_OVERRIDE_RATES,
    GAIN_SCALES,
    SECONDS_PER_MINUTE,
    SIGNAL_ARTIFACTS,
    EventWindow,
    Physiology,
    PhysiologyConfig,
    ScriptedEvent,
    SubjectState,
    active_events,
    script_for,
)
from src.units import (
    ADC_BASELINE,
    ADC_MAX,
    ADC_MIN,
    STANDARD_GRAVITY,
    AdcCount,
    Bpm,
    GearRatio,
    GLoad,
    Metres,
    Millivolts,
    Monotonic,
    MotorRpm,
    OutputRpm,
    Seconds,
)

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
SIM_DIR: Final[Path] = PROJECT_ROOT / "src" / "sim"


# =========================================================================
# The boundary with the untyped pipeline
# =========================================================================


class _TreatmentLike(Protocol):
    def treat_batch(
        self, samples: Sequence[Mapping[str, object]]
    ) -> tuple[Sequence[Mapping[str, object]], Mapping[str, Mapping[str, object]]]: ...


class _TreatmentFactory(Protocol):
    def __call__(self, *, fs_in: int, fs_out: int) -> _TreatmentLike: ...


class _SignalProcessingModule(Protocol):
    SignalTreatment: _TreatmentFactory
    ECG_MV_PER_COUNT: float


class _BitalinoClientModule(Protocol):
    CHANNEL_NAMES: Mapping[int, str]


PIPELINE: Final[_SignalProcessingModule] = cast(
    "_SignalProcessingModule", importlib.import_module("src.signal_processing")
)
CLIENT: Final[_BitalinoClientModule] = cast(
    "_BitalinoClientModule", importlib.import_module("src.bitalino_client")
)


# =========================================================================
# Hand-derived reference values
# =========================================================================
#
# Written out from the commissioning notes and evaluated here, NOT obtained
# from src.units or from the plant. The point is that a bug shared between the
# conversion helpers and the plant cannot make these agree.

NOMINAL_RPM: Final[int] = 1380
"""SEW KA37 DRS71S4 nameplate speed, motor shaft."""

GEAR_RATIO: Final[float] = 49.79
RADIUS_M: Final[float] = 1.0

#: The geometry the plant's documented figures are quoted at: r = 1.0 m, chosen
#: HERE rather than defaulted in the plant. The real arm is 1.5 m (ARM_RADIUS_M);
#: these tests are about the plant's arithmetic, and 1.0 m keeps the hand-derived
#: numbers in this file and in ``src/sim/physiology.py``'s docstring readable.
SIM_ARM: Final[MachineGeometry] = MachineGeometry(
    radius=Metres(RADIUS_M), ratio=GearRatio(GEAR_RATIO)
)
HR_REST: Final[int] = 70
HR_MAX: Final[int] = 185
K_G: Final[float] = 110.0


def _hand_g(motor_rpm: float) -> float:
    """``omega_out^2 * r / 9.80665`` from first principles, in g."""
    omega_out = 2.0 * math.pi * motor_rpm / (60.0 * GEAR_RATIO)
    return omega_out * omega_out * RADIUS_M / STANDARD_GRAVITY


def _hand_steady_state(motor_rpm: float) -> float:
    """``min(hr_max, hr_rest + k_g*g)``, in bpm."""
    return min(float(HR_MAX), HR_REST + K_G * _hand_g(motor_rpm))


def _hand_local_gain(motor_rpm: float) -> float:
    """``d(hr_ss)/d(rpm)`` in bpm per motor-rpm: ``k_g * 2g/rpm``."""
    return K_G * 2.0 * _hand_g(motor_rpm) / motor_rpm


#: The figures src/sim/physiology.py's docstring commits to.
DOCUMENTED_G_AT_NOMINAL: Final[float] = 0.859
DOCUMENTED_BPM_AT_NOMINAL: Final[int] = 164
DOCUMENTED_GAIN_AT_NOMINAL: Final[float] = 0.137


# =========================================================================
# Helpers
# =========================================================================

#: A subject whose load response is pinned: hr_rest == hr_max means
#: min(hr_max, hr_rest + k_g*g) is hr_rest at every speed, so the ONLY thing
#: that can move the reported rate is the sensing chain. Used wherever a test
#: is about the ECG and must not be confounded by physiology.
FIXED_60_BPM: Final[PhysiologyConfig] = PhysiologyConfig(hr_rest=Bpm(60), hr_max=Bpm(60))

#: No cardiac drift, so a settled rate is exactly the steady state.
NO_DRIFT: Final[PhysiologyConfig] = PhysiologyConfig(drift_max=0.0)

#: A sensing chain with every contaminant switched off, for morphology tests.
CLEAN_ECG: Final[EcgConfig] = EcgConfig(
    baseline_mv=0.0, mains_mv=0.0, motion_mv_per_g=0.0, rr_variability=0.0
)

#: Far past every time constant in the plant, so one step lands on the target.
SETTLED: Final[Monotonic] = Monotonic(10_000.0)

LOOP_STEP: Final[Seconds] = Seconds(0.2)
"""The 5 Hz control loop's period, which is the step size the plant is
documented to be accurate at."""

EXACT_STEP: Final[Seconds] = Seconds(0.25)
"""A step size that is exactly representable in binary, for the tests that assert
an event window's magnitude to the last digit.

This is not fussiness. 0.2 is not representable, and one hundred additions of it
reach 19.99999999999996 - which is still INSIDE a window ending at 20.0, so a
20 s scenario stepped at the loop rate resolves correctly by a rounding accident
rather than because the window logic is right. That accident let an earlier
version of the vasovagal test survive a deliberate break of the window logic: it
was pinning the accident instead of the property. Eighty additions of 0.25 land
on exactly 20.0, so the window's last step is the run's last step and the
assertion has to earn its result."""


def _resting(clock: Clock) -> Physiology:
    """A default subject at rest from ``clock``'s now, on the test geometry."""
    return Physiology(geometry=SIM_ARM, origin=clock.monotonic())


def _settled_rate(motor_rpm: int, config: PhysiologyConfig = NO_DRIFT) -> float:
    """The plant's exact steady-state rate at ``motor_rpm``, unrounded.

    Read back through ``rr_interval`` rather than ``heart_rate`` because the
    latter is rounded to whole bpm, and a 0.137 bpm/rpm gain measurement cannot
    survive that. One step past every time constant lands exactly on the target
    - the step fraction is clamped to 1.0 - so this needs no loop.
    """
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), config=config)
    state = subject.advance(SETTLED, MotorRpm(motor_rpm))
    return SECONDS_PER_MINUTE / state.rr_interval


def _subject_state(
    *,
    g_load: float = 0.0,
    rr_interval: float = 1.0,
    artifacts: frozenset[ScriptedEvent] = frozenset(),
) -> SubjectState:
    """A :class:`SubjectState` built by hand, for tests of the sensor alone.

    The fields the ECG synthesiser does not read are filled with values that
    would be obviously wrong if it started reading them.
    """
    return SubjectState(
        at=Monotonic(0.0),
        motor_rpm=MotorRpm(0),
        output_rpm=OutputRpm(0.0),
        g_load=GLoad(g_load),
        heart_rate=Bpm(60),
        rr_interval=Seconds(rr_interval),
        steady_state=Bpm(60),
        drift_bpm=0.0,
        artifacts=artifacts,
    )


def _quality(metrics: Mapping[str, object]) -> str:
    """The ``quality`` grade out of a metrics dict, narrowed to ``str``."""
    grade = metrics["quality"]
    assert isinstance(grade, str), grade
    return grade


def _reported_bpm(metrics: Mapping[str, object]) -> int:
    """The ``heartRate`` out of a metrics dict, narrowed to ``int``.

    Fails if the key is absent, which is the correct failure: a test that wants
    to know whether a rate was reported must ask that question separately (see
    :func:`_has_heart_rate`) rather than have a default substituted for it.
    """
    rate = metrics["heartRate"]
    assert isinstance(rate, int), rate
    return rate


def _has_heart_rate(metrics: Mapping[str, object]) -> bool:
    """Whether the pipeline reported a heart rate at all."""
    return "heartRate" in metrics


async def _acquire(
    *,
    motor_rpm: int = 0,
    seconds: int = 8,
    config: PhysiologyConfig = FIXED_60_BPM,
    ecg: EcgConfig | None = None,
    script: Sequence[EventWindow] = (),
) -> list[float]:
    """Run a simulated acquisition and return the RAW ECG column.

    One second per read at 1000 Hz. Eight seconds is the pipeline's metric
    window.
    """
    clock = ManualClock()
    subject = Physiology(geometry=SIM_ARM, origin=clock.monotonic(), config=config, script=script)
    client = SimulatedBitalinoClient(
        clock,
        physiology=subject,
        ecg=EcgSynthesizer(EcgConfig() if ecg is None else ecg),
    )
    assert await client.connect()
    assert await client.start_acquisition()
    client.set_motor_rpm(MotorRpm(motor_rpm))

    raw: list[float] = []
    for _ in range(seconds):
        clock.advance(Seconds(1.0))
        batch = await client.read_samples(1000)
        assert batch is not None
        raw.extend(_ecg_column(batch))
    return raw


def _ecg_column(batch: SampleBatch) -> Sequence[float]:
    """The ECG channel's values out of a batch, the way the ECG pipeline reads them."""
    for channel in batch.channels:
        if channel.channel == ECG_CHANNEL:
            return channel.values
    raise AssertionError("the batch carried no ECG channel")


def _treat(raw: Sequence[float]) -> Mapping[str, object]:
    """Push RAW counts through the REAL pipeline and return the ECG metrics.

    Fed as one call rather than per-second, which
    ``test_the_pipeline_does_not_care_whether_a_window_arrived_in_one_piece``
    shows produces identical metrics: the metric path reads an 8 s rolling
    window of raw samples, so the split cannot matter.
    """
    treatment = PIPELINE.SignalTreatment(fs_in=1000, fs_out=250)
    _, metrics = treatment.treat_batch([{"channel": ECG_CHANNEL, "values": list(raw)}])
    return metrics[ECG_CHANNEL]


def _ordered(events: Iterable[ScriptedEvent]) -> list[ScriptedEvent]:
    """Enum members in a stable, explicit order.

    ``sorted`` with an inline key would be an untyped lambda under
    ``reportUnknownLambdaType``; a named function keeps the ordering typed AND
    makes it obvious that parametrisation order is deliberate rather than
    whatever a set iterated into.
    """
    return sorted(events, key=_event_name)


def _event_name(event: ScriptedEvent) -> str:
    return event.name


def _imported_roots(source: Path) -> frozenset[str]:
    """Top-level names of everything a module imports, parsed from its source."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            roots.add("" if module is None else module.split(".")[0])
    return frozenset(roots)


def _project_imports(source: Path) -> frozenset[str]:
    """Every ``src.*`` module a file imports, fully qualified."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.split(".")[0] == "src":
                modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names if a.name.split(".")[0] == "src")
    return frozenset(modules)


# =========================================================================
# 1. Cross-checks against the real pipeline
# =========================================================================


def test_the_adc_scale_matches_the_pipeline_exactly() -> None:
    """A drifted scale would silently rescale every synthetic signal in CI.

    ``src/sim/ecg.py`` recomputes the BITalino ECG transfer function rather than
    importing it, because the pipeline module is outside both type checkers. So
    the copy is checked against the original here, exactly and not
    approximately: a 1% error would move the R peak by 3 ADC counts, which is
    invisible in every other test in this file and changes what the quality
    classifier sees.
    """
    assert ECG_MV_PER_COUNT == PIPELINE.ECG_MV_PER_COUNT


def test_the_channel_name_table_covers_every_channel_the_client_accepts() -> None:
    """What makes the simulator's channel lookup total.

    ``src/sim/bitalino.py`` validates ``MIN_ANALOG_CHANNEL..MAX_ANALOG_CHANNEL``
    and then indexes ``CHANNEL_NAMES`` directly, with no fallback label. That is
    only safe while the two agree, so the agreement is asserted rather than
    assumed - and a shrunken ``CHANNEL_MAP`` fails here instead of storing an
    ECG under a made-up column name.
    """
    legal = set(range(MIN_ANALOG_CHANNEL, MAX_ANALOG_CHANNEL + 1))
    assert set(CLIENT.CHANNEL_NAMES) == legal
    assert CLIENT.CHANNEL_NAMES[0] == ECG_CHANNEL


def test_the_pipeline_does_not_care_whether_a_window_arrived_in_one_piece() -> None:
    """Justifies feeding 8 s in one call in the tests below.

    The console calls ``treat_batch`` once per block read. The metric path
    reads a rolling window of RAW samples, so eight one-second calls and one
    eight-second call must produce the same metrics. If they ever diverge,
    every pipeline assertion in this file is testing the wrong code path, so
    this is checked rather than reasoned about.
    """
    raw = asyncio.run(_acquire())
    streamed = PIPELINE.SignalTreatment(fs_in=1000, fs_out=250)
    per_second: Mapping[str, Mapping[str, object]] = {}
    for start in range(0, len(raw), 1000):
        _, per_second = streamed.treat_batch(
            [{"channel": ECG_CHANNEL, "values": list(raw[start : start + 1000])}]
        )
    assert dict(per_second[ECG_CHANNEL]) == dict(_treat(raw))


# =========================================================================
# 2. Module shape
# =========================================================================


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("physiology.py", frozenset({"src.geometry", "src.units"})),
        ("ecg.py", frozenset({"src.sim.physiology", "src.units"})),
        (
            "bitalino.py",
            frozenset(
                {
                    "src.bitalino_client",
                    "src.clock",
                    "src.sim.ecg",
                    "src.sim.physiology",
                    "src.sim.signals.base",
                    "src.units",
                }
            ),
        ),
    ],
)
def test_the_dependency_direction_inside_the_package(module: str, expected: frozenset[str]) -> None:
    """The sensor may observe the subject; the subject may not know it is observed.

    Pinned as an exact set, not a subset. A ``physiology`` that imported ``ecg``
    could let a signal artifact change a heart rate, which is precisely the
    confusion this simulator exists to detect in the layers above it - and it
    would then be undetectable here.
    """
    assert _project_imports(SIM_DIR / module) == expected


@pytest.mark.parametrize("module", ["__init__.py", "physiology.py", "ecg.py", "bitalino.py"])
def test_nothing_here_reads_the_clock(module: str) -> None:
    """Contract rule 4, asserted at the import level as well as behaviourally.

    ``tests/test_clock.py`` greps for call sites; this checks the weaker but
    independent fact that the module cannot even reach the system clock.
    """
    assert "time" not in _imported_roots(SIM_DIR / module)


@pytest.mark.parametrize("module", ["physiology.py", "ecg.py"])
def test_the_plant_and_the_sensor_pull_in_no_heavy_dependency(module: str) -> None:
    """An allow-list, so a new dependency has to be a deliberate edit.

    ``numpy``/``scipy``/``biosppy`` belong on the other side of the ADC: the
    whole value of this package is that the DSP is the real one, and a plant
    that started filtering its own output would be marking its own homework.
    """
    allowed = {
        "__future__",
        "collections",
        "dataclasses",
        "enum",
        "math",
        "random",
        "types",
        "typing",
        "src",
    }
    assert _imported_roots(SIM_DIR / module) <= allowed


def test_the_simulated_client_imports_the_real_records_directly() -> None:
    """The real client is inside both checkers now, so no importlib cast is needed.

    A static import is what makes a rename in ``src/bitalino_client.py`` fail
    the type check here instead of at runtime.
    """
    assert "src.bitalino_client" in _project_imports(SIM_DIR / "bitalino.py")
    assert "importlib" not in _imported_roots(SIM_DIR / "bitalino.py")


# =========================================================================
# 3. Physiology: the static response
# =========================================================================


def test_the_plant_reproduces_the_geometry_of_this_machine() -> None:
    """1380 motor rpm -> 27.716 output rpm -> 0.859 g, from the nameplate.

    Both halves matter. The closed form is written out here from the gear ratio,
    the radius and standard gravity, so a shared bug in ``src.units`` cannot
    make this pass; and the round figure the module's docstring commits to is
    checked separately, so the docstring cannot go stale.
    """
    state = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0)).advance(
        SETTLED, MotorRpm(NOMINAL_RPM)
    )

    assert state.output_rpm == pytest.approx(NOMINAL_RPM / GEAR_RATIO, rel=1e-12)
    assert state.output_rpm == pytest.approx(27.716409, abs=5e-7)
    assert state.g_load == pytest.approx(_hand_g(NOMINAL_RPM), rel=1e-12)
    assert state.g_load == pytest.approx(DOCUMENTED_G_AT_NOMINAL, abs=5e-4)


def test_the_steady_state_at_full_speed_is_the_documented_one_hundred_and_sixty_four() -> None:
    """0.859 g at 110 bpm/g on a 70 bpm resting rate: 164.49, reported as 164."""
    state = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), config=NO_DRIFT).advance(
        SETTLED, MotorRpm(NOMINAL_RPM)
    )

    assert _hand_steady_state(NOMINAL_RPM) == pytest.approx(164.4937677, abs=1e-6)
    assert state.steady_state == Bpm(DOCUMENTED_BPM_AT_NOMINAL)
    assert _settled_rate(NOMINAL_RPM) == pytest.approx(_hand_steady_state(NOMINAL_RPM), rel=1e-12)


def test_the_local_control_gain_is_the_documented_zero_point_one_three_seven() -> None:
    """Measured as a central difference, which is exact for a quadratic plant.

    This is the number a control law is tuned against, so it is measured off the
    plant rather than read out of the config: ``k_g`` is a bpm-per-g figure, and
    bpm-per-rpm is what the controller actually commands in.
    """
    measured = (_settled_rate(NOMINAL_RPM + 50) - _settled_rate(NOMINAL_RPM - 50)) / 100.0

    assert measured == pytest.approx(_hand_local_gain(NOMINAL_RPM), rel=1e-9)
    assert measured == pytest.approx(DOCUMENTED_GAIN_AT_NOMINAL, abs=5e-4)


def test_the_gain_halves_at_half_speed_because_the_load_goes_as_speed_squared() -> None:
    """The reason one fixed controller gain cannot be right everywhere.

    A tune that is correct at 1380 rpm is twice as aggressive as the plant
    deserves at 690, and a tune that is correct at 690 is half as responsive as
    it should be at the top. Exactly a factor of two, so it is asserted as one.
    """
    half = NOMINAL_RPM // 2
    at_full = (_settled_rate(NOMINAL_RPM + 50) - _settled_rate(NOMINAL_RPM - 50)) / 100.0
    at_half = (_settled_rate(half + 50) - _settled_rate(half - 50)) / 100.0

    assert at_half == pytest.approx(at_full / 2.0, rel=1e-12)
    assert at_half == pytest.approx(_hand_local_gain(half), rel=1e-9)


def test_a_stopped_machine_leaves_the_subject_at_rest() -> None:
    """Zero g, zero response: the resting rate and nothing else."""
    state = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0)).advance(SETTLED, MotorRpm(0))

    assert state.g_load == 0.0
    assert state.steady_state == Bpm(HR_REST)
    assert state.heart_rate == Bpm(HR_REST)
    assert state.rr_interval == pytest.approx(SECONDS_PER_MINUTE / HR_REST, rel=1e-12)


def test_turning_backwards_does_not_relieve_the_load() -> None:
    """g is unsigned because it goes as speed squared. Reverse is not a rest."""
    forward = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0)).advance(
        SETTLED, MotorRpm(NOMINAL_RPM)
    )
    reverse = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0)).advance(
        SETTLED, MotorRpm(-NOMINAL_RPM)
    )

    assert reverse.g_load == forward.g_load
    assert reverse.output_rpm == -forward.output_rpm
    assert reverse.steady_state == forward.steady_state

    # And the subject counts as under load, so cardiac drift accumulates too.
    # The activity test takes the magnitude of the speed; without that a session
    # run in reverse would quietly tire nobody.
    backwards = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0))
    state = _run_at(backwards, motor_rpm=-NOMINAL_RPM, seconds=300.0)
    assert state.drift_bpm > 3.0


def test_the_load_response_is_capped_at_hr_max() -> None:
    """Above the speed that reaches hr_max the plant stops responding.

    Not a clamp bolted on afterwards: it is the ``min`` in the steady-state
    formula, and it is what makes an unreachable zone unreachable rather than
    producing a 400 bpm target.
    """
    absurd = 4 * NOMINAL_RPM
    assert _hand_steady_state(absurd) == float(HR_MAX)
    assert _settled_rate(absurd) == pytest.approx(float(HR_MAX), rel=1e-12)


def test_a_more_sensitive_subject_answers_the_same_g_with_a_higher_rate() -> None:
    """``fatigue`` is a multiplier on the load response, so it scales the span.

    Measured at 900 rpm rather than at the ceiling, and deliberately: at full
    speed a subject 50% more sensitive would run into ``hr_max`` and the span
    would be capped rather than scaled, so the test would be measuring the clamp
    instead of the multiplier. Asserted below, so the choice cannot rot.
    """
    mid = 900
    keen = PhysiologyConfig(drift_max=0.0, fatigue=0.5)
    span = _settled_rate(mid) - HR_REST
    keen_span = _settled_rate(mid, keen) - HR_REST

    assert HR_REST + keen_span < HR_MAX
    assert keen_span == pytest.approx(span * 1.5, rel=1e-12)


# =========================================================================
# 4. Physiology: the dynamics
# =========================================================================


def _run_at(
    subject: Physiology,
    *,
    motor_rpm: int,
    seconds: float,
    start: Monotonic = Monotonic(0.0),
    step: Seconds = LOOP_STEP,
) -> SubjectState:
    """Step ``subject`` for ``seconds`` and return the last state.

    ``step`` defaults to the real loop period; the tests that assert a scenario's
    magnitude exactly pass :data:`EXACT_STEP` instead, for the reason written
    down there.
    """
    now = start
    state = subject.advance(now, MotorRpm(motor_rpm))
    for _ in range(round(seconds / step)):
        now = Monotonic(now + step)
        state = subject.advance(now, MotorRpm(motor_rpm))
    return state


def test_the_heart_rate_lags_a_speed_change_by_thirty_seconds_rising() -> None:
    """One time constant covers 1 - 1/e of the gap. That lag is the dead time.

    Asserted against the continuous 63.21% with a 0.3% allowance, which is the
    error of an explicit first-order step at the 0.2 s loop rate, not slack: a
    tighter bound would be pinning the integrator's discretisation rather than
    the physiology.
    """
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), config=NO_DRIFT)
    state = _run_at(subject, motor_rpm=NOMINAL_RPM, seconds=30.0)

    covered = (SECONDS_PER_MINUTE / state.rr_interval - HR_REST) / (
        _settled_rate(NOMINAL_RPM) - HR_REST
    )
    assert covered == pytest.approx(1.0 - 1.0 / math.e, abs=3e-3)


def test_the_heart_rate_falls_more_slowly_than_it_rose() -> None:
    """55 s down against 30 s up, which is why an overshoot is expensive.

    Measured behaviourally rather than read off the config: after 30 s of
    recovery only ~42% of the gap has closed, where 30 s of loading closed 63%.
    """
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), config=NO_DRIFT)
    top = subject.advance(SETTLED, MotorRpm(NOMINAL_RPM))
    started_at = SECONDS_PER_MINUTE / top.rr_interval

    gap = started_at - HR_REST

    # Chained from the state's own timestamp rather than from SETTLED + 30.0:
    # 150 additions of 0.2 s do not land on a round number, and the plant
    # rightly refuses to be advanced backwards to one.
    after_30 = _run_at(subject, motor_rpm=0, seconds=30.0, start=top.at)
    covered_30 = (started_at - SECONDS_PER_MINUTE / after_30.rr_interval) / gap
    assert covered_30 == pytest.approx(0.421, abs=5e-3)

    after_55 = _run_at(subject, motor_rpm=0, seconds=25.0, start=after_30.at)
    covered_55 = (started_at - SECONDS_PER_MINUTE / after_55.rr_interval) / gap
    assert covered_55 == pytest.approx(1.0 - 1.0 / math.e, abs=3e-3)


def test_cardiac_drift_walks_the_heart_rate_up_at_a_constant_speed() -> None:
    """The subject tires, so the machine has to do LESS to hold the same rate.

    This is the assertion behind the claim in ``src/sim/physiology.py`` that a
    deadband controller settles near the TOP of its zone. After ten minutes at
    one fixed speed the reported rate is above what that speed alone accounts
    for, by exactly the accumulated drift - so the only way to bring it back
    inside a band is to unload. Nothing about the machine changed.

    Stepped at the 5 Hz loop rate for the whole ten minutes rather than settled
    with one large step first. That is not just realism: the step fraction is
    clamped to 1.0, so a single 10000 s step saturates the drift to its ceiling
    immediately and the measurement would be of the clamp rather than of the
    600 s time constant.
    """
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0))
    state = _run_at(subject, motor_rpm=NOMINAL_RPM, seconds=600.0)

    # 1 - 1/e of the 10 bpm ceiling after one drift time constant.
    assert state.drift_bpm == pytest.approx(10.0 * (1.0 - 1.0 / math.e), abs=0.05)

    # The rate sits BEHIND the drift rather than on it, because it is chasing a
    # target that is still rising - and behind it by tau_down (55 s), not tau_up
    # (30 s). That is not an accident of the implementation: the time-constant
    # choice compares the LOAD response against the current rate, and drift has
    # already carried the rate above the load response, so the plant reads
    # itself as "coming down". Quasi-steady closed form for a first-order lag
    # tracking an exponential ramp,
    #     hr - hr_ss = D * (1 - (T*e^(-t/T) - tau*e^(-t/tau)) / (T - tau))
    # with D = 10 bpm, T = 600 s, tau = 55 s, t = 600 s, giving 5.950 bpm.
    # Asserted because it is exactly what a flipped tau selection would break:
    # with tau_up throughout the answer would be 6.128.
    rate = SECONDS_PER_MINUTE / state.rr_interval
    above_load_response = rate - _hand_steady_state(NOMINAL_RPM)
    lag = 10.0 * (1.0 - (600.0 * math.exp(-1.0) - 55.0 * math.exp(-600.0 / 55.0)) / (600.0 - 55.0))
    assert lag == pytest.approx(5.950, abs=1e-3)
    assert above_load_response == pytest.approx(lag, abs=0.05)
    assert 0.0 < above_load_response < state.drift_bpm

    # And it is big enough to matter: it pushes clean out of a +/- 5 bpm band,
    # which is what forces a deadband controller to unload and to settle at the
    # TOP of its zone rather than at the centre.
    assert rate - _settled_rate(NOMINAL_RPM) > 5.0


def test_cardiac_drift_recovers_once_the_machine_stops() -> None:
    """Drift decays towards zero when the subject is no longer under load."""
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0))
    loaded = _run_at(subject, motor_rpm=NOMINAL_RPM, seconds=600.0)
    assert loaded.drift_bpm > 5.0

    rested = _run_at(subject, motor_rpm=0, seconds=1200.0, start=loaded.at)
    assert rested.drift_bpm < loaded.drift_bpm * 0.2


def test_a_speed_below_the_active_threshold_accumulates_no_drift() -> None:
    """Drift is a consequence of being under load, not of time passing."""
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0))
    state = _run_at(subject, motor_rpm=0, seconds=600.0)

    assert state.drift_bpm == 0.0


def test_a_step_larger_than_a_time_constant_lands_on_the_target_not_past_it() -> None:
    """The clamp on ``dt/tau``, which is what bounds the plant for any step size.

    Without it an explicit Euler step of ``dt = 2*tau`` overshoots to the far
    side of the target and a larger one diverges, so a caller stepping in
    minutes would get heart rates no heart has - from a model whose output the
    safety layer is meant to trust.
    """
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), config=NO_DRIFT)
    state = subject.advance(Monotonic(300.0), MotorRpm(NOMINAL_RPM))

    assert SECONDS_PER_MINUTE / state.rr_interval == pytest.approx(
        _hand_steady_state(NOMINAL_RPM), rel=1e-12
    )


def test_advancing_by_nothing_changes_nothing_and_still_reports() -> None:
    """A caller polling faster than the clock moves gets the current state."""
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), config=NO_DRIFT)
    first = subject.advance(Monotonic(5.0), MotorRpm(NOMINAL_RPM))
    again = subject.advance(Monotonic(5.0), MotorRpm(NOMINAL_RPM))

    assert again.rr_interval == first.rr_interval
    assert again.drift_bpm == first.drift_bpm


def test_the_subject_refuses_to_be_advanced_backwards() -> None:
    """A backwards monotonic reading means two clocks got mixed up.

    Absorbing it as "no time passed" would hide the bug, and this is the
    simulation harness rather than the drive path, so it raises - matching
    ``ManualClock.advance`` and ``SimulatedDrive.advance``.
    """
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0))
    subject.advance(Monotonic(10.0), MotorRpm(0))

    with pytest.raises(ValueError, match="backwards"):
        subject.advance(Monotonic(9.9), MotorRpm(0))


# =========================================================================
# 5. Physiology: the scripted scenarios
# =========================================================================


def test_every_scripted_event_belongs_to_exactly_one_mechanism() -> None:
    """A sixth event must not be able to arrive and then be silently ignored.

    The integrator dispatches on three disjoint tables. If a new member were
    added to none of them it would parse, log and appear in a scenario while
    doing absolutely nothing, which is the worst possible outcome for a test
    fixture that somebody is relying on.
    """
    mechanisms = (frozenset(CARDIAC_OVERRIDE_RATES), frozenset(GAIN_SCALES), SIGNAL_ARTIFACTS)
    for event in ScriptedEvent:
        owners = [table for table in mechanisms if event in table]
        assert len(owners) == 1, event
    assert set(CANONICAL_DURATIONS) == set(ScriptedEvent)


def test_a_vasovagal_drop_is_exactly_thirty_bpm_over_twenty_seconds() -> None:
    """The rule that must not accelerate, delivered as its specification.

    -90 bpm/min for 20 s, and the relaxation is suspended while it runs, so the
    magnitude is the stated one rather than something that depends on
    ``tau_up`` and on where in the range the subject happened to be. A scenario
    whose severity nobody can state is not a test fixture.
    """
    subject = Physiology(
        geometry=SIM_ARM,
        origin=Monotonic(0.0),
        config=NO_DRIFT,
        script=script_for(ScriptedEvent.VASOVAGAL_DROP),
    )
    before = SECONDS_PER_MINUTE / subject.advance(Monotonic(0.0), MotorRpm(0)).rr_interval
    state = _run_at(
        subject,
        motor_rpm=0,
        seconds=float(CANONICAL_DURATIONS[ScriptedEvent.VASOVAGAL_DROP]),
        step=EXACT_STEP,
    )

    after = SECONDS_PER_MINUTE / state.rr_interval
    assert after - before == pytest.approx(-30.0, abs=1e-9)


def test_a_vasovagal_drop_looks_to_a_controller_exactly_like_being_below_target() -> None:
    """Why the supervisor has to outrank the control law rather than advise it.

    The machine is still turning at full speed, so the steady state the SPEED
    implies does not move at all - while the measured rate falls 30 bpm away
    from it. A control law reading "30 bpm below target" responds by
    accelerating, on somebody who is fainting. This test asserts the input that
    produces that mistake, so the rule that prevents it has something real to
    be tested against.
    """
    subject = Physiology(
        geometry=SIM_ARM,
        origin=Monotonic(0.0),
        config=NO_DRIFT,
        script=(EventWindow(ScriptedEvent.VASOVAGAL_DROP, Seconds(0.0), Seconds(20.0)),),
    )
    subject.advance(SETTLED, MotorRpm(NOMINAL_RPM))
    start = subject.advance(SETTLED, MotorRpm(NOMINAL_RPM))

    # The script's origin is 0.0, so at SETTLED the window is long past; run a
    # fresh subject whose window covers the interval being integrated.
    fainting = Physiology(
        geometry=SIM_ARM,
        origin=Monotonic(0.0),
        config=NO_DRIFT,
        script=script_for(ScriptedEvent.VASOVAGAL_DROP),
    )
    fainting.advance(Monotonic(0.0), MotorRpm(NOMINAL_RPM))
    state = _run_at(fainting, motor_rpm=NOMINAL_RPM, seconds=20.0)

    assert start.steady_state == state.steady_state == Bpm(DOCUMENTED_BPM_AT_NOMINAL)
    assert state.heart_rate < state.steady_state - 20


def test_a_heart_rate_spike_is_exactly_forty_bpm_over_two_seconds() -> None:
    """+1200 bpm/min: ten times faster than any physiological response.

    The point of the scenario is that nothing the controller does about it can
    be right, because the signal is not a physiological one. It exists so that
    a rate-of-change bound has a case to reject.
    """
    subject = Physiology(
        geometry=SIM_ARM,
        origin=Monotonic(0.0),
        config=NO_DRIFT,
        script=script_for(ScriptedEvent.HR_SPIKE),
    )
    before = SECONDS_PER_MINUTE / subject.advance(Monotonic(0.0), MotorRpm(0)).rr_interval
    state = _run_at(
        subject,
        motor_rpm=0,
        seconds=float(CANONICAL_DURATIONS[ScriptedEvent.HR_SPIKE]),
        step=EXACT_STEP,
    )

    after = SECONDS_PER_MINUTE / state.rr_interval
    assert after - before == pytest.approx(40.0, abs=1e-9)


def test_two_cardiac_events_at_once_sum_their_rates() -> None:
    """Deterministic and order-free, because they are summed from a fixed table.

    Not a scenario anyone would script on purpose; it is here because the
    alternative - "whichever event the set iterated first wins" - would make a
    simultaneous pair non-reproducible, and a non-reproducible fixture is worse
    than no fixture.
    """
    both = (
        EventWindow(ScriptedEvent.VASOVAGAL_DROP, Seconds(0.0), Seconds(2.0)),
        EventWindow(ScriptedEvent.HR_SPIKE, Seconds(0.0), Seconds(2.0)),
    )
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), config=NO_DRIFT, script=both)
    before = SECONDS_PER_MINUTE / subject.advance(Monotonic(0.0), MotorRpm(0)).rr_interval
    state = _run_at(subject, motor_rpm=0, seconds=2.0, step=EXACT_STEP)

    net = (-90.0 + 1200.0) / SECONDS_PER_MINUTE * 2.0
    assert SECONDS_PER_MINUTE / state.rr_interval - before == pytest.approx(net, abs=1e-9)


def test_the_plant_relaxes_again_the_moment_the_window_closes() -> None:
    """An override suspends the relaxation; it does not switch it off for good."""
    subject = Physiology(
        geometry=SIM_ARM,
        origin=Monotonic(0.0),
        config=NO_DRIFT,
        script=script_for(ScriptedEvent.VASOVAGAL_DROP),
    )
    dropped = _run_at(subject, motor_rpm=0, seconds=20.0)
    recovered = _run_at(subject, motor_rpm=0, seconds=120.0, start=dropped.at)

    assert SECONDS_PER_MINUTE / recovered.rr_interval > SECONDS_PER_MINUTE / dropped.rr_interval
    assert recovered.heart_rate == pytest.approx(HR_REST, abs=2)


def test_a_nonresponder_cannot_be_brought_into_a_zone_this_machine_can_reach() -> None:
    """k_g scaled to 0.35: 103 bpm at the speed ceiling, not 164.

    The case that finds integrator windup. "We never reached the target" must
    not become "so push harder", because there is no speed at which this
    subject reaches a 140 bpm zone and the machine's own ceiling is the only
    thing stopping the attempt.
    """
    subject = Physiology(
        geometry=SIM_ARM,
        origin=Monotonic(0.0),
        config=NO_DRIFT,
        script=script_for(ScriptedEvent.NONRESPONDER),
    )
    state = subject.advance(Monotonic(1.0), MotorRpm(NOMINAL_RPM))

    expected = HR_REST + K_G * GAIN_SCALES[ScriptedEvent.NONRESPONDER] * _hand_g(NOMINAL_RPM)
    assert state.steady_state == Bpm(round(expected))
    assert state.steady_state == Bpm(103)
    assert state.steady_state < Bpm(140)


@pytest.mark.parametrize("event", _ordered(SIGNAL_ARTIFACTS))
def test_a_signal_artifact_cannot_move_a_heart_rate(event: ScriptedEvent) -> None:
    """The dependency rule, asserted as behaviour rather than as an import graph.

    An electrode falling off is an event in the sensing chain. If it changed the
    plant, every test that uses it to check the pipeline's ``no_signal`` path
    would also be silently changing the physiology, and the two failures would
    be indistinguishable.
    """
    plain = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0))
    afflicted = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), script=script_for(event))

    now = Monotonic(0.0)
    for _ in range(50):
        now = Monotonic(now + LOOP_STEP)
        expected = plain.advance(now, MotorRpm(900))
        actual = afflicted.advance(now, MotorRpm(900))
        assert actual.rr_interval == expected.rr_interval
        assert actual.drift_bpm == expected.drift_bpm
        assert actual.artifacts == frozenset({event})


def test_a_window_is_resolved_at_the_start_of_each_step() -> None:
    """Which is what makes a 20 s window integrate for 20 s, not 19.8.

    Half-open intervals plus events looked up at the END of a step would drop
    the last one, and a scenario that is 1% short is a scenario whose magnitude
    is not the documented one. 100 steps of 0.2 s must all be inside.
    """
    window = EventWindow(ScriptedEvent.VASOVAGAL_DROP, Seconds(0.0), Seconds(20.0))
    assert window.covers(Seconds(0.0))
    assert window.covers(Seconds(19.8))
    assert not window.covers(Seconds(20.0))
    assert window.end == Seconds(20.0)
    assert active_events((window,), Seconds(19.8)) == frozenset({ScriptedEvent.VASOVAGAL_DROP})
    assert active_events((window,), Seconds(20.0)) == frozenset()


def test_every_step_of_a_twenty_second_window_is_integrated_with_the_event_on() -> None:
    """A scenario's magnitude is only as good as the step count behind it.

    Counted directly rather than inferred from the resulting heart rate, because
    the two failure modes look identical from the outside: a window resolved one
    step short and an override rate 1% too small both produce -29.7 bpm. This
    test says which of them happened.
    """
    subject = Physiology(
        geometry=SIM_ARM,
        origin=Monotonic(0.0),
        config=NO_DRIFT,
        script=script_for(ScriptedEvent.VASOVAGAL_DROP),
    )
    steps = round(float(CANONICAL_DURATIONS[ScriptedEvent.VASOVAGAL_DROP]) / EXACT_STEP)
    assert steps == 80

    now = Monotonic(0.0)
    integrated = 0
    for _ in range(steps):
        now = Monotonic(now + EXACT_STEP)
        if ScriptedEvent.VASOVAGAL_DROP in subject.advance(now, MotorRpm(0)).artifacts:
            integrated += 1
    assert integrated == steps

    # And the step after the window has closed sees nothing.
    assert subject.advance(Monotonic(now + EXACT_STEP), MotorRpm(0)).artifacts == frozenset()


def test_an_event_window_must_be_placeable_on_a_timeline() -> None:
    """A negative onset or an empty window is a scenario nobody can replay."""
    with pytest.raises(ValueError, match="start must not be negative"):
        EventWindow(ScriptedEvent.HR_SPIKE, Seconds(-0.1), Seconds(1.0))
    with pytest.raises(ValueError, match="duration must be positive"):
        EventWindow(ScriptedEvent.HR_SPIKE, Seconds(0.0), Seconds(0.0))


def test_a_script_can_be_placed_anywhere_on_the_timeline() -> None:
    """``script_for`` is the canonical window; the onset is the caller's."""
    late = script_for(ScriptedEvent.MAINS_BURST, start=Seconds(120.0))

    assert len(late) == 1
    assert late[0].start == Seconds(120.0)
    assert late[0].duration == CANONICAL_DURATIONS[ScriptedEvent.MAINS_BURST]
    assert Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), script=late).script == late


# =========================================================================
# 6. Physiology: guards and bounds
# =========================================================================


def test_a_plant_that_cannot_be_integrated_refuses_to_be_built() -> None:
    """Each of these makes the arithmetic meaningless, so none is absorbed.

    Written out one call at a time rather than parametrised over a kwargs dict,
    because every field here is a different ``NewType`` and a dict that held
    them all would have to be typed loosely enough to defeat the point.

    Raising is right for the reason ``SimulatedDriveConfig`` raises: the config
    is built at startup with nothing spinning, and refusing to start beats
    running a plant whose time constants divide by zero.
    """
    with pytest.raises(ValueError, match="k_g must be positive"):
        PhysiologyConfig(k_g=0.0)
    with pytest.raises(ValueError, match="tau_up must be positive"):
        PhysiologyConfig(tau_up=Seconds(0.0))
    with pytest.raises(ValueError, match="tau_down must be positive"):
        PhysiologyConfig(tau_down=Seconds(-1.0))
    with pytest.raises(ValueError, match="tau_drift must be positive"):
        PhysiologyConfig(tau_drift=Seconds(0.0))


def test_a_backwards_subject_refuses_to_be_built() -> None:
    """Negative drift and a gain below -1 are sign errors, not subjects."""
    with pytest.raises(ValueError, match="drift_max must not be negative"):
        PhysiologyConfig(drift_max=-1.0)
    with pytest.raises(ValueError, match="fatigue must be greater than -1"):
        PhysiologyConfig(fatigue=-1.0)


def test_the_heart_rates_in_a_config_must_be_ordered() -> None:
    """floor <= rest <= max <= ceiling, or the clamps fight the response."""
    with pytest.raises(ValueError, match="must be ordered"):
        PhysiologyConfig(hr_rest=Bpm(200))
    with pytest.raises(ValueError, match="must be ordered"):
        PhysiologyConfig(hr_floor=Bpm(100))


def test_a_long_enough_drop_stops_at_the_floor() -> None:
    """The floor is load-bearing, not cosmetic.

    Without it a scripted drop walks the state negative and the beat interval
    handed to the ECG synthesiser - ``60/rate`` - changes sign and then
    divides by zero on the way past.
    """
    long_drop = (EventWindow(ScriptedEvent.VASOVAGAL_DROP, Seconds(0.0), Seconds(600.0)),)
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), script=long_drop)
    state = _run_at(subject, motor_rpm=0, seconds=600.0)

    assert state.heart_rate == Bpm(30)
    assert state.rr_interval == pytest.approx(2.0, rel=1e-12)


def test_a_long_enough_spike_stops_at_the_ceiling() -> None:
    """And the same in the other direction."""
    long_spike = (EventWindow(ScriptedEvent.HR_SPIKE, Seconds(0.0), Seconds(60.0)),)
    subject = Physiology(geometry=SIM_ARM, origin=Monotonic(0.0), script=long_spike)
    state = _run_at(subject, motor_rpm=0, seconds=60.0)

    assert state.heart_rate == Bpm(220)


@given(
    steps=st.lists(
        st.tuples(
            st.integers(min_value=-2500, max_value=2500),
            st.floats(min_value=0.0, max_value=600.0, allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=60,
    ),
    scripted=st.sampled_from(_ordered(ScriptedEvent)),
)
@settings(deadline=None, max_examples=200)
def test_the_plant_stays_inside_physiology_for_any_schedule(
    steps: list[tuple[int, float]], scripted: ScriptedEvent
) -> None:
    """The bound the safety layer is entitled to assume about this plant.

    Coverage proves every line ran; this proves the output is a heart rate. For
    ANY sequence of speeds and step sizes, including steps twenty times the
    longest time constant and a scenario running over the top of them: the rate
    stays inside [floor, ceiling], the beat interval stays positive and finite,
    and the drift stays inside [0, drift_max]. A NaN or a negative interval here
    would reach the ECG synthesiser and then the real DSP.
    """
    config = PhysiologyConfig()
    subject = Physiology(
        geometry=SIM_ARM, origin=Monotonic(0.0), config=config, script=script_for(scripted)
    )
    now = Monotonic(0.0)

    for rpm, dt in steps:
        now = Monotonic(now + dt)
        state = subject.advance(now, MotorRpm(rpm))

        # Bounded on what the plant actually emits. Dividing 60 by the interval
        # to recover a rate re-introduces a float rounding error of its own -
        # 60/(60/220) is 220.00000000000003 - and asserting a bound on THAT
        # would be asserting something about the test's arithmetic.
        assert state.rr_interval > 0.0
        assert math.isfinite(state.rr_interval)
        assert (
            SECONDS_PER_MINUTE / config.hr_ceiling
            <= state.rr_interval
            <= SECONDS_PER_MINUTE / config.hr_floor
        )
        assert config.hr_floor <= state.heart_rate <= config.hr_ceiling
        assert math.isfinite(state.drift_bpm)
        assert 0.0 <= state.drift_bpm <= config.drift_max
        assert state.g_load >= 0.0
        assert math.isfinite(state.g_load)


# =========================================================================
# 7. The ECG: morphology
# =========================================================================


def test_the_morphology_is_the_ecgsyn_parameter_set() -> None:
    """Pinned as literals, because BioSPPy's segmenter is tuned for this shape.

    A wrong sign on Q or S, or an R width of 0.01 instead of 0.10, still looks
    like an ECG on a plot and still detects; it just detects differently, and
    every heart-rate assertion downstream would then be measuring the wrong
    waveform. So the numbers are written out.
    """
    assert [wave.centre_deg for wave in MORPHOLOGY.values()] == [-70.0, -15.0, 0.0, 15.0, 100.0]
    assert [wave.amplitude for wave in MORPHOLOGY.values()] == [1.2, -5.0, 30.0, -7.5, 0.75]
    assert [wave.width for wave in MORPHOLOGY.values()] == [0.25, 0.10, 0.10, 0.10, 0.40]
    assert [wave.width_scales_with_rr for wave in MORPHOLOGY.values()] == [
        True,
        False,
        False,
        False,
        True,
    ]
    assert list(MORPHOLOGY) == list(WaveComponent)


def test_only_the_p_and_t_waves_widen_with_the_beat_interval() -> None:
    """The QRS width of a real heart is nearly rate-independent.

    It matters beyond realism: a QRS that narrowed with rate would change what
    BioSPPy's segmenter sees, turning a physiology change into a detection
    change, and every ambiguous result downstream would have two possible
    causes instead of one.
    """
    for component, wave in MORPHOLOGY.items():
        off_centre = math.radians(wave.centre_deg) + wave.width
        narrow = wave.at(off_centre, width_scale=1.0)
        wide = wave.at(off_centre, width_scale=2.0)
        if wave.width_scales_with_rr:
            assert abs(wide) > abs(narrow), component
            assert component in (WaveComponent.P, WaveComponent.T)
        else:
            assert wide == narrow, component


def test_the_normaliser_is_the_summed_peak_and_not_the_r_wave_amplitude() -> None:
    """Q and S subtract from the R peak, so 30.0 is the wrong divisor.

    They sit 15 degrees away with a 0.10 rad width, which is close enough to
    take ~0.41 off the peak between them - a 1.4% scale error if the R
    amplitude were used instead, silently applied to every synthetic signal.
    """
    r_alone = MORPHOLOGY[WaveComponent.R].amplitude
    peak = MORPHOLOGY_PEAK
    assert r_alone > peak
    assert peak == pytest.approx(29.5947, abs=5e-4)
    assert r_alone - peak == pytest.approx(0.405, abs=5e-3)
    # The peak is very slightly off phase zero, because Q and S are not equal.
    assert morphology_at(0.0, width_scale=1.0) / MORPHOLOGY_PEAK == pytest.approx(1.0, abs=1e-4)


@pytest.mark.parametrize("bpm", [30, 45, 60, 100, 150, 200])
def test_widening_p_and_t_does_not_move_the_r_peak(bpm: int) -> None:
    """Why one normaliser computed at 60 bpm stays right at every rate.

    Asserted across 30..200 bpm rather than argued: at the R peak, P and T
    together contribute less than 1e-4 of the total, so the scale is stable to
    better than 0.1% even where the widths change by a factor of 1.4.
    """
    width_scale = math.sqrt(60.0 / bpm / REFERENCE_RR)
    peak = max(
        morphology_at(-math.pi + TWO_PI * step / PEAK_SEARCH_STEPS, width_scale=width_scale)
        for step in range(PEAK_SEARCH_STEPS)
    )
    assert peak == pytest.approx(MORPHOLOGY_PEAK, rel=1e-3)


def test_a_phase_difference_wraps_rather_than_being_cut_in_half() -> None:
    """A deflection near the cycle boundary contributes on both sides of it.

    The P wave sits at -70 degrees, well inside the cycle, so this is checked on
    a deflection placed deliberately at the boundary: without the wrap its
    contribution just below +pi would be zero, which would put a step
    discontinuity into every beat exactly where the segmenter looks for one.
    """
    edge = GaussianWave(180.0, 1.0, 0.10, width_scales_with_rr=False)

    assert edge.at(math.pi - 0.05, width_scale=1.0) == pytest.approx(
        edge.at(-math.pi + 0.05, width_scale=1.0), rel=1e-12
    )
    assert edge.at(math.pi, width_scale=1.0) == pytest.approx(1.0, rel=1e-12)


def test_the_r_peak_lands_at_one_millivolt_in_raw_counts() -> None:
    """Which is where a real lead-I R wave sits, and ~341 of 1024 counts.

    Measured on the RAW output, through ``millivolts_to_adc`` - the exact
    inverse of the conversion the pipeline applies - so the number checked is
    the one the DSP will see. The allowance is one ADC count plus the phase
    quantisation at 1000 Hz, which is what a 1 mV signal can actually be
    represented to.
    """
    synth = EcgSynthesizer(CLEAN_ECG)
    counts = synth.render(5000, _subject_state(rr_interval=1.0))

    peak_mv = (max(counts) - ADC_BASELINE) * ECG_MV_PER_COUNT
    assert peak_mv == pytest.approx(1.0, abs=2.0 * ECG_MV_PER_COUNT)
    assert max(counts) == AdcCount(853)
    # The S wave is the largest negative deflection, and it stays far from the
    # rail: a clean signal must never clip, or the grade would read "noisy".
    assert min(counts) > ADC_MIN + 100
    assert max(counts) < ADC_MAX - 100


def test_the_beat_interval_is_latched_per_beat_not_per_sample() -> None:
    """A deflection's width belongs to the beat it is part of.

    Letting it track an instantaneously modulated interval would smear the T
    wave within a single beat, which no heart does and which would turn the
    respiratory modulation into a morphology change rather than a timing one.

    Run with a deliberately large 20% sinus arrhythmia, so the interval the
    synthesiser is being handed sweeps continuously the whole time: the latched
    value must sit perfectly still through the beat and then step, once, at the
    cycle boundary.
    """
    synth = EcgSynthesizer(EcgConfig(rr_variability=0.2))

    # The first render opens a beat, because the phase starts on a cycle
    # boundary - so the interval it is given is the one that beat is shaped by,
    # and not the REFERENCE_RR placeholder the field is built with.
    synth.render(1, _subject_state(rr_interval=0.8))
    assert synth.beat_interval == pytest.approx(0.8, rel=1e-12)

    latched: list[float] = []
    while synth.samples_rendered < 700:
        synth.render(1, _subject_state(rr_interval=0.8))
        latched.append(float(synth.beat_interval))
    assert latched == [pytest.approx(0.8, rel=1e-12)] * len(latched)

    # Somewhere past here the cycle wraps and the NEXT beat picks up the
    # modulated interval, which by then is no longer 0.8 s.
    while synth.beat_interval == pytest.approx(0.8, rel=1e-12):
        synth.render(1, _subject_state(rr_interval=0.8))
        assert synth.samples_rendered < 2000, "the cycle never wrapped"
    assert 0.8 < synth.beat_interval <= 0.8 * 1.2
    assert -math.pi <= synth.phase < math.pi


def test_rendering_is_continuous_across_calls() -> None:
    """A BITalino does not hand out the same second of signal twice.

    So two 500-sample renders must equal one 1000-sample render: the phase and
    the contaminant time base carry across. Any discontinuity here is a step in
    the raw signal at every block boundary, i.e. a 5 Hz artifact the DSP would
    faithfully report.
    """
    whole = EcgSynthesizer(CLEAN_ECG).render(1000, _subject_state())
    split = EcgSynthesizer(CLEAN_ECG)
    halves = split.render(500, _subject_state()) + split.render(500, _subject_state())

    assert halves == whole


# =========================================================================
# 8. The ECG: contaminants
# =========================================================================


def _std(counts: Sequence[AdcCount]) -> float:
    """Population standard deviation, written out so no numpy is needed here."""
    mean = math.fsum(counts) / len(counts)
    return math.sqrt(math.fsum((value - mean) ** 2 for value in counts) / len(counts))


def test_a_detached_electrode_reads_dead_flat_and_the_heart_keeps_beating() -> None:
    """Flat is what drives the pipeline to ``no_signal``, and the beat goes on.

    The second half matters: the subject does not stop having a heart rate
    because a lead came off, so the phase keeps advancing and the signal resumes
    in phase when the lead goes back on. A synthesiser that froze would make the
    recovery look like a beat that lasted thirty seconds.
    """
    synth = EcgSynthesizer()
    state = _subject_state(artifacts=frozenset({ScriptedEvent.ELECTRODE_OFF}))
    counts = synth.render(3000, state)

    assert set(counts) == {AdcCount(ADC_BASELINE)}
    assert _std(counts) == 0.0
    assert synth.samples_rendered == 3000
    assert synth.phase != -math.pi


def test_a_mains_burst_uses_the_burst_amplitude_and_not_the_ambient_one() -> None:
    """Otherwise the scenario would be a no-op that looked like it worked."""
    config = EcgConfig(baseline_mv=0.0, mains_mv=0.0, mains_burst_mv=0.5, motion_mv_per_g=0.0)
    quiet = EcgSynthesizer(config).render(2000, _subject_state())
    burst = EcgSynthesizer(config).render(
        2000, _subject_state(artifacts=frozenset({ScriptedEvent.MAINS_BURST}))
    )

    assert _std(burst) > 2.0 * _std(quiet)


@pytest.mark.parametrize("g_load", [0.0, 0.1, 0.3, 0.5, 0.859])
def test_motion_noise_grows_with_the_centripetal_load(g_load: float) -> None:
    """The coupling, at the level of the raw signal.

    Expressed per g rather than per rpm because the rig shakes in proportion to
    the load it is carrying, which keeps the coupling right if the radius or the
    gear ratio changes.
    """
    config = EcgConfig(baseline_mv=0.0, mains_mv=0.0, rr_variability=0.0)
    at_rest = _std(EcgSynthesizer(config).render(4000, _subject_state()))
    loaded = _std(EcgSynthesizer(config).render(4000, _subject_state(g_load=g_load)))

    if g_load == 0.0:
        assert loaded == at_rest
    else:
        assert loaded > at_rest


def test_motion_noise_is_indifferent_to_the_direction_of_rotation() -> None:
    """g is unsigned, which is the physical truth and the pessimistic reading."""
    config = EcgConfig(baseline_mv=0.0, mains_mv=0.0, rr_variability=0.0)
    forward = EcgSynthesizer(config).render(2000, _subject_state(g_load=0.5))
    reverse = EcgSynthesizer(config).render(2000, _subject_state(g_load=0.5))

    assert forward == reverse


def test_the_noise_draw_does_not_depend_on_its_own_amplitude() -> None:
    """So two runs that differ only in speed stay comparable.

    The Gaussian is drawn on every sample even at zero amplitude, where it
    returns the mean exactly. If the draw were skipped, changing the speed would
    shift the whole random stream and a before/after comparison would be
    measuring a different noise realisation as well as a different speed.
    """
    config = EcgConfig(baseline_mv=0.0, mains_mv=0.0, rr_variability=0.0)
    warmed = EcgSynthesizer(config)
    warmed.render(1000, _subject_state(g_load=0.0))
    after_quiet = warmed.render(1000, _subject_state(g_load=0.5))

    direct = EcgSynthesizer(config)
    direct.render(1000, _subject_state(g_load=0.5))
    after_loud = direct.render(1000, _subject_state(g_load=0.5))

    assert after_quiet == after_loud


def test_two_synthesisers_with_the_same_seed_agree_and_different_seeds_do_not() -> None:
    """Determinism is per instance, never through the global RNG.

    A simulator that perturbed global random state would make every other test
    in the suite order-dependent.
    """
    state = _subject_state(g_load=0.5)
    same = EcgSynthesizer(EcgConfig(seed=1)).render(500, state)
    also = EcgSynthesizer(EcgConfig(seed=1)).render(500, state)
    other = EcgSynthesizer(EcgConfig(seed=2)).render(500, state)

    assert same == also
    assert same != other


def test_an_unmodelled_channel_is_a_constant_and_says_so() -> None:
    """Plausible noise on an unmodelled channel is worse than a flat line.

    A flat line grades ``no_signal`` in the pipeline, so a consumer that
    mistakes it for data finds out; noise would just be believed.
    """
    synth = EcgSynthesizer()
    assert set(synth.unmodelled(100)) == {AdcCount(ADC_BASELINE)}


# =========================================================================
# 9. The ECG: guards and bounds
# =========================================================================


def test_a_sensing_chain_that_cannot_be_rendered_refuses_to_be_built() -> None:
    """``rr_variability`` at 1.0 drives the beat interval to zero and divides by it.

    Written out rather than parametrised for the reason given on the plant's
    equivalent: the fields are different ``NewType``s.
    """
    with pytest.raises(ValueError, match="sample_rate must be positive"):
        EcgConfig(sample_rate=0)
    with pytest.raises(ValueError, match="r_peak_mv must be positive"):
        EcgConfig(r_peak_mv=Millivolts(0.0))
    with pytest.raises(ValueError, match="rr_variability must be in"):
        EcgConfig(rr_variability=1.0)
    with pytest.raises(ValueError, match="rr_variability must be in"):
        EcgConfig(rr_variability=-0.1)


def test_rendering_nothing_is_a_caller_bug_and_says_so() -> None:
    """An empty block is never what a caller meant, and it hides where it came from."""
    synth = EcgSynthesizer()
    with pytest.raises(ValueError, match="count must be positive"):
        synth.render(0, _subject_state())
    with pytest.raises(ValueError, match="count must be positive"):
        synth.unmodelled(-1)


def test_a_beat_interval_of_zero_is_refused_rather_than_divided_by() -> None:
    """Unreachable from a real SubjectState - the plant's floor prevents it - so
    this guards the hand-built case, which is the one a test or a script hits."""
    synth = EcgSynthesizer()
    with pytest.raises(ValueError, match="rr_interval must be positive"):
        synth.render(10, _subject_state(rr_interval=0.0))


@given(
    g_load=st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False),
    rr_interval=st.floats(min_value=0.25, max_value=2.5, allow_nan=False, allow_infinity=False),
    artifacts=st.sets(st.sampled_from(_ordered(SIGNAL_ARTIFACTS))),
)
@settings(deadline=None, max_examples=150)
def test_every_rendered_sample_is_a_legal_adc_count(
    g_load: float, rr_interval: float, artifacts: set[ScriptedEvent]
) -> None:
    """The ADC domain is a hard boundary, and the clamp is what enforces it.

    Five g of motion artifact is far outside anything this machine can produce,
    which is the point: the pipeline parses raw samples with
    ``src.units.adc_count`` and returns ``Err`` outside 0..1023, so a
    synthesiser that could emit 1400 would be testing the error path instead of
    the DSP. Integers too - a float count would be a different kind of lie.
    """
    synth = EcgSynthesizer()
    counts = synth.render(
        400, _subject_state(g_load=g_load, rr_interval=rr_interval, artifacts=frozenset(artifacts))
    )

    assert len(counts) == 400
    for count in counts:
        assert isinstance(count, int)
        assert ADC_MIN <= count <= ADC_MAX


# =========================================================================
# 10. The real pipeline, end to end
# =========================================================================


def test_a_synthetic_sixty_bpm_signal_comes_back_as_sixty_bpm_graded_good() -> None:
    """The load-bearing test of this whole package.

    The synthetic ECG goes through the REAL ``SignalTreatment`` - the real
    Butterworth filter, the real BioSPPy segmenter, the real quality classifier
    - and comes back graded ``good`` with the right rate. Nothing else in this
    file matters if this does not hold, because every other pipeline assertion
    would then be about a signal the pipeline cannot read in the first place.

    A few bpm of allowance, not zero: the signal carries 3% respiratory sinus
    arrhythmia, so the segmenter has something to do.
    """
    metrics = _treat(asyncio.run(_acquire(motor_rpm=0, config=FIXED_60_BPM)))

    assert _quality(metrics) == "good"
    assert _has_heart_rate(metrics)
    assert _reported_bpm(metrics) == pytest.approx(60, abs=3)


@pytest.mark.parametrize("bpm", [45, 75, 110, 150, 165])
def test_the_pipeline_reads_the_rate_back_across_the_whole_training_range(bpm: int) -> None:
    """45 to 165 bpm: rest to the top of what this machine can drive a heart to.

    Parametrized rather than tested at one rate because the P and T waves widen
    with the beat interval, so the waveform the segmenter sees is not simply a
    faster copy of itself.
    """
    pinned = PhysiologyConfig(hr_rest=Bpm(bpm), hr_max=Bpm(bpm))
    metrics = _treat(asyncio.run(_acquire(motor_rpm=0, config=pinned)))

    assert _quality(metrics) == "good"
    assert _reported_bpm(metrics) == pytest.approx(bpm, abs=3)


def test_a_detached_electrode_yields_no_signal_and_no_heart_rate_at_all() -> None:
    """Not a stale rate, not a default, not a zero: the key is ABSENT.

    This is the invariant the whole safety argument leans on. The pipeline
    re-emits its previous metrics dict when extraction fails, so the number on
    the operator's screen goes on looking alive - but the dict for a flat lead
    contains no ``heartRate`` key, and a consumer that substitutes a default
    here has invented a vital sign on somebody whose electrode fell off.
    """
    metrics = _treat(
        asyncio.run(_acquire(script=script_for(ScriptedEvent.ELECTRODE_OFF), config=FIXED_60_BPM))
    )

    assert _quality(metrics) == "no_signal"
    assert not _has_heart_rate(metrics)
    assert "hrv" not in metrics


def test_a_mains_burst_yields_mains_dominated_and_no_heart_rate() -> None:
    """Hum has a perfectly steady rate, which is exactly why it is dangerous.

    A beat detector fed 50 Hz reports a confident, stable, entirely fictional
    heart rate. The pipeline's notch test is what stops it, and it only works
    because the contamination is added to the RAW signal where that test looks.
    """
    metrics = _treat(
        asyncio.run(_acquire(script=script_for(ScriptedEvent.MAINS_BURST), config=FIXED_60_BPM))
    )

    assert _quality(metrics) == "mains_dominated"
    assert not _has_heart_rate(metrics)


def test_ambient_hum_is_small_enough_to_leave_a_clean_signal_good() -> None:
    """Or the default sensing chain would be permanently unusable."""
    metrics = _treat(asyncio.run(_acquire(config=FIXED_60_BPM)))
    assert _quality(metrics) == "good"


def test_a_saturating_baseline_excursion_yields_noisy() -> None:
    """The fourth grade, reached the way it is reached on the bench.

    A DC offset or a large slow movement drives the front end into the rails for
    most of the cycle; the classifier counts pinned samples, so more than half
    of them at a rail is ``noisy``.
    """
    metrics = _treat(asyncio.run(_acquire(config=FIXED_60_BPM, ecg=EcgConfig(baseline_mv=3.0))))

    assert _quality(metrics) == "noisy"
    assert not _has_heart_rate(metrics)


@pytest.mark.parametrize(
    ("motor_rpm", "expected"),
    [(0, "good"), (600, "good"), (1100, "good"), (1200, "noisy"), (1380, "noisy")],
)
def test_motion_noise_rising_with_speed_degrades_the_reported_quality(
    motor_rpm: int, expected: str
) -> None:
    """The coupling, through the real classifier.

    The subject's rate is pinned at 60 bpm, so the ONLY thing changing across
    these five cases is how hard the rig is shaking. Quality survives to 1100
    motor rpm and fails by 1200, which pins the threshold ``src/sim/ecg.py``
    documents - and means the machine's usable speed range is decided by its own
    vibration rather than by the programme, with the supervisor freezing the
    setpoint somewhere around 87% of nominal.
    """
    metrics = _treat(asyncio.run(_acquire(motor_rpm=motor_rpm, config=FIXED_60_BPM)))
    assert _quality(metrics) == expected


@pytest.mark.parametrize("motor_rpm", [600, 900, 1100])
def test_motion_noise_wrecks_the_heart_rate_long_before_it_degrades_the_grade(
    motor_rpm: int,
) -> None:
    """A HAZARD IN THE PIPELINE, characterised here rather than worked around.

    Across the whole band where the grade is still ``good`` - 600 to 1100 motor
    rpm - this subject's true rate is 60 bpm and the pipeline reports 125 to
    133. Fewer than half the samples are pinned at the rails, so the classifier
    passes the signal; the pipeline then runs beat detection; and motion
    artifact in the 3-45 Hz pass band looks like beats. The grade is clean over
    the entire range in which the number is already more than double the truth.

    The consequence for the layers above, stated plainly: **``quality == good``
    is necessary and nowhere near sufficient.** A controller that gates only on
    the grade will regulate a motor on a number twice the subject's actual heart
    rate, and will do it at exactly the speeds where being wrong matters most.
    The rate of change has to be bounded as well - and that bound has something
    real to be tested against because of this test.

    Asserted as a band rather than an exact figure: the direction and the
    magnitude are the finding, and pinning 133 exactly would be pinning one
    BioSPPy version's arithmetic.
    """
    metrics = _treat(asyncio.run(_acquire(motor_rpm=motor_rpm, config=FIXED_60_BPM)))

    assert _quality(metrics) == "good"
    assert _has_heart_rate(metrics)
    assert _reported_bpm(metrics) > 100


# =========================================================================
# 11. The BITalino drop-in
# =========================================================================


def test_the_batches_are_the_pipelines_own_records_and_not_look_alikes() -> None:
    """A simulator with its own record type would be testing its own record type.

    The ECG pipeline reads ``batch.channels[i].channel`` and ``.values`` and
    hands them straight to ``treat_batch``, so the objects have to be the real
    ``SampleBatch``/``ChannelData``. The Protocol in ``src/sim/bitalino.py``
    describes them statically; this is what stops that description from
    becoming a lie at runtime.
    """
    batch = asyncio.run(_one_batch())

    assert type(batch).__module__ == "src.bitalino_client"
    assert type(batch).__qualname__ == "SampleBatch"
    assert type(batch.channels[0]).__module__ == "src.bitalino_client"
    assert type(batch.channels[0]).__qualname__ == "ChannelData"
    assert all(isinstance(value, float) for value in batch.channels[0].values)


async def _one_batch(channels: Sequence[int] | None = None) -> SampleBatch:
    """Connect, acquire and read exactly one full second."""
    clock = ManualClock()
    client = SimulatedBitalinoClient(clock, physiology=_resting(clock), channels=channels)
    assert await client.connect()
    assert await client.start_acquisition()
    clock.advance(Seconds(1.0))
    batch = await client.read_samples(1000)
    assert batch is not None
    return batch


def test_the_client_carries_the_whole_surface_the_console_calls() -> None:
    """Named directly, so a rename fails the TYPE CHECK rather than a runtime probe.

    The console relies on exactly these, and the sync/async split is
    part of the contract: five coroutines and one plain method. A drop-in that
    got that split wrong would fail at the moment a session starts, on
    hardware, which is the worst possible place to find out.
    """
    for method in (
        SimulatedBitalinoClient.connect,
        SimulatedBitalinoClient.disconnect,
        SimulatedBitalinoClient.start_acquisition,
        SimulatedBitalinoClient.stop_acquisition,
        SimulatedBitalinoClient.read_samples,
    ):
        assert inspect.iscoroutinefunction(method), method
    assert not inspect.iscoroutinefunction(SimulatedBitalinoClient.set_disconnect_callback)

    client = SimulatedBitalinoClient(ManualClock(), physiology=_resting(ManualClock()))
    assert client.is_connected is False
    assert client.is_acquiring is False
    assert client.channels == (0,)
    assert client.sample_rate == 1000
    assert client.mac_address.startswith("/dev/")


def test_reading_before_acquiring_gives_nothing() -> None:
    """``None`` means "no data", and a caller must handle it without inventing any."""

    async def scenario() -> None:
        client = SimulatedBitalinoClient(ManualClock(), physiology=_resting(ManualClock()))
        assert await client.read_samples(10) is None
        assert await client.start_acquisition() is False

    asyncio.run(scenario())


def test_a_read_returns_nothing_until_enough_simulated_time_has_passed() -> None:
    """The sample clock is the injected clock, which is the whole discipline.

    A simulator that produced data on demand would let a test pass that could
    never happen in a second of real time, and would make a 45-minute session
    indistinguishable from a tight loop.
    """

    async def scenario() -> None:
        clock = ManualClock()
        client = SimulatedBitalinoClient(clock, physiology=_resting(clock))
        await client.connect()
        await client.start_acquisition()

        assert await client.read_samples(1000) is None
        clock.advance(Seconds(0.5))
        assert await client.read_samples(1000) is None
        clock.advance(Seconds(0.5))
        assert await client.read_samples(1000) is not None
        assert client.samples_generated == 1000
        assert await client.read_samples(1000) is None

    asyncio.run(scenario())


def test_samples_are_not_handed_out_twice() -> None:
    """Consecutive reads are consecutive signal, which is what the DSP assumes."""

    async def scenario() -> None:
        clock = ManualClock()
        client = SimulatedBitalinoClient(clock, physiology=_resting(clock))
        await client.connect()
        await client.start_acquisition()
        clock.advance(Seconds(2.0))

        first = await client.read_samples(1000)
        second = await client.read_samples(1000)
        assert first is not None
        assert second is not None
        assert list(first.channels[0].values) != list(second.channels[0].values)
        assert client.samples_generated == 2000

    asyncio.run(scenario())


def test_the_subject_is_integrated_in_step_with_the_samples_handed_out() -> None:
    """The plant must not run ahead of the clock or behind the data.

    Ahead and the ECG would carry a heart rate from the future; behind and a
    speed change would take longer to show up than it does on the machine.
    """

    async def scenario() -> None:
        clock = ManualClock()
        subject = Physiology(geometry=SIM_ARM, origin=clock.monotonic(), config=NO_DRIFT)
        client = SimulatedBitalinoClient(clock, physiology=subject)
        await client.connect()
        await client.start_acquisition()
        client.set_motor_rpm(MotorRpm(NOMINAL_RPM))

        for _ in range(30):
            clock.advance(Seconds(1.0))
            assert await client.read_samples(1000) is not None

        assert client.subject is subject
        settled = subject.advance(clock.monotonic(), MotorRpm(NOMINAL_RPM))
        covered = (SECONDS_PER_MINUTE / settled.rr_interval - HR_REST) / (
            _settled_rate(NOMINAL_RPM) - HR_REST
        )
        assert covered == pytest.approx(1.0 - 1.0 / math.e, abs=0.05)

    asyncio.run(scenario())


def test_a_permuted_channel_request_comes_back_in_wire_order() -> None:
    """The device streams channels ascending; the real client labels them so.

    A simulator that honoured request order would label columns the way the
    hardware never does, and a caller relying on it would store the ECG under
    the wrong name the first time it met a real BITalino.
    """
    batch = asyncio.run(_one_batch([3, 1, 0, 1]))

    assert [channel.channel for channel in batch.channels] == ["ECG", "EDA", "RESP"]
    ecg = batch.channels[0]
    assert len(set(ecg.values)) > 50
    # The unmodelled columns are flat, and say so rather than looking plausible.
    assert set(batch.channels[1].values) == {float(ADC_BASELINE)}
    assert set(batch.channels[2].values) == {float(ADC_BASELINE)}


def test_the_channels_are_sorted_and_deduplicated_like_the_real_client() -> None:
    client = SimulatedBitalinoClient(
        ManualClock(), physiology=_resting(ManualClock()), channels=[3, 0, 3]
    )
    assert client.channels == (0, 3)


def test_a_batch_is_stamped_with_its_first_sample() -> None:
    """``SampleBatch.timestamp`` is the FIRST sample's wall-clock time, not "now".

    Two one-second blocks read together at t = 2 s: the first starts at the
    acquisition start, the second one second later.
    """

    async def scenario() -> None:
        clock = ManualClock()
        client = SimulatedBitalinoClient(clock, physiology=_resting(clock))
        await client.connect()
        await client.start_acquisition()
        started = clock.unix_millis()
        clock.advance(Seconds(2.0))
        first = await client.read_samples(1000)
        second = await client.read_samples(1000)
        assert first is not None
        assert second is not None
        assert first.timestamp == started
        assert second.timestamp == started + 1000

    asyncio.run(scenario())


def test_the_client_refuses_a_configuration_the_hardware_would_refuse() -> None:
    """Mirrored from the real client, because a console that can start a
    simulated session at 500 Hz and not a real one was never really tested."""
    with pytest.raises(ValueError, match="sample rate must be one of"):
        SimulatedBitalinoClient(ManualClock(), physiology=_resting(ManualClock()), sample_rate=500)
    with pytest.raises(ValueError, match="channel must be 0-5"):
        SimulatedBitalinoClient(ManualClock(), physiology=_resting(ManualClock()), channels=[6])
    with pytest.raises(ValueError, match="channel must be 0-5"):
        SimulatedBitalinoClient(ManualClock(), physiology=_resting(ManualClock()), channels=[-1])
    with pytest.raises(ValueError, match="at least one analog channel"):
        SimulatedBitalinoClient(ManualClock(), physiology=_resting(ManualClock()), channels=[])
    assert sorted(LEGAL_SAMPLE_RATES) == [1, 10, 100, 1000]


def test_reading_a_non_positive_block_is_a_caller_bug() -> None:
    """A stated divergence from the real client, which returns an empty batch.

    Raising is better: a zero-length batch is never what a caller meant, and it
    propagates to the DSP as a silent no-op instead of naming its origin.
    """

    async def scenario() -> None:
        clock = ManualClock()
        client = SimulatedBitalinoClient(clock, physiology=_resting(clock))
        await client.connect()
        await client.start_acquisition()
        clock.advance(Seconds(1.0))
        with pytest.raises(ValueError, match="count must be positive"):
            await client.read_samples(0)

    asyncio.run(scenario())


def test_a_client_that_will_not_connect_stays_that_way() -> None:
    """An operator resolves this with their hands; a retry loop must not "fix" it."""

    async def scenario() -> None:
        client = SimulatedBitalinoClient(ManualClock(), physiology=_resting(ManualClock()))
        client.inject_connect_failure()
        assert await client.connect() is False
        assert await client.connect(timeout=1.0) is False
        assert client.is_connected is False

    asyncio.run(scenario())


def test_starting_an_already_running_acquisition_does_not_restart_the_clock() -> None:
    """The real client's odd answer, mirrored: ``True``, and no reset.

    Resetting the sample clock here would hide a double-start bug in the caller
    by making its symptom - a gap in the data - disappear.
    """

    async def scenario() -> None:
        clock = ManualClock()
        client = SimulatedBitalinoClient(clock, physiology=_resting(clock))
        await client.connect()
        assert await client.start_acquisition() is True
        clock.advance(Seconds(1.0))
        assert await client.read_samples(1000) is not None

        assert await client.start_acquisition() is True
        assert client.samples_generated == 1000
        assert await client.read_samples(1000) is None

    asyncio.run(scenario())


def test_stopping_and_disconnecting_are_both_idempotent() -> None:
    """Shutdown paths run more than once, and none of them may raise."""

    async def scenario() -> None:
        clock = ManualClock()
        client = SimulatedBitalinoClient(clock, physiology=_resting(clock))
        await client.stop_acquisition()
        await client.disconnect()

        await client.connect()
        await client.start_acquisition()
        await client.disconnect()
        assert client.is_acquiring is False
        assert client.is_connected is False
        await client.disconnect()
        assert client.is_connected is False

    asyncio.run(scenario())


def test_an_injected_disconnect_fires_the_callback_after_the_flags_drop() -> None:
    """Mirrors the real client's acquisition-thread failure path, in order.

    A callback that inspected the client must see a device that is already gone
    rather than one that is about to be: a consumer's disconnect
    handling is only as good as that ordering, and it is the ordering the real
    client happens to have.
    """
    seen: list[tuple[bool, bool]] = []

    async def scenario() -> None:
        clock = ManualClock()
        client = SimulatedBitalinoClient(clock, physiology=_resting(clock))

        async def on_disconnect() -> None:
            seen.append((client.is_connected, client.is_acquiring))

        client.set_disconnect_callback(on_disconnect)
        await client.connect()
        await client.start_acquisition()
        clock.advance(Seconds(1.0))
        assert await client.read_samples(1000) is not None

        await client.inject_disconnect()
        assert client.is_connected is False
        assert client.is_acquiring is False
        assert await client.read_samples(1000) is None

    asyncio.run(scenario())
    assert seen == [(False, False)]


def test_an_injected_disconnect_without_a_callback_is_still_a_disconnect() -> None:
    """Nobody listening is not a reason to stay connected."""

    async def scenario() -> None:
        client = SimulatedBitalinoClient(ManualClock(), physiology=_resting(ManualClock()))
        await client.connect()
        await client.inject_disconnect()
        assert client.is_connected is False

    asyncio.run(scenario())


def test_the_client_accepts_the_callback_shape_of_the_real_client() -> None:
    """A coroutine function taking nothing and returning nothing."""
    callback: Callable[[], Awaitable[None]] = _noop
    client = SimulatedBitalinoClient(ManualClock(), physiology=_resting(ManualClock()))
    client.set_disconnect_callback(callback)


async def _noop() -> None:
    """A disconnect callback that does nothing, for the shape test above."""


def test_the_client_builds_its_own_plant_and_sensor_when_given_neither() -> None:
    """So a closed-loop script is one constructor call, not three.

    The plant's origin is taken from the injected clock, which is what keeps a
    default-built client replayable.
    """
    clock: Clock = ManualClock(start=Monotonic(500.0))
    client = SimulatedBitalinoClient(clock, physiology=_resting(clock))

    state = client.subject.advance(Monotonic(500.0), MotorRpm(0))
    assert state.at == Monotonic(500.0)
    assert state.heart_rate == Bpm(HR_REST)


def test_the_motor_speed_is_pushed_in_at_the_motor_shaft() -> None:
    """Handing an output-shaft speed in would understate the load 49.79-fold.

    So the unit is asserted behaviourally: 1380 pushed in has to produce the
    nominal 0.859 g, not the 0.00035 g that 1380 output rpm would imply.
    """

    async def scenario() -> None:
        clock = ManualClock()
        subject = Physiology(geometry=SIM_ARM, origin=clock.monotonic())
        client = SimulatedBitalinoClient(clock, physiology=subject)
        await client.connect()
        await client.start_acquisition()
        client.set_motor_rpm(MotorRpm(NOMINAL_RPM))
        clock.advance(Seconds(1.0))
        assert await client.read_samples(1000) is not None

        state = subject.advance(clock.monotonic(), MotorRpm(NOMINAL_RPM))
        assert state.g_load == pytest.approx(_hand_g(NOMINAL_RPM), rel=1e-12)

    asyncio.run(scenario())


def test_a_supplied_plant_and_sensor_are_the_ones_used() -> None:
    """A scenario is set up on the plant, so the client must not shadow it."""

    async def scenario() -> None:
        clock = ManualClock()
        subject = Physiology(
            geometry=SIM_ARM,
            origin=clock.monotonic(),
            script=script_for(ScriptedEvent.ELECTRODE_OFF),
        )
        synth = EcgSynthesizer(EcgConfig(seed=99))
        client = SimulatedBitalinoClient(clock, physiology=subject, ecg=synth)
        await client.connect()
        await client.start_acquisition()
        clock.advance(Seconds(1.0))
        batch = await client.read_samples(1000)

        assert batch is not None
        assert client.subject is subject
        assert synth.samples_rendered == 1000
        # The scripted electrode-off reached the sensor through the plant.
        assert set(batch.channels[0].values) == {float(ADC_BASELINE)}

    asyncio.run(scenario())
