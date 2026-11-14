"""ANH-141 EX-4, EX-5, EX-8: the machine's configuration, applied at rest, safety by an operator.

Every key here is made at run time and forgotten with the test. The clock is a
:class:`~src.clock.ManualClock`: nothing waits for real time.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from src import config_sync
from src.clock import ManualClock
from src.cloud_sync import TOO_LARGE_ANSWER, Document, Refused, Unreachable
from src.config_sync import (
    APPLIED_PATH,
    CONFIG_PATH,
    CONFIG_PERIOD,
    LEVEL_FILE,
    MACHINE_UNASSIGNED,
    MAX_PAYLOAD_BYTES,
    AppliedConfig,
    ConfigSync,
    applied_environment,
    held_to_level,
    load_applied,
    running_values,
    view_of,
)
from src.config_view import ChangeView
from src.local_config import LocalConfig, load_local_config
from src.machine_config import ConfigValue, ValidationLevel, effective_values, read_offer
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.units import Seconds
from tests.medical_support import signed
from tests.test_machine_config_parity import CASES

type Reply = Result[Document, Unreachable | Refused]

BASE: Final[dict[str, object]] = dict(CASES["base"])
ENV: Final[dict[str, str]] = {"MOTOR_BACKEND": "sim", "ECG_SOURCE": "sim", "ARM_RADIUS_M": "1.5"}
MACHINE: Final[str] = "machine-1"
"""``RECORD_MACHINE_ID`` of the console under test, the machine :func:`payload` signs for."""


def config_of(env: Mapping[str, str]) -> LocalConfig:
    loaded = load_local_config(env, signed())
    assert isinstance(loaded, Ok)
    return loaded.value


def payload(
    values: Mapping[str, object] | None,
    *,
    revision: int = 2,
    issued_at: int = 1_800_000_000_000,
    level: str = "none",
    machine: str = "machine-1",
    **extra: object,
) -> str:
    """The text Convex signs: the values as their canonical text, and its hash."""
    text = (
        None
        if values is None
        else json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
    return json.dumps(
        {
            "kind": "machine_config",
            "machineId": machine,
            "issuedAt": issued_at,
            "validationLevel": level,
            "revision": None if text is None else revision,
            "hash": None if text is None else hashlib.sha256(text.encode()).hexdigest(),
            "values": text,
            **extra,
        }
    )


@dataclass
class Dashboard:
    """The two routes, scripted: what the GET answers, and every POST."""

    reply: Reply = field(default_factory=lambda: Ok({"status": "not_configured"}))
    posted: list[tuple[str, Mapping[str, JsonValue]]] = field(default_factory=list)
    post_reply: Reply = field(default_factory=lambda: Ok({"status": "applied"}))
    asked: int = 0
    meanwhile: Callable[[], None] = lambda: None
    """What happens at the console while the answer is awaited."""

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
        assert path == CONFIG_PATH
        assert params is None
        self.asked += 1
        self.meanwhile()
        return self.reply

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Reply:
        assert path == APPLIED_PATH
        self.posted.append((path, body))
        return self.post_reply


@dataclass
class Rig:
    clock: ManualClock
    dashboard: Dashboard
    sync: ConfigSync
    signer: Ed25519PrivateKey
    public: bytes
    said: list[str]
    rest: list[bool]
    restarts: list[int]
    path: Path
    holds: list[bool]

    async def step(self, seconds: float = 0.0) -> None:
        self.clock.advance(Seconds(seconds))
        await self.sync.step()

    def sign(self, text: str) -> None:
        signature = base64.b64encode(self.signer.sign(text.encode("utf-8"))).decode("ascii")
        self.dashboard.reply = Ok({"status": "signed", "payload": text, "signature": signature})

    def stored(self) -> Result[AppliedConfig | None, str]:
        return load_applied(self.path, self.public, MACHINE)


def make_rig(
    path: Path,
    *,
    current: Mapping[str, ConfigValue] | None = None,
    applied: AppliedConfig | None = None,
    signer: Ed25519PrivateKey | None = None,
    machine_id: str = "machine-1",
    say: Callable[[str], None] | None = None,
) -> Rig:
    clock = ManualClock()
    key = Ed25519PrivateKey.generate() if signer is None else signer
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    dashboard = Dashboard()
    said: list[str] = []
    rest = [True]
    restarts: list[int] = []
    holds: list[bool] = []
    sync = ConfigSync(
        clock=clock,
        transport=dashboard,
        public_key=public,
        path=path,
        at_rest=lambda: rest[0],
        say=said.append if say is None else say,
        machine_id=machine_id,
        local={},
        medical=signed(),
        current=effective_values(config_of(ENV)) if current is None else current,
        applied=applied,
        hold=holds.append,
        restart=lambda: restarts.append(1),
    )
    return Rig(clock, dashboard, sync, key, public, said, rest, restarts, path, holds)


async def applied_rig(tmp_path: Path, values: Mapping[str, object] = BASE) -> Rig:
    """A console that applied revision 2 of ``values``, restarted on it."""
    first = make_rig(tmp_path / "config.applied.json")
    first.sign(payload({**values, "location": "Ailleurs"}, revision=2))
    await first.step()
    loaded = first.stored()
    assert isinstance(loaded, Ok)
    assert loaded.value is not None
    return make_rig(
        first.path, current=loaded.value.values, applied=loaded.value, signer=first.signer
    )


# =========================================================================
# At rest only, and when it is due
# =========================================================================


async def test_nothing_is_asked_away_from_rest(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "c.json")
    rig.rest[0] = False
    await rig.step()
    assert rig.dashboard.asked == 0


async def test_asks_at_the_start_then_once_per_period(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "c.json")
    await rig.step()
    await rig.step(float(CONFIG_PERIOD) - 1.0)
    assert rig.dashboard.asked == 1
    await rig.step(1.0)
    assert rig.dashboard.asked == 2


async def test_nothing_signed_no_answer_or_nothing_approved_changes_nothing(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path / "c.json")
    await rig.step()
    rig.dashboard.reply = Err(Unreachable("timeout"))
    await rig.step(float(CONFIG_PERIOD))
    rig.sign(payload(None))
    await rig.step(float(CONFIG_PERIOD))
    assert rig.said == []
    assert not rig.path.exists()
    assert rig.restarts == []


# =========================================================================
# The signature and the text, before anything is applied
# =========================================================================


@pytest.mark.parametrize(
    ("answer", "words"),
    [
        (Err(Unreachable(TOO_LARGE_ANSWER)), "trop grande"),
        (Ok({"status": "signed", "payload": "{}", "signature": "AAAA"}), "signature"),
        (Ok({"status": "garbled"}), "reponse sans configuration signee"),
    ],
)
async def test_an_answer_that_does_not_verify_is_refused_and_said_once(
    tmp_path: Path, answer: Reply, words: str
) -> None:
    rig = make_rig(tmp_path / "c.json")
    rig.dashboard.reply = answer
    await rig.step()
    await rig.step(float(CONFIG_PERIOD))
    assert len(rig.said) == 1
    assert words in rig.said[0]
    assert not rig.path.exists()


@pytest.mark.parametrize(
    ("text", "words"),
    [
        (json.dumps({"kind": "operator_list"}), "pas une configuration"),
        (payload(BASE, machine="another"), "autre machine"),
        ("[]", "illisible"),
        (payload(BASE, revision=0), "mal formee"),
        (payload(BASE, validationLevel="max"), "mal formee"),
        (payload(BASE)[:-1] + ', "hash": "' + "0" * 64 + '"}', "empreinte"),
        ("x" * (MAX_PAYLOAD_BYTES + 1), "octets"),
    ],
)
async def test_a_signed_text_that_is_not_this_machine_s_configuration_is_refused(
    tmp_path: Path, text: str, words: str
) -> None:
    rig = make_rig(tmp_path / "c.json")
    rig.sign(text)
    await rig.step()
    assert len(rig.said) == 1
    assert words in rig.said[0]
    assert not rig.path.exists()


def test_a_revision_s_values_must_be_an_object_of_text() -> None:
    text = json.dumps(
        {
            "kind": "machine_config",
            "machineId": "m",
            "issuedAt": 1,
            "validationLevel": "none",
            "revision": 1,
            "hash": hashlib.sha256(b"[]").hexdigest(),
            "values": "[]",
        }
    )
    assert read_offer(text, "m") == Err("revision 1 : valeurs illisibles")
    lone = json.dumps({**json.loads(text), "values": "\ud800"})
    assert read_offer(lone, "m") == Err("configuration mal formee")
    assert read_offer("{pas du json", "m") == Err("texte de la configuration illisible")


# =========================================================================
# Revalidated, refused and said back to the dashboard once
# =========================================================================


async def test_a_lock_above_the_signed_level_is_refused_and_said_to_the_dashboard_once(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path / "c.json")
    text = payload({**BASE, "PROGRAMS_ENABLED": True}, level="bench")
    rig.sign(text)
    await rig.step()
    await rig.step(float(CONFIG_PERIOD))
    digest = str(cast("dict[str, object]", json.loads(text))["hash"])
    assert rig.dashboard.posted == [
        (
            APPLIED_PATH,
            {
                "revision": 2,
                "hash": digest,
                "refusal": "PROGRAMS_ENABLED: machine validée jusqu'au niveau bench",
            },
        )
    ]
    assert len(rig.said) == 1
    assert "revision 2 refusee" in rig.said[0]
    assert not rig.path.exists()
    assert rig.sync.view().refusal is not None


async def test_a_refusal_the_dashboard_did_not_take_is_said_again(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "c.json")
    rig.dashboard.post_reply = Err(Unreachable("timeout"))
    rig.sign(payload({**BASE, "MOTOR_MAX_RPM": 5000}))
    await rig.step()
    await rig.step(float(CONFIG_PERIOD))
    assert len(rig.dashboard.posted) == 2


# =========================================================================
# Applied: exploitation at once, safety after an operator at the console
# =========================================================================


async def test_a_change_of_exploitation_keys_is_written_as_received_then_restarts(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path / "data" / "config.applied.json")
    rig.sign(payload(BASE, revision=3))
    await rig.step()
    assert rig.restarts == [1]
    stored = rig.stored()
    assert isinstance(stored, Ok)
    assert stored.value is not None
    assert stored.value.revision.number == 3
    assert stored.value.values["name"] == "Banc d'essai"
    assert "configuration revision 3 appliquee" in rig.said[-1]
    assert rig.holds == [True], "every start refused from before the write to the restart"
    assert (rig.path.stat().st_mode & 0o777) == 0o600
    assert rig.sync.view().restarting
    # Restarting: nothing more is asked.
    await rig.step(float(CONFIG_PERIOD))
    assert rig.dashboard.asked == 1


async def test_a_safety_key_waits_for_an_operator_at_rest(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "c.json")
    rig.sign(payload({**BASE, "MOTOR_MAX_RPM": 400}))
    await rig.step()
    await rig.step(float(CONFIG_PERIOD))
    assert rig.restarts == []
    assert not rig.path.exists()
    pending = rig.sync.view().pending
    assert pending is not None
    assert pending.revision == 2
    rpm = next(change for change in pending.changes if change.key == "MOTOR_MAX_RPM")
    assert (rpm.safety, rpm.before, rpm.after) == (True, "300", "400")
    assert len([words for words in rig.said if "en attente" in words]) == 1
    rig.rest[0] = False
    assert await rig.sync.confirm("op-1") == Err(
        "console pas au repos : terminer la seance avant d'appliquer"
    )
    rig.rest[0] = True
    assert await rig.sync.confirm("op-1") == Ok(2)
    assert rig.restarts == [1]
    assert "confirmee par op-1" in rig.said[-1]
    assert rig.sync.view().pending is None
    assert await rig.sync.confirm("op-1") == Err("aucune configuration de surete en attente")


async def test_a_confirmation_takes_the_level_signed_now_not_the_one_it_came_with(
    tmp_path: Path,
) -> None:
    """R2: a revision fetched just before a downgrade carries the old level: the
    confirmation asks again, and the level of that answer refuses the lock."""
    rig = make_rig(tmp_path / "c.json")
    locked = {**BASE, "PROGRAMS_ENABLED": True}
    rig.sign(payload(locked, level="auto", issued_at=1_000))
    await rig.step()
    assert rig.sync.view().pending is not None
    rig.sign(payload(locked, level="bench", issued_at=2_000))
    refused = await rig.sync.confirm("op-1")
    assert isinstance(refused, Err)
    assert "plus permise" in refused.error
    assert not rig.path.exists()
    assert rig.restarts == []
    assert rig.sync.view().pending is None
    assert [body.get("refusal") for _, body in rig.dashboard.posted] == [
        "PROGRAMS_ENABLED: machine validée jusqu'au niveau bench"
    ]


@pytest.mark.parametrize(
    ("answer", "words"),
    [
        (Err(Unreachable("timeout")), "tableau de bord sans reponse signee"),
        (Ok({"status": "signed", "payload": "{}", "signature": "AAAA"}), "signature"),
    ],
)
async def test_a_confirmation_without_a_signed_answer_now_applies_nothing(
    tmp_path: Path, answer: Reply, words: str
) -> None:
    rig = make_rig(tmp_path / "c.json")
    rig.sign(payload({**BASE, "MOTOR_MAX_RPM": 400}))
    await rig.step()
    rig.dashboard.reply = answer
    refused = await rig.sync.confirm("op-1")
    assert isinstance(refused, Err)
    assert words in refused.error
    assert not rig.path.exists()
    assert rig.sync.view().pending is not None


async def test_a_file_the_disk_refuses_keeps_the_configuration_in_force(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    rig = make_rig(blocker / "c.json")
    rig.sign(payload({**BASE, "MOTOR_MAX_RPM": 400}))
    await rig.step()
    confirmed = await rig.sync.confirm("op-1")
    assert isinstance(confirmed, Err)
    assert rig.restarts == []
    assert "non enregistre" in rig.said[-1]
    assert rig.sync.view().pending is not None
    assert rig.holds == [True, False], "a write that failed lifts the hold: nothing restarts"


async def test_r2_a_write_stopped_by_anything_lifts_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_path: Path, _content: bytes) -> Result[None, str]:
        raise RuntimeError("injected: not an OSError")

    monkeypatch.setattr(config_sync, "replace_file", broken)
    rig = make_rig(tmp_path / "c.json")
    rig.sign(payload(BASE, revision=3))
    with pytest.raises(RuntimeError, match="injected"):
        await rig.step()
    assert rig.holds == [True, False]
    assert rig.restarts == []


async def test_r2_the_restart_is_asked_before_it_is_said(tmp_path: Path) -> None:
    """A line that fails to be written, once the file is: the restart is already asked,
    so the hold it keeps ends with the process."""

    def failing(words: str) -> None:
        if "redemarrage controle" in words:
            raise RuntimeError("injected: the log refuses the line")

    rig = make_rig(tmp_path / "c.json", say=failing)
    rig.sign(payload(BASE, revision=3))
    with pytest.raises(RuntimeError, match="injected"):
        await rig.step()
    assert rig.restarts == [1]
    assert rig.holds == [True]
    assert rig.path.exists()


@pytest.mark.parametrize("safety", [False, True])
async def test_a_session_started_while_the_answer_was_awaited_gets_nothing_written(
    tmp_path: Path, safety: bool
) -> None:
    """Rest is judged again once the answer is in: a start taken meanwhile keeps the
    file, the hold and the restart untouched; a safety revision still waits."""
    rig = make_rig(tmp_path / "c.json")
    values = {**BASE, "MOTOR_MAX_RPM": 400} if safety else BASE
    rig.sign(payload(values))
    if safety:
        await rig.step()
    rig.dashboard.meanwhile = lambda: rig.rest.__setitem__(0, False)
    if safety:
        refused = await rig.sync.confirm("op-1")
        assert isinstance(refused, Err)
        assert rig.sync.view().pending is not None
    else:
        await rig.step()
    assert not rig.path.exists()
    assert (rig.holds, rig.restarts) == ([], [])
    assert "la console n'est plus au repos" in rig.said[-1]


async def test_a_text_older_than_the_last_taken_is_refused(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "c.json")
    rig.sign(payload({**BASE, "MOTOR_MAX_RPM": 400}, issued_at=2_000))
    await rig.step()
    rig.sign(payload({**BASE, "MOTOR_MAX_RPM": 450}, issued_at=1_000))
    await rig.step(float(CONFIG_PERIOD))
    assert "plus ancienne" in rig.said[-1]
    pending = rig.sync.view().pending
    assert pending is not None
    assert next(c.after for c in pending.changes if c.key == "MOTOR_MAX_RPM") == "400"


# =========================================================================
# What runs: the ack, the revision held, the level signed with it
# =========================================================================


async def test_the_revision_applied_is_said_once_and_kept(tmp_path: Path) -> None:
    rig = await applied_rig(tmp_path)
    rig.dashboard.post_reply = Err(Unreachable("timeout"))
    rig.sign(payload({**BASE, "location": "Ailleurs"}, revision=2))
    await rig.step()
    rig.dashboard.post_reply = Ok({"status": "applied"})
    await rig.step(float(CONFIG_PERIOD))
    await rig.step(float(CONFIG_PERIOD))
    acks = [body for _, body in rig.dashboard.posted if "refusal" not in body]
    assert [body["revision"] for body in acks] == [2, 2]
    assert rig.restarts == []
    view = rig.sync.view()
    assert (view.revision, view.location, view.pending) == (2, "Ailleurs", None)


async def test_an_older_revision_than_the_one_applied_is_refused(tmp_path: Path) -> None:
    rig = await applied_rig(tmp_path)
    rig.sign(payload({**BASE, "MOTOR_MAX_RPM": 400}, revision=1))
    await rig.step()
    assert "la console applique la revision 2" in rig.said[-1]
    rig.sign(payload({**BASE, "MOTOR_MAX_RPM": 400}, revision=2))
    await rig.step(float(CONFIG_PERIOD))
    assert rig.restarts == []


async def test_a_lower_signed_level_holds_the_locks_of_the_revision_applied(
    tmp_path: Path,
) -> None:
    values = {**BASE, "PROGRAMS_ENABLED": True}
    first = make_rig(tmp_path / "c.json")
    first.sign(payload({**values, "location": "Ailleurs"}, level="auto"))
    await first.step()
    assert await first.sync.confirm("op-1") == Ok(2)
    loaded = first.stored()
    assert isinstance(loaded, Ok)
    assert loaded.value is not None
    rig = make_rig(
        first.path, current=loaded.value.values, applied=loaded.value, signer=first.signer
    )
    # The same revision, signed again with the level lowered on the registry.
    rig.sign(
        payload({**values, "location": "Ailleurs"}, level="bench", issued_at=1_900_000_000_000)
    )
    await rig.step()
    assert rig.restarts == [1]
    assert "PROGRAMS_ENABLED au dessus du niveau bench : mis a faux" in rig.said[-1]
    kept = rig.stored()
    assert isinstance(kept, Ok)
    assert kept.value is not None
    held, refused = held_to_level(config_of({**ENV, "PROGRAMS_ENABLED": "true"}), kept.value)
    assert refused == ("PROGRAMS_ENABLED",)
    assert held.programs_enabled is False
    # Raised again: the lock held false comes back by nothing of itself. Giving it back is
    # a safety change, shown false -> true, that an operator confirms at rest.
    again = make_rig(
        first.path, current=running_values(kept.value), applied=kept.value, signer=first.signer
    )
    raised = payload({**values, "location": "Ailleurs"}, level="auto", issued_at=1_950_000_000_000)
    again.sign(raised)
    await again.step()
    assert again.restarts == []
    pending = again.sync.view().pending
    assert pending is not None
    assert (pending.revision, pending.changes) == (
        2,
        (ChangeView("PROGRAMS_ENABLED", True, "false", "true"),),
    )
    assert "en attente : cles de surete changees (PROGRAMS_ENABLED)" in again.said[-1]
    await again.step(float(CONFIG_PERIOD))
    assert len(again.said) == 1, "said once"
    assert await again.sync.confirm("op-1") == Ok(2)
    assert again.restarts == [1]
    given = again.stored()
    assert isinstance(given, Ok)
    assert given.value is not None
    assert given.value.level is ValidationLevel.AUTO
    assert held_to_level(config_of({**ENV, "PROGRAMS_ENABLED": "true"}), given.value)[1] == ()


async def test_r2_a_lower_level_signed_for_a_later_revision_holds_the_locks_applied(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R2: revision 2 applied with PROGRAMS_ENABLED at ``auto``; revision 3, lock free,
    waits for an operator; the registry lowers the level. The answer is for revision 3,
    and its level still holds the lock of revision 2: kept next to the file applied, the
    console restarts on it, and revision 3 still waits for its operator."""
    values = {**BASE, "PROGRAMS_ENABLED": True}
    first = make_rig(tmp_path / "c.json")
    first.sign(payload(values, level="auto"))
    await first.step()
    assert await first.sync.confirm("op-1") == Ok(2)
    applied = first.stored()
    assert isinstance(applied, Ok)
    assert applied.value is not None
    rig = make_rig(
        first.path, current=applied.value.values, applied=applied.value, signer=first.signer
    )
    later = {**BASE, "MOTOR_MAX_RPM": 280}
    rig.sign(payload(later, revision=3, level="auto", issued_at=1_850_000_000_000))
    await rig.step()
    assert rig.sync.view().pending is not None
    assert rig.restarts == []
    rig.sign(payload(later, revision=3, level="bench", issued_at=1_900_000_000_000))
    await rig.step(float(CONFIG_PERIOD))
    assert rig.restarts == [1]
    assert "PROGRAMS_ENABLED au dessus du niveau bench : mis a faux" in rig.said[-1]
    kept = rig.stored()
    assert isinstance(kept, Ok)
    assert kept.value is not None
    assert kept.value.revision.number == 2, "the revision applied stays what it is"
    assert kept.value.level is ValidationLevel.BENCH
    held, refused = held_to_level(config_of({**ENV, "PROGRAMS_ENABLED": "true"}), kept.value)
    assert (held.programs_enabled, refused) == (False, ("PROGRAMS_ENABLED",))
    # Restarted on it: the same answer changes nothing more, revision 3 waits as before.
    after = make_rig(first.path, current=kept.value.values, applied=kept.value, signer=first.signer)
    after.sign(payload(later, revision=3, level="bench", issued_at=1_900_000_000_000))
    await after.step()
    assert after.restarts == []
    pending = after.sync.view().pending
    assert pending is not None
    assert pending.revision == 3
    # A level file the console account broke holds every lock: the lowest level.
    (tmp_path / LEVEL_FILE).write_text("{}")
    broken = rig.stored()
    assert isinstance(broken, Ok)
    assert broken.value is not None
    assert broken.value.level is ValidationLevel.NONE
    assert f"data/{LEVEL_FILE}" in caplog.text


