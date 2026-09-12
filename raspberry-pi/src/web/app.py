"""Assembly, and the three things that are easy to get wrong here.

**1. Every handler must be a coroutine.** Starlette dispatches a *synchronous*
endpoint to a thread pool. A synchronous handler in this application would
therefore touch the control surface, the telemetry hub and the safety
supervisor from a thread that is not the event loop's - and every one of those
objects is deliberately lock-free on the grounds that only one thread ever
touches it. One ``def`` instead of one ``async def`` silently converts a
documented invariant into a data race on the state that stops a motor. So
:func:`require_async_routes` walks ``app.routes`` and **raises** otherwise,
both at construction and again on startup.

**2. uvicorn must not take the process's signal handlers.** ``uvicorn.Server``
installs its own SIGINT/SIGTERM handlers when it serves, which would mean
Ctrl+C or a ``docker stop`` is handled by the web server's shutdown path rather
than by the session process's - and the session process's is the one that
brings the motor to a stop, disables the drive and waits for standstill.
:class:`LoopSafeServer` neutralises that, and
:func:`unneutralised_signal_hooks` **fails the build of the server** if
uvicorn grows or renames a signal hook that is not accounted for, rather than
silently handing the signals back.

  Note for anyone comparing this against the brief: the method to override is
  ``capture_signals``, not ``install_signal_handlers``. uvicorn replaced the
  latter with a context manager some releases ago, and ``@override`` on a
  method that no longer exists is a type error - which is how this was caught.
  The check below is written against whatever uvicorn actually has, so the next
  rename fails loudly instead of quietly restoring the confiscation.

**3. ``log_config=None``.** uvicorn's default logging configuration calls
``logging.config.dictConfig``, which **disables existing loggers** by default -
so the session's own logging, configured in ``main.py`` before the web server
starts, would go quiet the moment the interface came up. Passing ``None`` means
uvicorn adds nothing and touches nothing.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator, Generator
from inspect import iscoroutinefunction
from typing import Final, final, override

import uvicorn
from fastapi import FastAPI
from starlette.routing import BaseRoute, Route, WebSocketRoute

from src.web.deps import Services, WebConfig
from src.web.routes import register_routes
from src.web.ws import register_socket

_logger: logging.Logger = logging.getLogger(__name__)

APP_TITLE: Final[str] = "AnHeart operator interface"

APP_SUMMARY: Final[str] = (
    "Local control and telemetry for one centrifuge. Loopback by default; "
    "off loopback it refuses to start without a shared token."
)


# =========================================================================
# The all-handlers-are-coroutines assertion
# =========================================================================


def require_async_routes(app: FastAPI) -> tuple[str, ...]:
    """Raise unless every route's endpoint is a coroutine function.

    Returns the paths it checked, so a caller (and a test) can see that it
    looked at something rather than at an empty list - a route walk that
    silently enumerates nothing is the failure mode of this kind of assertion.

    A ``raise``, not an ``assert``: ``assert`` vanishes under ``-O`` (contract
    rule 2), and this is the check that keeps shared mutable state on one
    thread.

    Route types other than ``Route`` and ``WebSocketRoute`` are refused rather
    than skipped. A ``Mount`` - a static-files app, say - has no endpoint to
    inspect, so allowing one would mean this function could no longer speak
    about everything the app serves. The page is served by ordinary async
    handlers for exactly that reason.
    """
    checked: list[str] = []
    offenders: list[str] = []
    unknown: list[str] = []
    route: BaseRoute
    for route in app.routes:
        if isinstance(route, Route | WebSocketRoute):
            checked.append(route.path)
            if not iscoroutinefunction(route.endpoint):
                offenders.append(f"{route.path} -> {route.endpoint!r}")
        else:
            unknown.append(f"{type(route).__name__} at {route!r}")
    if offenders:
        raise RuntimeError(
            "these route handlers are not coroutine functions, so Starlette would "
            "dispatch them to a thread pool and they would touch the control "
            "surface, the telemetry hub and the safety supervisor from off the event "
            "loop, where none of them is safe: " + ", ".join(offenders)
        )
    if unknown:
        raise RuntimeError(
            "these routes expose no endpoint to check, so nothing here can promise "
            "they run on the event loop: " + ", ".join(unknown)
        )
    if not checked:
        raise RuntimeError("no routes were registered: the interface would serve nothing")
    return tuple(checked)


# =========================================================================
# The app
# =========================================================================


def create_app(*, services: Services, config: WebConfig) -> FastAPI:
    """Build the operator interface.

    ``docs_url`` and ``redoc_url`` are disabled: both pages fetch their
    JavaScript from a CDN, so on the offline Pi they render as a blank screen
    that looks like a broken deployment. The OpenAPI document itself is kept -
    it costs nothing and it is how an operator tool discovers the API.
    """

    @contextlib.asynccontextmanager
    async def lifespan(instance: FastAPI) -> AsyncGenerator[None]:
        """Re-check the route contract on startup, then serve.

        Checked twice on purpose: once at construction, and once here, because
        a route added after ``create_app`` returned - by a plugin, a test
        fixture, a well-meaning patch - would otherwise pass unexamined.
        """
        paths = require_async_routes(instance)
        _logger.info(
            "operator interface serving %d routes on %s:%d (loopback=%s, token=%s)",
            len(paths),
            config.host,
            config.port,
            config.is_loopback,
            "required" if config.requires_token else "none",
        )
        yield

    app = FastAPI(
        title=APP_TITLE,
        summary=APP_SUMMARY,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    register_routes(app, services=services, config=config)
    register_socket(app, services=services, config=config)
    require_async_routes(app)
    return app


# =========================================================================
# The server
# =========================================================================

SIGNAL_HOOK_HINT: Final[str] = "signal"
"""Substring that identifies uvicorn's signal machinery on its Server class.

