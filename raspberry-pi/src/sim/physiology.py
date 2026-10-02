"""The subject: a heart driven by the real geometry of this machine.

The stressor in this centrifuge is **centripetal acceleration**, not an
abstract "load". That distinction is the whole reason this module exists in
this shape:

    omega_out = 2*pi*rpm_motor / (60*i)      i = 49.79, the SEW KA37 gearbox
    g         = omega_out^2 * r / 9.80665    r = the seat radius

so the stimulus goes as **speed squared**. A model driven by "load = rpm/max"
would give a constant control gain, the controller tuned against it would be
tuned wrong everywhere, and the error would be largest exactly where it matters
least to notice it and most to get right - at the bottom of the range, where a
naive tune is twice as aggressive as the plant deserves.

The geometry is **injected** (:class:`~src.geometry.MachineGeometry`, the one
record every speed in this system is rendered through), never defaulted: an
earlier version carried its own 1.0 m radius while the rest of the code quoted
1.5 m, which is how a simulated g-load comes to disagree with the screen.

The numbers this produces with the cardiac defaults below **at r = 1.0 m**
(the geometry ``tests/test_sim.py`` chooses explicitly, and asserts against
hand-derived values, not against these functions):

* 1380 motor rpm -> 27.716 output rpm -> **0.859 g** -> **164 bpm** steady state
* local gain there: **0.137 bpm per motor-rpm**
* local gain at 690 rpm: **0.068 bpm/rpm** - exactly half, because g goes as
  speed squared. A controller with one fixed gain is either sluggish at the
  bottom or oscillatory at the top.

Two dynamics on top of the static response, both first order:

1. **Response lag.** ``tau_up`` 30 s rising, ``tau_down`` 55 s falling: the
   heart follows a speed change slowly and recovers more slowly still. This is
   the dead time that makes an over-eager controller overshoot the zone
   ceiling.
2. **Cardiac drift.** At *constant* speed the heart rate keeps climbing, by up
   to ``drift_max`` 10 bpm with a 600 s time constant, because the subject
   tires. This is the one that surprises people, so it has its own paragraph
   below.

**Cardiac drift forces the controller to UNLOAD at constant speed.** Nothing
about the machine changed; the subject did. A deadband controller therefore
settles near the **TOP** of the target zone rather than its centre: drift walks
the heart rate up until it leaves the deadband, the controller trims the speed
down just enough to put it back inside, and the cycle repeats from the upper
edge. **That is correct behaviour, not a bug.** The target is a *zone*, and a
controller that chased the zone's centre would be commanding speed changes
continuously in response to a process with a 600 s time constant - more motion,
more motion artifact on the ECG, no clinical benefit. Any test that asserts
"settles at the zone centre" is asserting the wrong thing.

What this module deliberately does not do: read a clock (contract rule 4 -
every method takes ``now``), raise from anything the control path calls, or
know that electrodes exist. Signal artifacts are scripted here because a
*scenario* spans the subject and the sensor, but they are reported as an opaque
set for ``src/sim/ecg.py`` to render; nothing in this file lets an artifact
change a heart rate.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from types import MappingProxyType
from typing import Final, final

from src.geometry import MachineGeometry
from src.units import (
    Bpm,
    BpmPerMinute,
    GLoad,
    Monotonic,
    MotorRpm,
    OutputRpm,
    Seconds,
    elapsed,
    motor_to_output_rpm,
    output_rpm_to_g,
)

SECONDS_PER_MINUTE: Final[float] = 60.0


# =========================================================================
# The scripted scenarios
# =========================================================================


@unique
class ScriptedEvent(Enum):
    """A named thing that goes wrong, so each one is a test scenario by name.

    The values are the stable log/wire identifiers, not ``auto()``: an ordinal
    renumbers when the members are reordered and silently rewrites the meaning
    of every stored simulation trace. Same reason ``Phase`` does it.

    Each member belongs to exactly one of three mechanisms, and
    ``tests/test_sim.py`` asserts that partition is total - so a sixth event
    cannot be added and then silently ignored by the integrator:

    * :data:`CARDIAC_OVERRIDE_RATES` - drives the heart rate directly,
    * :data:`GAIN_SCALES` - changes how much the subject responds to g,
    * :data:`SIGNAL_ARTIFACTS` - corrupts the ECG and never touches the heart.
    """

    VASOVAGAL_DROP = "vasovagal_drop"
    # The rule that must not accelerate. A collapse of sympathetic tone: the
    # heart rate falls 30 bpm in 20 s while the machine is still turning. A
    # controller reading "below target" responds by speeding up, on somebody
    # who is fainting. This is why the safety supervisor outranks the control
    # law rather than advising it.

    HR_SPIKE = "hr_spike"
    # The opposite sign and ten times faster: +40 bpm in 2 s. No physiological
    # response is that fast, so anything the controller does about it is a
    # reaction to an artifact or an arrhythmia, and the correct answer is to
    # bound the rate of change rather than to track it.

    ELECTRODE_OFF = "electrode_off"
    # A lead comes off: the ECG goes flat, quality reads no_signal, and there
    # is NO heart rate at all - not a stale one, not a default. The pipeline
    # keeps re-emitting its previous metrics dict, so the number on screen goes
    # on looking alive. See HeartRateSample's sequence trap.

    MAINS_BURST = "mains_burst"
    # 50 Hz hum swamps the electrodes. Dangerous specifically because hum has a
    # perfectly steady "rate": a beat detector fed hum reports a confident,
    # stable, entirely fictional heart rate.

    NONRESPONDER = "nonresponder"
    # A subject whose heart barely answers the load at all. The controller
    # cannot reach the zone, saturates at the speed ceiling and stays there -
    # which is the case that finds integrator windup, and the case where "we
    # never reached the target" must not become "so push harder".


CARDIAC_OVERRIDE_RATES: Final[Mapping[ScriptedEvent, BpmPerMinute]] = MappingProxyType(
    {
        ScriptedEvent.VASOVAGAL_DROP: BpmPerMinute(-90.0),
        ScriptedEvent.HR_SPIKE: BpmPerMinute(1200.0),
    }
)
"""Events that drive the heart rate directly, in bpm per minute.