async def test_a_level_kept_before_the_revision_applied_no_longer_counts(tmp_path: Path) -> None:
    values = {**BASE, "PROGRAMS_ENABLED": True}
    rig = make_rig(tmp_path / "c.json")
    rig.sign(payload(values, level="auto", issued_at=2_000))
    await rig.step()
    assert await rig.sync.confirm("op-1") == Ok(2)
    old = payload(values, revision=1, level="bench", issued_at=1_000)
    signature = base64.b64encode(rig.signer.sign(old.encode("utf-8"))).decode("ascii")
    (tmp_path / LEVEL_FILE).write_text(
        json.dumps({"schema_version": 1, "payload": old, "signature": signature})
    )
    kept = rig.stored()
    assert isinstance(kept, Ok)
    assert kept.value is not None
    assert (kept.value.level, kept.value.issued_at) == (ValidationLevel.AUTO, 2_000)


def test_held_to_level_leaves_a_env_console_as_it_is() -> None:
    config = config_of(ENV)
    assert held_to_level(config, None) == (config, ())


# =========================================================================
# At every start: the file verified again, the .env's keys left out
# =========================================================================


def test_no_file_is_the_env(tmp_path: Path) -> None:
    assert load_applied(tmp_path / "none.json", None, MACHINE) == Ok(None)
    env = {"MOTOR_MAX_RPM": "300"}
    assert applied_environment(env, None) == (env, ())


