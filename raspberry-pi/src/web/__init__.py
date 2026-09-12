"""The local operator interface: one page, served off the Pi, no internet.

Layout:

* ``deps.py`` - the bind/auth configuration and the services a handler may
  reach. Owns the refusal to listen on a network address without a token.
* ``schemas.py`` - the wire shapes. The one place domain ``NewType``s are
  turned into JSON, and the render contract the page is written against.
* ``routes.py`` - the HTTP handlers. Every one of them ``async def``.
* ``ws.py`` - the telemetry socket, including the ``Origin`` check that CORS
  does not do for WebSockets.
* ``app.py`` - assembly, the all-handlers-are-coroutines assertion, and the
  uvicorn server that does **not** take the process's signal handlers.
* ``static/`` - the page. Plain HTML, CSS and JS: no build step, no CDN,
  because the machine is expected to work with no network at all.

The whole app runs as a task on the **same event loop** as the session loop.
Not a thread, not a second process: ``src/control_surface.py`` explains why
that is the premise its lock-free design rests on, and every handler here is a
coroutine so that no request is ever dispatched to a thread pool where it could
touch that shared state from somewhere else.

Deliberately no re-exports. One blessed import path per symbol, so grep stays
honest about who depends on what.

See .claude/skills/anheart-strict-python/SKILL.md.
"""