-90 bpm/min is -1.5 bpm/s, so the canonical 20 s vasovagal window is exactly
-30 bpm. +1200 bpm/min is +20 bpm/s, so a 2 s spike is exactly +40 bpm.

**While one of these is active the plant's own relaxation is SUSPENDED.** That
is not a trick to make an assertion come out round. A vasovagal episode is a
different mechanism from the exercise response, and superimposing the two would
make the size of the drop depend on ``tau_up`` and on where in the range the
subject happened to be - i.e. a scenario whose severity nobody could state.
The rule this scenario exists to test ("never accelerate on a falling heart
rate") is specified as *30 bpm in 20 seconds*, so the plant delivers exactly
that, and the natural relaxation takes over again the moment the window closes.
"""

GAIN_SCALES: Final[Mapping[ScriptedEvent, float]] = MappingProxyType(
    {ScriptedEvent.NONRESPONDER: 0.35}
)
"""Events that scale ``k_g``, the subject's bpm-per-g sensitivity.

0.35 puts the steady state at full speed around 103 bpm instead of 164, so a
zone above that is unreachable at any speed the machine has. Iterated in this
mapping's own order rather than over the active set, so the product is
bit-for-bit deterministic however the caller assembled the script.
"""

SIGNAL_ARTIFACTS: Final[frozenset[ScriptedEvent]] = frozenset(
    {ScriptedEvent.ELECTRODE_OFF, ScriptedEvent.MAINS_BURST}
)
"""Events that corrupt the ECG only. Rendered by ``src/sim/ecg.py``.

Nothing in this module reads this set: it is here so the partition of
:class:`ScriptedEvent` into mechanisms lives in one place, and so a test can
prove no event falls through all three.
"""

CANONICAL_DURATIONS: Final[Mapping[ScriptedEvent, Seconds]] = MappingProxyType(
    {
        ScriptedEvent.VASOVAGAL_DROP: Seconds(20.0),
        ScriptedEvent.HR_SPIKE: Seconds(2.0),
        ScriptedEvent.ELECTRODE_OFF: Seconds(30.0),
        ScriptedEvent.MAINS_BURST: Seconds(15.0),
        ScriptedEvent.NONRESPONDER: Seconds(3600.0),
    }
)
"""How long each scenario lasts when nobody says otherwise.

The two cardiac ones are chosen so the magnitude is a stated round number (see
:data:`CARDIAC_OVERRIDE_RATES`). ``NONRESPONDER`` runs an hour because it is a
trait rather than an incident - it is a window only so that one mechanism
covers all five events.
"""


@dataclass(frozen=True, slots=True)
class EventWindow:
    """One scripted event, active over ``[start, start + duration)``.

    ``start`` is measured from the origin the :class:`Physiology` was built
    with, so a script is written once and replayed against any clock.

    The interval is **half-open** on purpose: two windows that meet at an
    instant must not both be active at it, or a scenario built by concatenating
    scripts would briefly apply both.
    """

    event: ScriptedEvent
    start: Seconds
    duration: Seconds

    def __post_init__(self) -> None:
        # Raises rather than returning a Result: a script is assembled before
        # anything is spinning, and refusing to start is the right answer to a
        # scenario that cannot be placed on a timeline. Same boundary
        # SimulatedDriveConfig draws.
        if self.start < 0.0:
            raise ValueError(f"window start must not be negative, got {self.start}")
        if self.duration <= 0.0:
            raise ValueError(f"window duration must be positive, got {self.duration}")

    @property
    def end(self) -> Seconds:
        """The first instant at which this window is no longer active."""
        return Seconds(self.start + self.duration)

    def covers(self, moment: Seconds) -> bool:
        """Whether ``moment``, measured from the script's origin, is inside."""
        return self.start <= moment < self.end


def script_for(event: ScriptedEvent, *, start: Seconds = Seconds(0.0)) -> tuple[EventWindow, ...]:
    """The canonical one-window script for a scenario.

    One function for all five events rather than five constants, so adding an
    event cannot leave a scenario undefined: the lookup fails loudly, and a
    test asserts :data:`CANONICAL_DURATIONS` covers every member.
    """
    return (EventWindow(event, start, CANONICAL_DURATIONS[event]),)


def active_events(script: Sequence[EventWindow], moment: Seconds) -> frozenset[ScriptedEvent]:
    """Which events are active at ``moment``, measured from the script's origin."""
    return frozenset(window.event for window in script if window.covers(moment))


# =========================================================================
# The subject
# =========================================================================


@dataclass(frozen=True, slots=True)
class PhysiologyConfig:
    """The subject's numbers, all in one frozen record.

    Cardiac numbers only: the geometry is injected into :class:`Physiology`
    rather than defaulted here. The cardiac numbers are
    literature-shaped placeholders, and they are knobs precisely so that
    measuring a real subject is a config change and not a code change - but
    note that the *controller* must not be tuned so finely that it depends on
    them, because a second subject will have different ones.
    """

    hr_rest: Bpm = Bpm(70)
    """Heart rate with the machine stopped."""

    hr_max: Bpm = Bpm(185)
    """Ceiling on the *load response*. Cardiac drift rides on top of it (see
    :attr:`drift_max`), so the plant can settle slightly above this - which is
    the honest model: drift is not part of the load response it caps."""

    k_g: float = 110.0
    """Steady-state sensitivity, **bpm per g**.

    A bare ``float`` and not a ``NewType``, which is the one unit-typed
    quantity in this module that is not one: the right home for a ``BpmPerG``
    alias is ``src/units.py``, which this agent does not own. Reported rather
    than worked around."""

    tau_up: Seconds = Seconds(30.0)
    """Time constant while the heart rate is climbing towards its target."""

    tau_down: Seconds = Seconds(55.0)
    """And while it is falling. Slower than :attr:`tau_up`, which is why an
    over-shoot costs about twice as long to undo as it took to cause."""

    drift_max: float = 10.0
    """Cardiac drift ceiling, in bpm. A signed bpm offset, so a bare ``float``
    for the same reason :attr:`k_g` is: ``Bpm`` is an ``int``."""

    tau_drift: Seconds = Seconds(600.0)
    """Drift time constant. Ten minutes - far slower than anything the control
    loop should react to, and slower than most test sessions, which is why it
    has to be simulated rather than discovered on the bench."""

    fatigue: float = 0.0
    """Multiplier on the load response: ``k_g * g * (1 + fatigue)``.

    A subject trait, not an integrated state - a deconditioned subject answers
    the same g with a higher rate. ``NONRESPONDER`` moves the same response the
    other way, through :data:`GAIN_SCALES`."""

    hr_floor: Bpm = Bpm(30)
    """Absolute floor on the modelled rate.

    Load-bearing for two reasons, not cosmetic: a scripted drop long enough
    would otherwise walk the state negative, and the beat interval handed to
    the ECG synthesiser is ``60 / rate``, which needs the rate to stay well
    away from zero."""

    hr_ceiling: Bpm = Bpm(220)
    """Absolute ceiling, above :attr:`hr_max` + :attr:`drift_max` so it binds
    only on a scripted spike."""

    active_above: MotorRpm = MotorRpm(1)
    """The subject counts as under load at or above this speed, which is what
    accumulates drift. 1 rpm is also the finest speed the drive can report, so
    nothing observable is being discarded."""

    def __post_init__(self) -> None:
        for name, value in (
            ("k_g", self.k_g),
            ("tau_up", float(self.tau_up)),
            ("tau_down", float(self.tau_down)),
            ("tau_drift", float(self.tau_drift)),
        ):
            if value <= 0.0:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.drift_max < 0.0:
            raise ValueError(f"drift_max must not be negative, got {self.drift_max}")
        if self.fatigue <= -1.0:
            # 1 + fatigue is a gain on the load response; at -1 the subject
            # stops responding and below it responds backwards, which is not a
            # subject, it is a sign error.
            raise ValueError(f"fatigue must be greater than -1, got {self.fatigue}")
        if not self.hr_floor <= self.hr_rest <= self.hr_max <= self.hr_ceiling:
            raise ValueError(
                "heart rates must be ordered floor <= rest <= max <= ceiling, got "
                f"{self.hr_floor} / {self.hr_rest} / {self.hr_max} / {self.hr_ceiling}"
            )


DEFAULT_PHYSIOLOGY: Final[PhysiologyConfig] = PhysiologyConfig()
"""Shared default. A module-level instance, not a call in a default argument."""


@dataclass(frozen=True, slots=True)
class SubjectState:
    """Everything observable about the subject at one instant.

    Frozen, so a decision taken from an observation cannot be changed under the
    decision. This is *ground truth* - what a perfect sensor would see. It is
    deliberately NOT a ``HeartRateSample``: the whole point of the simulator is
    that the number the system acts on has been through
    ``src/sim/ecg.py`` and the real ``src/signal_processing.py`` first, and may
    be absent, late or simply wrong. A test that compares a controller against
    this record directly is testing a system that does not exist.
    """

    at: Monotonic
    """When, on the monotonic clock."""

    motor_rpm: MotorRpm
    """The speed that produced this state, at the MOTOR shaft."""

    output_rpm: OutputRpm
    """The same speed at the gearbox output: what the seat actually turns at."""

    g_load: GLoad
    """Centripetal load at the injected geometry's radius. Unsigned: load goes
    as speed squared, so turning backwards does not relieve it."""

    heart_rate: Bpm
    """Ground truth, rounded - what a perfect monitor would display."""

    rr_interval: Seconds
    """The beat interval, **unrounded**. This is the handoff to the ECG
    synthesiser, and it is a duration rather than a rate precisely so that it
    can be continuous: ``Bpm`` is an ``int``, and quantising the rate to whole
    bpm would put a visible 1-bpm staircase into the synthetic RR series and
    therefore into every HRV number computed downstream."""

    steady_state: Bpm
    """Where the rate is heading at this speed: ``min(hr_max, hr_rest + k_g*g*(1+fatigue))``,
    rounded. For logs and for tests of the static response."""

    drift_bpm: float
    """Accumulated cardiac drift, in bpm. Bare ``float`` for the reason given
    on :attr:`PhysiologyConfig.drift_max`."""

    artifacts: frozenset[ScriptedEvent]
    """The events active over the step that produced this state.

    Opaque here - this module never reads it. ``src/sim/ecg.py`` renders it.
    """


@final
class Physiology:
    """A subject-shaped plant. **Mutable by design**, which the contract treats
    as the exception: this object *is* a person's cardiovascular state, and a
    frozen one could not respond to anything.

    Every field is private and every mutation happens in :meth:`advance`, so
    the state a caller sees only ever comes from a :class:`SubjectState`.

    Time is handed in, never read (contract rule 4)::

        clock = ManualClock()
        subject = Physiology(origin=clock.monotonic(), geometry=arm)
        state = subject.advance(clock.advance(Seconds(0.2)), MotorRpm(900))

    One explicit first-order step over the whole ``dt``, so accuracy depends on
    the step size; the control loop runs at 5 Hz, so step in 0.2 s or less.
    Two consequences of that, both deliberate and both tested:

    * The step fraction ``dt/tau`` is clamped to 1.0, so an oversized step
      lands *on* the target instead of past it. Without the clamp explicit
      Euler oscillates for ``dt > tau`` and diverges for ``dt > 2*tau``, and a
      physiology model that can emit -400 bpm hands the safety layer garbage
      that looks like a measurement. With it, every update is a convex
      combination of the current state and its target, so the state provably
      cannot leave the interval between them.
    * A window shorter than one step can be stepped straight over. Events are
      resolved at the **start** of each step, which is what makes a 20 s window
      integrate for exactly 20 s in 0.2 s steps rather than 19.8 s.
    """

    __slots__ = ("_config", "_drift", "_geometry", "_last_at", "_origin", "_rate", "_script")

    def __init__(
        self,
        *,
        origin: Monotonic,
        geometry: MachineGeometry,
        config: PhysiologyConfig = DEFAULT_PHYSIOLOGY,
        script: Sequence[EventWindow] = (),
    ) -> None:
        """Build a subject sitting at rest, with the machine stopped.

        ``origin`` is the zero of the script's timeline and of nothing else.
        There is no ``Clock`` here on purpose: a plant that could read the time
        could not be replayed.

        ``geometry`` is required, with no default, for the reason
        :class:`~src.geometry.MachineGeometry` has no default radius.
        """
        self._geometry: MachineGeometry = geometry
        self._config: PhysiologyConfig = config
        self._script: tuple[EventWindow, ...] = tuple(script)
        self._origin: Monotonic = origin
        self._last_at: Monotonic = origin
        self._rate: float = float(config.hr_rest)
        """Heart rate, unrounded, in bpm. Held as a float because the response
        is continuous; rounded only where a monitor would report it."""
        self._drift: float = 0.0
        """Cardiac drift, in bpm, accumulated while under load."""

    # -- observation -------------------------------------------------------

    @property
    def script(self) -> tuple[EventWindow, ...]:
        """The scenario this subject is being run through. For logs and tests."""
        return self._script

    # -- the plant ---------------------------------------------------------

    def advance(self, now: Monotonic, motor_rpm: MotorRpm) -> SubjectState:
        """Integrate to ``now`` at ``motor_rpm``, and report what is true there.

        Raises ``ValueError`` if ``now`` is behind the plant, matching
        ``ManualClock.advance`` and ``SimulatedDrive.advance``: a monotonic
        reading cannot go backwards, so a backwards one means two clocks got
        mixed up or a wall-clock value leaked in, and absorbing that as "no
        time passed" would hide the bug. This is the simulation harness, not
        the drive path.

        A ``dt`` of exactly zero is fine and is a no-op integration: it still
        returns the current state, which is what a caller polling faster than
        the clock moves should get.
        """
        if now < self._last_at:
            raise ValueError(f"cannot advance the subject backwards, from {self._last_at} to {now}")
        dt = elapsed(self._last_at, now)
        active = active_events(self._script, elapsed(self._origin, self._last_at))
        self._last_at = now

        output_rpm = motor_to_output_rpm(motor_rpm, self._geometry.ratio)
        g_load = output_rpm_to_g(output_rpm, self._geometry.radius)
        steady = self._steady_state(g_load, active)

        self._step(dt=dt, steady=steady, motor_rpm=motor_rpm, active=active)

        return SubjectState(
            at=now,
            motor_rpm=motor_rpm,
            output_rpm=output_rpm,
            g_load=g_load,
            heart_rate=Bpm(round(self._rate)),
            rr_interval=Seconds(SECONDS_PER_MINUTE / self._rate),
            steady_state=Bpm(round(steady)),
            drift_bpm=self._drift,
            artifacts=active,
        )

    def _steady_state(self, g_load: GLoad, active: frozenset[ScriptedEvent]) -> float:
        """``min(hr_max, hr_rest + k_g*g*(1+fatigue))``, in bpm.

        The ``min`` caps the **load response** only; drift is added to this
        later and may carry the state above ``hr_max``. See
        :attr:`PhysiologyConfig.hr_max`.
        """
        config = self._config
        gain = config.k_g
        for event, scale in GAIN_SCALES.items():
            if event in active:
                gain *= scale
        response = float(config.hr_rest) + gain * float(g_load) * (1.0 + config.fatigue)
        return min(float(config.hr_max), response)

    def _step(
        self,
        *,
        dt: Seconds,
        steady: float,
        motor_rpm: MotorRpm,
        active: frozenset[ScriptedEvent],
    ) -> None:
        """Advance the heart rate and the drift by one step of ``dt``."""
        config = self._config
        override = self._override_rate(active)

        if override is None:
            # tau is chosen against the LOAD response, not against the
            # drift-inclusive target: which way the exercise response is moving
            # is what sets the lag, and drift is a slow additive offset on top
            # of it rather than a thing the heart chases at its own rate.
            tau = config.tau_up if steady > self._rate else config.tau_down
            target = steady + self._drift
            self._rate += (target - self._rate) * _fraction(dt, tau)
        else:
            self._rate += override * dt

        self._rate = min(float(config.hr_ceiling), max(float(config.hr_floor), self._rate))

        loaded = config.drift_max if abs(motor_rpm) >= config.active_above else 0.0
        self._drift += (loaded - self._drift) * _fraction(dt, config.tau_drift)

    def _override_rate(self, active: frozenset[ScriptedEvent]) -> float | None:
        """Scripted rate of change in bpm per **second**, or ``None`` for no override.

        ``None`` rather than ``0.0`` because the two are not the same thing: a
        zero override would still suspend the plant's relaxation, which is the
        one behaviour a "no event" step must not have. Iterated over the
        mapping rather than over ``active`` so simultaneous events sum in a
        fixed order and the result is reproducible bit for bit.
        """
        rates = [float(rate) for event, rate in CARDIAC_OVERRIDE_RATES.items() if event in active]
        if len(rates) == 0:
            return None
        return math.fsum(rates) / SECONDS_PER_MINUTE


def _fraction(dt: Seconds, tau: Seconds) -> float:
    """``dt/tau``, clamped to 1.0.

    The clamp is what keeps every update a convex combination of the state and
    its target, and therefore what bounds the state for any step size a caller
    can hand in. See :class:`Physiology`. ``tau`` is validated positive by
    :meth:`PhysiologyConfig.__post_init__`, so there is no division to guard.
    """
    return min(1.0, dt / tau)
