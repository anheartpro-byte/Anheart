"""ANH-152: the signed operator list, asked for at rest and adopted when accepted.

Every key here is made at run time and forgotten with the test.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import src.operator_sync as operator_sync_module
from src.clock import ManualClock
from src.cloud_sync import TOO_LARGE_ANSWER, Document, Refused, Unreachable
from src.console_access import (
    LIST_KIND,
    MAX_PAYLOAD_BYTES,
    OPERATORS_PATH,
    OPERATORS_PERIOD,
    ConsoleAccess,
    load_list,
    replace_file,
)
from src.operator_sync import DATE_WRITE_PERIOD, OperatorSync
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.units import Seconds
from tests.access_support import OPERATOR_ID, OPERATOR_NAME, signed_code

type Reply = Result[Document, Unreachable | Refused]


def payload(issued_at: int = 1_790_000_000_000, *, operators: int = 1, **extra: object) -> str:
    code = signed_code("482915")
    entry = {
        "userId": OPERATOR_ID,
        "name": OPERATOR_NAME,
        "code": {
            "algorithm": "scrypt",
            "n": code.n,
            "r": code.r,
            "p": code.p,
            "salt": base64.b64encode(code.salt).decode(),
            "hash": base64.b64encode(code.digest).decode(),
        },
    }
    return json.dumps(
        {
            "kind": LIST_KIND,
            "machineId": "machine-1",
            "issuedAt": issued_at,
            "operators": [entry][:operators],
            "devices": [],
            **extra,
        }
    )


@dataclass
class Dashboard:
    """The route, scripted: one reply, and how many times it was asked."""

    reply: Reply = field(default_factory=lambda: Ok({"status": "not_configured"}))
    asked: int = 0
    during: Callable[[], None] = lambda: None

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
        assert path == OPERATORS_PATH
        assert params is None
        self.asked += 1
        self.during()
        return self.reply

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Reply:
        raise AssertionError(f"the list posts nothing ({path}, {body})")


@dataclass
class Rig:
    clock: ManualClock
    dashboard: Dashboard
    access: ConsoleAccess
    sync: OperatorSync
    signer: Ed25519PrivateKey
    public: bytes
    said: list[str]
    rest: list[bool]
    path: Path

    async def step(self, seconds: float = 0.0) -> None:
        self.clock.advance(Seconds(seconds))
        await self.sync.step()

    def sign(self, text: str) -> None:
        signature = base64.b64encode(self.signer.sign(text.encode("utf-8"))).decode("ascii")
        self.dashboard.reply = Ok({"status": "signed", "payload": text, "signature": signature})


def make_rig(path: Path, machine_id: str | None = None) -> Rig:
    clock = ManualClock()
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    dashboard = Dashboard()
    access = ConsoleAccess(clock=clock, held=None, lockout_path=None)
    said: list[str] = []
    rest = [True]
    sync = OperatorSync(
        clock=clock,
        transport=dashboard,
        access=access,
        path=path,
        public_key=public,
        at_rest=lambda: rest[0],
        say=said.append,
        machine_id=machine_id,
    )
    return Rig(clock, dashboard, access, sync, key, public, said, rest, path)


async def test_accepts_a_signed_list_writes_it_and_asks_again_after_the_period(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path / "operators.json")
    rig.sign(payload())
    await rig.step()
    held = rig.access.held
    assert held is not None
    assert held.operators[0].user_id == OPERATOR_ID
    assert rig.said == ["liste des operateurs recue : 1 operateur(s), 0 tablette(s)"]
    stored = load_list(rig.path, rig.public, None)
    assert stored == Ok(held)
    await rig.step(OPERATORS_PERIOD - 1.0)
    assert rig.dashboard.asked == 1
    # The same holders again: nothing said, its date taken; on the disk within the hour.
    rig.sign(payload(1_790_000_000_500))
    await rig.step(1.0)
    assert rig.dashboard.asked == 2
    assert len(rig.said) == 1
    newer = rig.access.held
    assert newer is not None
    assert newer.issued_at == 1_790_000_000_500
    assert load_list(rig.path, rig.public, None) == Ok(held)
    rig.sign(payload(1_790_000_003_600))
    await rig.step(DATE_WRITE_PERIOD)
    newest = rig.access.held
    assert newest is not None
    assert load_list(rig.path, rig.public, None) == Ok(newest)
    # So a list from between the two, replayed, is refused.
    rig.sign(payload(1_790_000_000_200, operators=0))
    await rig.step(OPERATORS_PERIOD)
    assert rig.access.held == newest


async def test_nothing_is_asked_away_from_rest(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "operators.json")
    rig.rest[0] = False
    await rig.step()
    assert rig.dashboard.asked == 0


async def test_a_session_that_begins_during_the_request_changes_nothing(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "operators.json")
    rig.sign(payload())

    def begin() -> None:
        rig.rest[0] = False

    rig.dashboard.during = begin
    await rig.step()
    assert rig.access.held is None
    rig.rest[0] = True
    rig.dashboard.during = lambda: None
    await rig.step()  # asked again at once, the list never taken
    assert rig.access.held is not None


async def test_an_older_list_is_refused_once_and_the_newer_one_kept(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "operators.json")
    rig.sign(payload(1_790_000_000_000))
    await rig.step()
    rig.sign(payload(1_789_999_999_999, operators=0))
    await rig.step(OPERATORS_PERIOD)
    await rig.step(OPERATORS_PERIOD)
    held = rig.access.held
    assert held is not None
    assert len(held.operators) == 1
    refused = [words for words in rig.said if "pas plus recente" in words]
    assert len(refused) == 1
    # A newer list with fewer operators is taken.
    rig.sign(payload(1_790_000_001_000, operators=0))
    await rig.step(OPERATORS_PERIOD)
    held = rig.access.held
    assert held is not None
    assert held.operators == ()


async def test_a_list_of_the_same_instant_is_taken_only_when_it_is_the_same(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path / "operators.json")
    rig.sign(payload(1_790_000_000_000))
    await rig.step()
    held = rig.access.held
    rig.sign(payload(1_790_000_000_000))
    await rig.step(OPERATORS_PERIOD)
    assert rig.access.held == held
    rig.sign(payload(1_790_000_000_000, operators=0))
    await rig.step(OPERATORS_PERIOD)
    assert rig.access.held == held
    assert any("pas plus recente" in words for words in rig.said)


async def test_a_session_that_begins_during_the_write_keeps_the_list_in_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(tmp_path / "operators.json")
    real = replace_file

    def begin_during(path: Path, content: bytes) -> Result[None, str]:
        rig.rest[0] = False
        return real(path, content)

    monkeypatch.setattr(operator_sync_module, "replace_file", begin_during)
    rig.sign(payload())
    await rig.step()
    # Written, not adopted: the session runs under the list it began with.
    assert rig.access.held is None
    assert rig.path.exists()
    monkeypatch.setattr(operator_sync_module, "replace_file", real)
    rig.rest[0] = True
    await rig.step()  # asked again at the next rest, at once
    assert rig.access.held is not None


async def test_the_file_never_keeps_a_list_the_console_went_back_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2's r8: a list written during a session, then reverted by the site."""
    rig = make_rig(tmp_path / "operators.json")
    rig.sign(payload(1_000, operators=0))  # L1: nobody
    await rig.step()

    def written_then_a_session(path: Path, content: bytes) -> Result[None, str]:
        written = replace_file(path, content)
        rig.rest[0] = False
        return written

    monkeypatch.setattr(operator_sync_module, "replace_file", written_then_a_session)
    rig.sign(payload(2_000, operators=1))  # L2: an operator enabled
    await rig.step(OPERATORS_PERIOD)
    monkeypatch.setattr(operator_sync_module, "replace_file", replace_file)
    held = rig.access.held
    assert held is not None
    assert held.operators == ()  # L1 still in force for that session
    stored = load_list(rig.path, rig.public, None)
    assert isinstance(stored, Ok)
    assert stored.value is not None
    assert len(stored.value.operators) == 1  # L2 on the disk, newer, not yet in force

    rig.rest[0] = True
    rig.sign(payload(3_000, operators=0))  # L3: the admin went back to nobody
    await rig.step(OPERATORS_PERIOD)
    held = rig.access.held
    assert held is not None
    assert (held.issued_at, held.operators) == (3_000, ())
    # What the next start loads is L3, never L2.
    assert load_list(rig.path, rig.public, None) == Ok(held)


