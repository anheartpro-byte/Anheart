"""ANH-151 EX-3: a session owes nothing to the console's TLS, and TLS takes nothing from it.

The real console (``build_panel``, ``LocalPanel.run``, its real web server
with its real certificate) on the real clock, over a simulated drive. It is
configured for an address of a network and served on this machine's loopback.
Then TLS clients that never finish are attached to it, the way a hostile or a
broken device of that network would:

* some connect and say nothing;
* some send half of their first message and stop;
* some send it whole, let the console answer, and never reply: each of those
  has cost the console the one signature a handshake costs, and now sits
  there;
* one sends its first message a byte at a time.

With all of them attached, and held until the console has left:

* **the control tick is measured**: the instants of the snapshots the page
  receives, before and after they are attached. The supervisor's own bound
  (``loop_stall``, 0.6 s) is never approached, and it says so itself;
* **the session goes on**, and **the operator stops it on the connection the
  page already had**: no new connection is needed to stop a machine;
* **the wired stop stops it too**: a stop that acts on the drive, which no
  network takes part in, ends the next session on its verdict;
* **somebody new still gets in**, and **the console still leaves** when it is
  asked to, drive first.

A second, shorter run serves a certificate that has EXPIRED: the console
starts, runs a session and stops it. A certificate that expires is never the
reason a machine cannot be stopped.

Not established here: what the process does once it has no file descriptor
left for a new connection. The stalled clients of this test are a few dozen,
far from that limit; bounding how many one device may hold is the firewall's
business (``docs/securite.md``).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import statistics
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from itertools import pairwise
from pathlib import Path
from typing import Final, final

import httpx
import pytest

from src.clock import RealClock
from src.local_config import LocalConfig
from src.local_panel import EXIT_OK, DriveSide, LocalPanel, build_panel
from src.motor.drive import DriveFault
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.telemetry import PayloadKind
from src.training.runtime import EndReason, RuntimeState
from src.training.safety import RULE_DRIVE_FAULT, RULE_LOOP_STALL
from src.units import Seconds
from src.web.app import build_server, create_app, serve
from src.web.deps import TOKEN_HEADER, Services
from tests.test_failure_rig import BENCH_ENV, ENERGISED, MIN_RUN, OPERATOR, config_of, no_dsp
from tests.test_web_api import TOKEN
from tests.tls_support import (
    AP_ADDRESS,
    AP_NAME,
    Material,
    issue_dated,
    loopback_listener,
    make_material,
    port_of,
    requires_openssl,
    unverified,
)

pytestmark = requires_openssl

STALL_BOUND: Final[float] = 0.6
"""The supervisor's own bound on the time between two ticks (``loop_stall``)."""

PERIOD: Final[float] = 0.2
"""The control period."""

EACH: Final[int] = 15
"""Stalled clients of each of the three kinds: 45 connections that never finish."""

CHURN: Final[int] = 60
"""Whole handshakes made back to back while they are attached: 60 signatures."""

FAST_TTO: Final[Seconds] = Seconds(1.0)

TARGET_OUTPUT_RPM: Final[float] = 1.2
"""Just above the lowest speed the drive runs at: reached, and left, in a second."""


@final
class LoopbackRunner:
    """The console's own web server, on a loopback socket instead of the configured address.

    What :class:`src.local_panel.UvicornRunner` does, with the one difference
    that makes the test safe to run anywhere: the server is built from the
    console's configuration (an address of a network, a certificate) and
    listens on ``127.0.0.1``.
    """

    def __init__(self, services: Services, config: LocalConfig, listener: socket.socket) -> None:
        web = config.web
        self._server = build_server(create_app(services=services, config=web), web)
        self._listener = listener

    async def serve(self) -> None:
        await serve(self._server, sockets=[self._listener])

    def request_exit(self) -> None:
        self._server.should_exit = True

    @property
    def started(self) -> bool:
        return self._server.started

    def connections(self) -> frozenset[object]:
        """The connections the server holds that completed their handshake."""
        return frozenset(self._server.server_state.connections)


