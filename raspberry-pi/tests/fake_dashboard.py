"""A dashboard in memory, for the end-to-end test of the sending.

:class:`FakeDashboard` is a :class:`~src.cloud_sync.CloudTransport` that
applies, to what the console sends, the rules the real dashboard applies
(``convex/training.ts``, contract 1.1): one session per local reference, one
row per ``(session, t)`` and per ``(session, seq)``, the session dated on its
own clock from the age the machine says, the dates of the points bounded on
the machine's axis.

It is not the proof that the real dashboard does the same. That proof is
``convex/journalTrace.test.ts``, which replays against the real functions
every request this fake received in the acceptance scenario
(``contracts/fixtures/journal-sync-trace.json``) and requires the same
answers.

It also plays the network: reachable or not, and an answer that is lost after
the request was received.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, cast

from src.cloud_sync import CloudError, Document, Refused, Unreachable
from src.contract import CONTRACT_VERSION, SERVER_VERSION_FIELD, ErrorCode
from src.result import Err, Ok, Result
from src.training.plan import JsonValue

EARLIEST_SESSION_START_MS: Final[int] = 1_704_067_200_000
WINDOW_MARGIN_MS: Final[int] = 60_000

LOCAL: Final[str] = "/api/machine/training/local"
TELEMETRY: Final[str] = "/api/machine/training/telemetry"
EVENTS: Final[str] = "/api/machine/training/events"
END: Final[str] = "/api/machine/training/end"
SESSION_ROUTES: Final[frozenset[str]] = frozenset({LOCAL, TELEMETRY, EVENTS, END})

type Reply = Result[Document, CloudError]
type Body = Mapping[str, JsonValue]


@dataclass
class ServerSession:
    """One session, as the dashboard holds it."""

    session_id: str
    local_ref: str
    started_at: int
    """On the dashboard's own clock."""

    machine_started_at: int | None
    """The start the machine dated; set only when it said the session's age."""

    status: str = "active"
    ended_at: int | None = None
    end_reason: str | None = None

    @property
    def shift(self) -> int:
        """What places a date of the machine's axis on the dashboard's clock."""
        return 0 if self.machine_started_at is None else self.started_at - self.machine_started_at


@dataclass(frozen=True)
class Stored:
    """One point or one event, with the date the dashboard first received it."""

    item: Mapping[str, JsonValue]
    received_at: int


@dataclass(frozen=True)
class Exchange:
    """One request of a session's delivery that reached the dashboard, and its answer."""

    at: int
    path: str
    body: Body
    status: int
    answer: Mapping[str, object]


def _number(body: Mapping[str, JsonValue], key: str) -> int | None:
    value = body.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _items(body: Body, key: str) -> Sequence[Mapping[str, JsonValue]]:
    return cast("Sequence[Mapping[str, JsonValue]]", body[key])


@dataclass
class FakeDashboard:
    """See the module docstring. ``now`` is the dashboard's clock, in unix ms."""

    now: Callable[[], int]
    online: bool = True
    cut_at: str | None = None
    """The route whose next request is the last before the network is lost: it is received
    and processed, its answer never reaches the console, and nothing gets through after."""

    sessions: dict[str, ServerSession] = field(default_factory=dict[str, ServerSession])
    telemetry: dict[tuple[str, int], Stored] = field(default_factory=dict[tuple[str, int], Stored])
    events: dict[tuple[str, int], Stored] = field(default_factory=dict[tuple[str, int], Stored])
    duplicates: int = 0
    rejected: int = 0
    trace: list[Exchange] = field(default_factory=list[Exchange])
    paths: list[str] = field(default_factory=list[str])

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
        if not self.online:
            return Err(Unreachable(f"GET {path}: no route to host"))
        self.paths.append(path)
        if path.endswith("/poll"):
            return Ok({"session": None, SERVER_VERSION_FIELD: CONTRACT_VERSION})
        session = self.sessions.get((params or {}).get("sessionId", ""))
        active = session is not None and session.status == "active"
        return Ok(
            {"active": active, "stopRequested": False, SERVER_VERSION_FIELD: CONTRACT_VERSION}
        )

    async def post(self, path: str, body: Body) -> Reply:
        if not self.online:
            return Err(Unreachable(f"POST {path}: no route to host"))
        self.paths.append(path)
        reply = self._handle(path, body)
        if path in SESSION_ROUTES:
            self.trace.append(
                Exchange(
                    at=self.now(),
                    path=path,
                    body=body,
                    status=200 if isinstance(reply, Ok) else 400,
                    answer=reply.value if isinstance(reply, Ok) else {},
                )
            )
        if path == self.cut_at:
            self.cut_at = None
            self.online = False
            return Err(Unreachable(f"POST {path}: the answer was lost"))
        return reply

    def _handle(self, path: str, body: Body) -> Reply:
        if path == LOCAL:
            return Ok({"sessionId": self._declared(body).session_id})
        if path not in (TELEMETRY, EVENTS, END):
            return Ok({"success": True})
        session = self.sessions.get(str(body.get("sessionId")))
        if session is None:
            return Err(Refused(400, "Session not found", ErrorCode("session_not_found")))
        if path == END:
            self._ended(session, body)
            return Ok({"success": True})
        store = self.telemetry if path == TELEMETRY else self.events
        key = "t" if path == TELEMETRY else "seq"
        return Ok(
            self._stored(
                session, _items(body, "points" if path == TELEMETRY else "events"), store, key
            )
        )

    def _declared(self, body: Body) -> ServerSession:
        reference = str(body["localRef"])
        for session in self.sessions.values():
            if session.local_ref == reference:
                return session
        machine_start = _number(body, "startedAt") or 0
        age = _number(body, "sessionAgeMs")
        now = self.now()
        aged = age is not None and age >= 0 and now - age >= EARLIEST_SESSION_START_MS
        session = ServerSession(
            session_id=f"SESSION-{len(self.sessions) + 1}",
            local_ref=reference,
            started_at=now - (age or 0) if aged else machine_start,
            machine_started_at=machine_start if aged else None,
        )
        self.sessions[session.session_id] = session
        return session

    def _ended(self, session: ServerSession, body: Body) -> None:
        if session.status != "active":
            return
        dated = _number(body, "endedAt")
        ended = self.now() if dated is None else dated
        if dated is not None and session.machine_started_at is not None:
            ended = max(session.started_at, dated + session.shift)
        session.status = "failed" if body.get("failed") is True else "completed"
        session.ended_at = ended
        session.end_reason = str(body.get("reason"))

    def _stored(
        self,
        session: ServerSession,
        items: Sequence[Mapping[str, JsonValue]],
        store: dict[tuple[str, int], Stored],
        key: str,
    ) -> Document:
        outcome = {"stored": 0, "duplicates": 0, "rejected": 0}
        for item in items:
            t = cast("int", item["t"])
            identity = (session.session_id, cast("int", item[key]))
            if not self._in_window(session, t):
                outcome["rejected"] += 1
            elif identity in store:
                outcome["duplicates"] += 1
            else:
                store[identity] = Stored(item, self.now())
                outcome["stored"] += 1
        self.duplicates += outcome["duplicates"]
        self.rejected += outcome["rejected"]
        return outcome

    def _in_window(self, session: ServerSession, t: int) -> bool:
        start = session.machine_started_at
        if start is None:
            return True
        if t < start - WINDOW_MARGIN_MS:
            return False
        ended = session.ended_at
        return ended is None or t <= ended - session.shift + WINDOW_MARGIN_MS