async def test_a_date_the_disk_refuses_is_taken_in_memory_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("", encoding="utf-8")
    rig = make_rig(blocked / "operators.json")
    rig.sign(payload())
    await rig.step()
    said = len(rig.said)
    rig.sign(payload(1_790_000_000_500))
    with caplog.at_level(logging.WARNING, logger="src.operator_sync"):
        await rig.step(OPERATORS_PERIOD)
    held = rig.access.held
    assert held is not None
    assert held.issued_at == 1_790_000_000_500
    assert "operator list date not written" in caplog.text
    assert len(rig.said) == said


async def test_a_list_of_another_machine_is_refused(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "operators.json", machine_id="machine-2")
    rig.sign(payload())
    await rig.step()
    assert rig.access.held is None
    assert "liste d'une autre machine" in rig.said[0]
    mine = make_rig(tmp_path / "mine.json", machine_id="machine-1")
    mine.sign(payload())
    await mine.step()
    assert mine.access.held is not None


async def test_a_bad_answer_is_refused_and_said_once(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "operators.json")
    rig.sign(payload())
    reply = rig.dashboard.reply
    assert isinstance(reply, Ok)
    rig.dashboard.reply = Ok({**reply.value, "payload": payload(1)})
    await rig.step()
    await rig.step(OPERATORS_PERIOD)
    assert rig.access.held is None
    assert len(rig.said) == 1
    assert rig.said[0].startswith("liste des operateurs refusee")


async def test_a_signed_text_that_is_not_a_list_is_refused(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "operators.json")
    rig.sign(json.dumps({"catalogRevision": 2, "unchanged": False, "programmes": []}))
    await rig.step()
    assert rig.access.held is None
    assert "n'est pas une liste" in rig.said[0]


async def test_a_signed_text_too_large_is_refused(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "operators.json")
    rig.sign(payload(padding="x" * MAX_PAYLOAD_BYTES))
    await rig.step()
    assert rig.access.held is None
    assert "octets" in rig.said[0]


async def test_nothing_signed_or_no_answer_leaves_the_list_and_says_nothing(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path / "operators.json")
    await rig.step()
    rig.dashboard.reply = Err(Unreachable("down", None))
    await rig.step(OPERATORS_PERIOD)
    assert rig.access.held is None
    assert rig.said == []
    rig.dashboard.reply = Err(Unreachable(TOO_LARGE_ANSWER, 200))
    await rig.step(OPERATORS_PERIOD)
    assert rig.said == [
        "liste des operateurs refusee, la console garde la sienne : "
        "reponse de la liste trop grande pour etre lue"
    ]


async def test_a_list_the_disk_refuses_is_in_force_until_the_next_start(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("", encoding="utf-8")
    rig = make_rig(blocked / "operators.json")
    rig.sign(payload())
    await rig.step()
    assert rig.access.held is not None
    assert rig.said[0].startswith("liste des operateurs non enregistree")