@dataclass
class Leaving:
    """How the console left, filled in once it has."""

    code: int | None = None
    took: float | None = None


@dataclass
class Console:
    """The running console of a test, and what the test reads of it."""

    panel: LocalPanel
    sim: SimulatedDrive
    web: LoopbackRunner
    port: int
    seen: list[float] = field(default_factory=list)
    """The instant of every snapshot the page's socket would have received."""

    leaving: Leaving = field(default_factory=Leaving)

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def state(self) -> RuntimeState:
        """Read through a call: a checker keeps a property's narrowing across an ``await``."""
        return self.panel.runtime.state

    def turning(self) -> float:
        return float(self.panel.runtime.snapshot().measured.motor_rpm)


async def until(condition: Callable[[], bool], what: str, within: float = 30.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"never happened within {within:.0f} s: {what}")
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def running(tmp_path: Path, material: Material) -> AsyncGenerator[Console]:
    """The real console on the real clock, serving ``material``; asked to stop on the way out."""
    listener = loopback_listener()
    port = port_of(listener)
    config = config_of(
        {
            **BENCH_ENV,
            "UI_HOST": AP_ADDRESS,
            "UI_PORT": str(port),
            "UI_TOKEN": TOKEN,
            "UI_TLS_CERT": str(material.certificate),
            "UI_TLS_KEY": str(material.key),
            "UI_TLS_NAMES": AP_NAME,
        }
    )
    assert not config.web.is_loopback, "the console under test is one of a network"
    clock = RealClock()
    sim = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True, tto=FAST_TTO))
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=no_dsp,
        drive=DriveSide(backend=sim, simulator=sim, release=lambda: None),
    )
    web = LoopbackRunner(panel.services, config, listener)
    console = Console(panel=panel, sim=sim, web=web, port=port)
    watcher = panel.hub.subscribe()

    async def watch() -> None:
        while True:
            payload = await watcher.next_payload()
            if payload.kind is PayloadKind.SNAPSHOT and payload.snapshot is not None:
                console.seen.append(float(payload.snapshot.at))

    stop = asyncio.Event()
    watching = asyncio.create_task(watch())
    run = asyncio.create_task(panel.run(stop, web))
    try:
        await until(lambda: web.started or run.done(), "the web server starting")
        yield console
    finally:
        asked = time.monotonic()
        stop.set()
        console.leaving.code = await asyncio.wait_for(run, 60.0)
        console.leaving.took = time.monotonic() - asked
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)


def page_of(console: Console, client: ssl.SSLContext) -> httpx.AsyncClient:
    """The operator's page: ONE connection, kept, carrying the token."""
    return httpx.AsyncClient(
        verify=client,
        base_url=console.url,
        headers={TOKEN_HEADER: TOKEN},
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        timeout=10.0,
    )


async def start_session(console: Console, page: httpx.AsyncClient) -> None:
    """A bench session, started and brought to speed through the page."""
    started = await page.post(
        "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
    )
    assert started.status_code == HTTPStatus.ACCEPTED, started.text
    await until(lambda: console.state() is RuntimeState.RUNNING, "the session starting")
    target = await page.post(
        "/api/manual/target", json={"output_rpm": TARGET_OUTPUT_RPM, "operator": OPERATOR}
    )
    assert target.status_code == HTTPStatus.ACCEPTED, target.text
    await until(lambda: console.turning() >= MIN_RUN, "the shaft turning")


async def attest(page: httpx.AsyncClient) -> None:
    attested = await page.post(
        "/api/safety/attest",
        json={"operator": OPERATOR, "sto_jumper_removed": True, "mushroom_wired_nc": True},
    )
    assert attested.status_code == HTTPStatus.OK, attested.text


