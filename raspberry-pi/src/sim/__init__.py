"""The plant on the other side of the sensors: a subject, an ECG, a BITalino.

This package closes the loop with no hardware attached. Together with
``src/motor/simulated.py`` it is what makes the control law and the safety
supervisor testable **before a person is ever inside the centrifuge**, and it
stays the CI path forever - so anything modelled loosely here is a class of bug
that reaches the bench with nothing standing in its way.

Layout, and the dependency rule that keeps it honest:

* ``physiology.py`` is the **plant**: the subject's heart, driven from the real
  geometry (gear ratio, radius, centripetal g), plus the scripted scenarios.
  It imports ``src.units`` and nothing else from this repository - in
  particular it never reads a clock (contract rule 4): every call takes ``now``.
* ``ecg.py`` is the **sensor**: it turns the plant's beat interval into RAW
  10-bit ADC counts, so the real ``src/signal_processing.py`` does the actual
  DSP and quality grading. It may import ``physiology``; ``physiology`` must
  never import it.
* ``bitalino.py`` is the **wire**: a drop-in duck type for
  ``src.bitalino_client.BITalinoClient`` that hands out real ``SampleBatch``
  objects. It wires the two above together and is the only module here that
  holds a ``Clock``.

Why the direction matters: the sensor observes the subject, so a subject that
knew about its own electrodes could make a signal artifact change a heart rate,
which is exactly the confusion this simulator exists to detect in the layers
above it.

The one honest departure from the contract is stated where it happens
(``bitalino.py``): that module's method signatures are dictated by the real
client's - bools and ``None`` rather than ``Result`` - because a simulator that
is better typed than the thing it replaces is no longer a drop-in, and the
substitution is the whole value.

Deliberately no re-exports here. One blessed import path per symbol
(``from src.sim.physiology import Physiology``) keeps grep honest about who
depends on what, and it means adding a sibling module never requires editing
this file.

See .claude/skills/anheart-strict-python/SKILL.md.
"""
