"""The camera/presence fail-safe: who is near the arm, and who is in the capsule.

A camera (not yet chosen) will watch the machine. Everything but the vision
model lives here:

* :mod:`src.presence.types` - the typed observation a detector publishes, and
  the :class:`~src.presence.types.PresenceSource` protocol it is read through;
* :mod:`src.presence.monitor` - the pure, clock-injected rules that turn those
  observations and the machine's state into latched decisions;
* :mod:`src.presence.simulated` - a scriptable camera for tests and the
  simulation;
* :mod:`src.presence.adapter` - applies decisions through the runtime's
  existing API only (``request_estop``, ``trip_from_thread``) and offers the
  start gate the console consults.

See ``src/presence/README.md`` and .claude/skills/anheart-strict-python/SKILL.md.
"""