async def polling(page: httpx.AsyncClient, seconds: float) -> None:
    """Let ``seconds`` pass with the page polling the console, as an open page does."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        polled = await page.get("/api/panel")
        assert polled.status_code == HTTPStatus.OK
        await asyncio.sleep(0.5)


def gaps_since(console: Console, mark: int) -> list[float]:
    """The time between each tick and the next, for the ticks seen since ``mark``."""
    return [later - earlier for earlier, later in pairwise(console.seen[mark:])]


# =========================================================================
# TLS clients that never finish
# =========================================================================


def client_hello() -> bytes:
    """The first message of a real TLS client, as a browser sends it."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = unverified().wrap_bio(incoming, outgoing)
    with contextlib.suppress(ssl.SSLWantReadError):
        client.do_handshake()
    return outgoing.read()


@dataclass
class Stalled:
    """Connections to the console's port that start a TLS handshake and never finish it."""

    port: int
    silent: list[socket.socket] = field(default_factory=list)
    half: list[socket.socket] = field(default_factory=list)
    answered: list[socket.socket] = field(default_factory=list)

    def attach(self, each: int) -> None:
        hello = client_hello()
        for _ in range(each):
            self.silent.append(self._open(b""))
            self.half.append(self._open(hello[: len(hello) // 2]))
            self.answered.append(self._open(hello))

    def _open(self, said: bytes) -> socket.socket:
        link = socket.create_connection(("127.0.0.1", self.port), timeout=5.0)
        link.sendall(said)
        link.setblocking(False)
        return link

    def heard_back(self) -> int:
        """How many of the clients that sent a whole first message were answered.

        An answer is the console's own first message: it has done its part of
        the handshake for that client, signature included, and now waits for a
        reply that never comes.
        """
        heard = 0
        for link in self.answered:
            with contextlib.suppress(BlockingIOError):
                heard += bool(link.recv(1, socket.MSG_PEEK))
        return heard

    def still_open(self) -> int:
        """How many of the stalled connections the console has not closed."""
        kept = 0
        for link in (*self.silent, *self.half, *self.answered):
            try:
                gone = link.recv(1, socket.MSG_PEEK) == b""
            except BlockingIOError:
                gone = False
            except ConnectionError:
                gone = True
            kept += not gone
        return kept

    def close(self) -> None:
        for link in (*self.silent, *self.half, *self.answered):
            link.close()


async def drip(port: int) -> None:
    """One more client: its first message, one byte every 20 ms, for as long as it is let."""
    hello = client_hello()
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        for byte in hello[:-1]:
            writer.write(bytes([byte]))
            await writer.drain()
            await asyncio.sleep(0.02)
        await asyncio.Event().wait()
    finally:
        writer.close()


def churn(port: int, client: ssl.SSLContext, count: int) -> int:
    """``count`` whole handshakes, one after the other, each dropped as soon as it is done.

    The opposite of a client that stalls: one that makes the console sign as
    fast as it can. Run from a thread of its own, so that what it costs the
    console is the console's share and not this client's. Returns how many
    completed.
    """
    done = 0
    for _ in range(count):
        with (
            socket.create_connection(("127.0.0.1", port), timeout=10.0) as link,
            client.wrap_socket(link, server_hostname=AP_ADDRESS),
        ):
            done += 1
    return done


# =========================================================================
# The session under them
# =========================================================================


@pytest.mark.slow
async def test_ex3_stalled_tls_clients_move_neither_the_tick_nor_either_stop_of_a_session(
    tmp_path: Path,
) -> None:
    material = make_material(tmp_path / "tls", AP_ADDRESS, AP_NAME)
    stalled: Stalled | None = None
    dripping: asyncio.Task[None] | None = None
    try:
        async with running(tmp_path, material) as console:
            stalled = Stalled(console.port)
            runtime = console.panel.runtime
            async with page_of(console, material.client()) as page:
                await attest(page)
                await start_session(console, page)
                own = console.web.connections()
                assert len(own) == 1, "the page holds one connection"

                mark = len(console.seen)
                await polling(page, 2.0)
                quiet = gaps_since(console, mark)

                # From here on they are attached, and the ticks counted include
                # the moment they arrive and the handshakes the console signs.
                mark = len(console.seen)
                stalled.attach(EACH)
                dripping = asyncio.create_task(drip(console.port))
                await until(
                    lambda: stalled.heard_back() == EACH, "the console answering each hello"
                )
                signed, _ = await asyncio.gather(
                    asyncio.to_thread(churn, console.port, material.client(), CHURN),
                    polling(page, 3.0),
                )
                flooded = gaps_since(console, mark)
                assert signed == CHURN

                # The tick did not move, and the supervisor never said otherwise.
                assert len(quiet) >= 7
                assert len(flooded) >= 11
                assert max(flooded) < STALL_BOUND, f"a tick came {max(flooded):.3f} s late"
                assert abs(statistics.median(flooded) - PERIOD) < 0.05
                assert abs(statistics.median(flooded) - statistics.median(quiet)) < 0.05
                standing = runtime.standing
                assert standing is None or standing.rule != RULE_LOOP_STALL
                # The session went on, every stalled client still attached.
                assert console.state() is RuntimeState.RUNNING
                assert console.turning() >= MIN_RUN
                assert stalled.still_open() == 3 * EACH
                assert dripping is not None
                assert not dripping.done()

                # The operator's STOP, on the connection the page had before any of them.
                stop = await page.post("/api/session/stop", json={"operator": OPERATOR})
                assert stop.status_code == HTTPStatus.ACCEPTED, stop.text
                assert console.web.connections() == own, "no new connection was needed to stop"
                await until(lambda: console.state() is RuntimeState.FINISHED, "the session ending")
                assert runtime.end_reason is EndReason.OPERATOR_STOP
                assert console.turning() < 1

                # The wired stop: it acts on the drive, and no network is part of it.
                await start_session(console, page)
                console.sim.inject_fault(DriveFault.EXTERNAL_FAULT_INPUT)
                await until(
                    lambda: runtime.end_reason is EndReason.SAFETY_VERDICT,
                    "the session ending on the drive's fault",
                )
                verdict = runtime.standing
                assert verdict is not None
                assert verdict.rule == RULE_DRIVE_FAULT
                await until(
                    lambda: console.sim.sim_state not in ENERGISED,
                    "the drive letting go of the motor",
                )

                # Somebody new still gets in: the listener is not held by those who stall.
                async with page_of(console, material.client()) as newcomer:
                    health = await newcomer.get("/healthz")
                assert health.status_code == HTTPStatus.OK
                assert stalled.still_open() == 3 * EACH
        # The console left when asked, every stalled client still attached.
        leaving = console.leaving
        assert leaving.code == EXIT_OK
        assert leaving.took is not None
        assert leaving.took < 15.0, f"the console took {leaving.took:.1f} s to leave"
        console.sim.advance(RealClock().monotonic())
        assert console.sim.sim_state not in ENERGISED
    finally:
        if dripping is not None:
            dripping.cancel()
            await asyncio.gather(dripping, return_exceptions=True)
        if stalled is not None:
            stalled.close()


@pytest.mark.slow
async def test_ex3_an_expired_certificate_never_keeps_a_session_from_being_stopped(
    tmp_path: Path,
) -> None:
    """The console does not judge its own dates: it starts, serves, and stops what it runs.

    The tablet does judge them: a NEW connection that verifies is refused by
    the client. The page here does not verify, which is a connection opened
    before the expiry, or an operator who passed the browser's warning to
    stop the machine.
    """
    now = datetime.now(UTC)
    expired = issue_dated(
        tmp_path / "tls", not_before=now - timedelta(days=400), not_after=now - timedelta(days=1)
    )
    async with running(tmp_path, expired) as console:
        async with page_of(console, expired.client()) as verifying:
            with pytest.raises(httpx.ConnectError, match="certificate has expired"):
                await verifying.get("/healthz")
        async with page_of(console, unverified()) as page:
            await attest(page)
            await start_session(console, page)
            assert console.state() is RuntimeState.RUNNING
            stop = await page.post("/api/session/stop", json={"operator": OPERATOR})
            assert stop.status_code == HTTPStatus.ACCEPTED, stop.text
            await until(lambda: console.state() is RuntimeState.FINISHED, "the session ending")
            assert console.panel.runtime.end_reason is EndReason.OPERATOR_STOP
    assert console.leaving.code == EXIT_OK
