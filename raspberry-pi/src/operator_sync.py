"""The signed operator list, asked for at rest and adopted when accepted (ANH-152).

What the list is, how it is verified and what it opens: :mod:`src.console_access`.
This module is the link only, apart from it so that the web layer, which needs
the access, never imports the dashboard link.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import cast, final

import anyio

from src.clock import Clock
from src.cloud_sync import TOO_LARGE_ANSWER, CloudError, CloudTransport, Unreachable
from src.console_access import (
    MAX_PAYLOAD_BYTES,
    OPERATORS_PATH,
    OPERATORS_PERIOD,
    STORED_LIST,
    ConsoleAccess,
    OperatorList,
    StoredList,
    read_list,
    replace_file,
    signed_text,
)
from src.result import Err, Result
from src.units import Monotonic, elapsed

_logger: logging.Logger = logging.getLogger(__name__)


def _bounded_list(text: str, machine_id: str | None) -> Result[OperatorList, str]:
    """The signed text read, when it is no longer than Convex ever sends."""
    if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return Err(f"liste au-dela de {MAX_PAYLOAD_BYTES} octets")
    return read_list(text, machine_id)


def _not_newer(accepted: OperatorList, held: OperatorList | None) -> bool:
    """Whether ``accepted`` would go back on ``held``: older, or as old and not the same.

    Two different lists of one instant cannot both be the last: the one held stays.
    """
    if held is None:
        return False
    return accepted.issued_at < held.issued_at or (
        accepted.issued_at == held.issued_at and accepted != held
    )


@final
class OperatorSync:
    """Asks for the list from a task of its own, at rest; adopts what it accepts.

    Mutable, owned by the event loop: when it last asked, and what it last said
    of a refused answer.
    """

    __slots__ = (
        "_access",
        "_asked",
        "_at_rest",
        "_clock",
        "_machine_id",
        "_path",
        "_public_key",
        "_refused",
        "_say",
        "_transport",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        transport: CloudTransport,
        access: ConsoleAccess,
        path: Path,
        public_key: bytes | None,
        at_rest: Callable[[], bool],
        say: Callable[[str], None],
        machine_id: str | None,
    ) -> None:
        """``path``: where an accepted answer is written. ``at_rest``: whether the list
        may change now (the catalogue's rest). ``say``: one warning for the logbook.
        ``machine_id``: this machine (``RECORD_MACHINE_ID``), ``None`` when not set: a
        list of another is refused."""
        self._clock: Clock = clock
        self._transport: CloudTransport = transport
        self._access: ConsoleAccess = access
        self._path: Path = path
        self._public_key: bytes | None = public_key
        self._at_rest: Callable[[], bool] = at_rest
        self._say: Callable[[str], None] = say
        self._machine_id: str | None = machine_id
        self._asked: Monotonic | None = None
        self._refused: tuple[str, object] | None = None

    async def step(self) -> None:
        """Ask for the list when it is due and the machine is at rest; never raises."""
        now = self._clock.monotonic()
        if self._asked is not None and elapsed(self._asked, now) < OPERATORS_PERIOD:
            return
        if not self._at_rest():
            return
        self._asked = now
        fetched = await self._transport.get(OPERATORS_PATH, None)
        if isinstance(fetched, Err):
            self._missed(fetched.error)
            return
        answer = fetched.value
        signature = answer.get("signature")
        text = signed_text(answer, self._public_key)
        if isinstance(text, Err):
            self._refuse(text.error, signature)
        elif text.value is None:
            _logger.info("operator list: the dashboard signs none")
        else:
            read = _bounded_list(text.value, self._machine_id)
            if isinstance(read, Err):
                self._refuse(read.error, signature)
            else:
                await self._receive(read.value, text.value, signature)

    async def _receive(self, accepted: OperatorList, payload: str, signature: object) -> None:
        held = self._access.held
        if _not_newer(accepted, held):
            self._refuse("liste qui n'est pas plus recente que celle de la console", signature)
            return
        self._refused = None
        if accepted == held:
            return
        same_people = held is not None and accepted.same_holders(held)
        if not same_people and not self._at_rest():
            # A session began during the request: asked again at the next rest.
            self._asked = None
            return
        # Written whatever changed, its date alone included: the file is always the list
        # in force, or a newer one a session kept from it, which the next start takes.
        envelope = StoredList(payload=payload, signature=cast(str, signature))
        content = STORED_LIST.dump_json(envelope) + b"\n"
        written = await anyio.to_thread.run_sync(partial(replace_file, self._path, content))
        if same_people:
            # Nobody gains or loses: in force at once, its date with it, so that a list
            # replayed from between the two is refused. Said by nothing but the log.
            self._access.adopt(accepted)
            if isinstance(written, Err):
                _logger.warning("operator list date not written: %r", written.error)
            return
        if not self._at_rest():
            # A session began during the write: the disk holds the new list, memory the
            # one in force for that session; adopted at the next rest, or the next start.
            self._asked = None
            return
        if isinstance(written, Err):
            self._say(
                f"liste des operateurs non enregistree ({written.error}) : en vigueur "
                "jusqu'au prochain demarrage seulement"
            )
        self._access.adopt(accepted)
        self._say(
            f"liste des operateurs recue : {len(accepted.operators)} operateur(s), "
            f"{len(accepted.devices)} tablette(s)"
        )

    def _missed(self, error: CloudError) -> None:
        """No answer to read: the list stays. One too large to read is refused and said."""
        if isinstance(error, Unreachable) and error.detail == TOO_LARGE_ANSWER:
            self._refuse("reponse de la liste trop grande pour etre lue", None)
            return
        _logger.info("operator list: nothing received (%r)", error)

    def _refuse(self, reason: str, signature: object) -> None:
        """Say an answer dropped, once for the same reason and the same signature."""
        key = (reason, signature)
        if key == self._refused:
            return
        self._refused = key
        self._say(f"liste des operateurs refusee, la console garde la sienne : {reason}")