@pytest.mark.parametrize(
    ("content", "words"),
    [
        (b"{", "mal forme"),
        (b'{"schema_version": 1, "payload": "{}", "signature": "AAAA"}', "refuse : signature"),
    ],
)
def test_a_file_that_does_not_verify_is_refused(tmp_path: Path, content: bytes, words: str) -> None:
    path = tmp_path / "c.json"
    path.write_bytes(content)
    loaded = load_applied(path, b"not a key", MACHINE)
    assert isinstance(loaded, Err)
    assert loaded.error.startswith(words)


async def test_r2_an_unnamed_machine_reads_no_file_not_even_another_machine_s(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R2: ``RECORD_MACHINE_ID`` not set, a signed configuration of another machine at
    ``occupied`` is never loaded: the console runs on its .env, and says so."""
    values = {**BASE, "PROGRAMS_ENABLED": True, "OCCUPANCY_OCCUPIED_ENABLED": True}
    rig = make_rig(tmp_path / "config.applied.json", machine_id="machine-OTHER")
    rig.sign(payload(values, level="occupied", machine="machine-OTHER"))
    await rig.step()
    assert await rig.sync.confirm("op-1") == Ok(2)
    assert load_applied(rig.path, rig.public, None) == Ok(None)
    assert MACHINE_UNASSIGNED in caplog.text
    assert load_applied(tmp_path / "none.json", rig.public, None) == Ok(None)
    refused = load_applied(rig.path, rig.public, MACHINE)
    assert isinstance(refused, Err)
    assert "configuration d'une autre machine" in refused.error


def test_an_unreadable_file_is_refused(tmp_path: Path) -> None:
    loaded = load_applied(tmp_path, None, MACHINE)
    assert isinstance(loaded, Err)
    assert loaded.error.startswith("illisible")


async def test_a_file_with_no_revision_or_bad_values_is_refused(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "c.json")
    for text, words in (
        (payload(None), "refuse : aucune revision"),
        (payload({**BASE, "MOTOR_MAX_RPM": "x"}), "refuse : MOTOR_MAX_RPM"),
        (payload(BASE, machine="another"), "refuse : configuration d'une autre machine"),
    ):
        signature = base64.b64encode(rig.signer.sign(text.encode())).decode()
        rig.path.write_text(
            json.dumps({"schema_version": 1, "payload": text, "signature": signature})
        )
        loaded = load_applied(rig.path, rig.public, MACHINE)
        assert isinstance(loaded, Err)
        assert loaded.error.startswith(words), loaded.error


async def test_the_env_keeps_its_local_keys_and_loses_the_dashboard_s(tmp_path: Path) -> None:
    rig = await applied_rig(tmp_path)
    loaded = rig.stored()
    assert isinstance(loaded, Ok)
    env = {**ENV, "MOTOR_MAX_RPM": "999", "UI_HOST": "127.0.0.1", "PRESENCE_SOURCE": " "}
    merged, ignored = applied_environment(env, loaded.value)
    assert ignored == ("ARM_RADIUS_M", "ECG_SOURCE", "MOTOR_BACKEND", "MOTOR_MAX_RPM")
    assert merged["MOTOR_MAX_RPM"] == "300"
    assert merged["UI_HOST"] == "127.0.0.1"


def test_the_view_of_a_env_console_names_nothing() -> None:
    view = view_of(None, effective_values(config_of(ENV)))
    assert (view.revision, view.config_hash, view.name, view.location) == (None, None, None, None)