Derived rather than hard-coded so a rename is caught: see
:func:`unneutralised_signal_hooks`.
"""


@final
class LoopSafeServer(uvicorn.Server):
    """A uvicorn server that leaves the process's signal handlers alone.

    The session process owns SIGINT and SIGTERM, because its shutdown path is
    the one that stops the motor: zero the speed reference, wait for measured
    standstill, then disable the output stage. uvicorn's own handler would
    shut the web server down and let the process exit while the drive was still
    enabled - and with STO jumpered, an exiting process is a centrifuge
    coasting on whatever setpoint it last had until the drive's ``ttO``
    timeout notices the keepalive stopped.
    """

    @override
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        """Install nothing and restore nothing. The process owns its signals."""
        yield


def unneutralised_signal_hooks() -> tuple[str, ...]:
    """uvicorn signal hooks that :class:`LoopSafeServer` does not override.

    Discovered from uvicorn's own class rather than compared against a
    hard-coded name, because the name has already changed once:
    ``install_signal_handlers`` became the ``capture_signals`` context manager.
    An override of a method that no longer exists neutralises nothing, and the
    failure is silent - the server quietly takes the signals back and the
    process stops stopping the motor.

    Membership tests only, never a ``getattr``: reading an attribute off a
    class by name yields an untyped value, and this module has no holes in it.
    """
    return tuple(
        name
        for name in sorted(vars(uvicorn.Server))
        if SIGNAL_HOOK_HINT in name.lower() and name not in vars(LoopSafeServer)
    )


def build_server(app: FastAPI, config: WebConfig) -> LoopSafeServer:
    """Build the uvicorn server, refusing a configuration that is not loop-safe.

    ``log_config=None`` so uvicorn does not run ``dictConfig`` and silence the
    session's loggers; ``access_log=False`` because a 5 Hz dashboard would
    otherwise write a line per request and per socket frame, and on an SD card
    that is a wear problem rather than a tidiness one; ``lifespan="on"`` so the
    startup assertion actually runs.
    """
    unneutralised = unneutralised_signal_hooks()
    if unneutralised:
        raise RuntimeError(
            "this uvicorn has signal machinery this server does not override "
            f"({', '.join(unneutralised)}), so it would take SIGINT/SIGTERM away from "
            "the session process - whose shutdown is the one that stops the motor. "
            "Override the new hook in LoopSafeServer before serving"
        )
    settings = uvicorn.Config(
        app=app,
        host=config.host,
        port=config.port,
        log_config=None,
        access_log=False,
        lifespan="on",
        # "auto" rather than the explicit "websockets": uvicorn deprecated its
        # legacy websockets implementation and "auto" selects the current one,
        # while an explicit literal would have to track uvicorn's own renames.
        # WebSocket support must really be installed - `requirements-base.txt`
        # pins `websockets` for this - because "auto" degrades to no WebSocket
        # support at all when it is missing, and a telemetry socket that never
        # connects looks exactly like a machine that is not running.
        ws="auto",
        timeout_graceful_shutdown=2,
    )
    return LoopSafeServer(settings)


async def serve(server: LoopSafeServer) -> None:
    """Run the server until it is told to stop.

    Mounted by the session process as a task on **its own event loop**::

        server = build_server(create_app(services=..., config=...), config)
        web = asyncio.create_task(serve(server))
        ...
        server.should_exit = True
        await web

    Not a thread and not a second process, and that is not a deployment
    preference: ``src/control_surface.py`` and ``src/telemetry.py`` are
    lock-free on the premise that one thread touches them. Running this
    anywhere else makes every method in both modules a race.
    """
    await server.serve()
