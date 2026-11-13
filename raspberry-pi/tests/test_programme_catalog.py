"""The signed programme catalogue, on the console's side (ANH-138).

EX-2: asked at rest only, at the start then once a minute at most; the
signature checked before anything of the text is read; an invalid one said
in the logbook and ignored, the programmes left as they were. EX-3: every
programme judged again with this console's rules, the accepted ones written
over the store at once, the refused ones named with their reason. EX-5: a
session never sees the programmes change. EX-7: the tests of each, on a
:class:`~src.clock.ManualClock`.

Every key is generated here, at run time, and forgotten with the test: no
private key is written anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Final, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from src import local_panel
from src.clock import ManualClock
from src.cloud_sync import (
    LAUNCH_TIMEOUT,
    MAX_RESPONSE_BYTES,
    TOO_LARGE_ANSWER,
    CloudSync,
    Document,
    Refused,
    Unreachable,
)
from src.local_config import CardiacTiers, load_local_config
from src.local_panel import build_panel
from src.medical import PUBLIC_KEY_PATH as MEDICAL_PUBLIC_KEY_PATH
from src.medical import SHIPPED_DOCUMENT, SHIPPED_PARAMETERS, MedicalVersion
from src.programme_catalog import (
    CATALOG_PATH,
    CATALOG_PERIOD,
    DUPLICATE,
    MAX_ID_CHARS,
    MAX_PAYLOAD_BYTES,
    MAX_REASON_CHARS,
    MAX_REPORT_ENTRIES,
    PUBLIC_KEY_PATH,
    CatalogPayload,
    LocalRules,
    ProgrammeCatalog,
    RejectedProgramme,
    read_payload,
    read_public_key,
    review,
    review_all,
    rules_of,
    screen_stored,
    signed_text,
)
from src.result import Err, Ok, Result
from src.training.plan import (
    SHIPPED_DEFAULTS_PATH,
    CatalogStamp,
    DefaultsUnusable,
    JsonValue,
    ProfileStore,
    StoreContent,
    TrainingProfile,
    parse_store,
)
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import Monotonic, MotorRpm, Seconds, UnixMillis
from tests.medical_support import public_pem, signed, unsigned
from tests.test_cloud_sync import (
    LAUNCH,
    LINKED_ENV,
    OPERATOR,
    POLL_PATH,
    START,
    config_of,
    count,
    launch_answer,
    ok,
)
from tests.test_cloud_sync import Dashboard as LinkDashboard
from tests.test_cloud_sync import Rig as CloudRig
from tests.test_cloud_sync import linked as linked_rig
from tests.test_cloud_sync import rig as cloud_rig
from tests.test_contract import shared
from tests.test_failure_process import cruising
from tests.test_failure_rig import make_rig as failure_rig
from tests.test_programme_validation_parity import CASES
from tests.test_remote_bench_launch import BENCH, confirm_over_http

VERSION: Final[MedicalVersion] = SHIPPED_DOCUMENT.version
TIERS: Final[CardiacTiers] = CardiacTiers(
    SHIPPED_PARAMETERS.hr_hard_max_bpm, SHIPPED_PARAMETERS.hr_critical_bpm
)
RULES: Final[LocalRules] = LocalRules(
    tiers=TIERS,
    min_run=MotorRpm(55),
    ceiling=Ok(MotorRpm(800)),
    medical_version=SHIPPED_DOCUMENT.version,
    medical_record=SHIPPED_DOCUMENT.version,
)


def programme(**changes: object) -> dict[str, object]:
    """A programme as Convex sends it: the shipped standard, on this machine's tiers."""
    defaults: object = json.loads(SHIPPED_DEFAULTS_PATH.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]  # narrowed below
    assert isinstance(defaults, dict)
    first = cast("list[dict[str, object]]", cast("dict[str, object]", defaults)["profiles"])[0]
    return {**first, "medical_parameters_version": VERSION, **changes}


def text_of(
    revision: int, programmes: Sequence[Mapping[str, object]], *, unchanged: bool = False
) -> str:
    return json.dumps(
        {"catalogRevision": revision, "unchanged": unchanged, "programmes": list(programmes)}
    )


def answer(key: Ed25519PrivateKey, text: str) -> Document:
    """A signed answer of the route: the text, and ``key``'s signature of its bytes."""
    signature = base64.b64encode(key.sign(text.encode("utf-8"))).decode("ascii")
    return {"status": "signed", "payload": text, "signature": signature}


type Reply = Result[Document, Unreachable | Refused]


@dataclass
class Dashboard:
    """The route, scripted: one reply, and every request it was asked with."""

    reply: Reply = field(default_factory=lambda: Ok({"status": "not_configured"}))
    asked: list[Mapping[str, str] | None] = field(default_factory=list[Mapping[str, str] | None])
    during: Callable[[], None] = lambda: None

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
        assert path == CATALOG_PATH
        self.asked.append(params)
        self.during()
        return self.reply

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Reply:
        raise AssertionError(f"the catalogue posts nothing ({path}, {body})")


@dataclass
class Rig:
    clock: ManualClock
    dashboard: Dashboard
    store: ProfileStore
    catalog: ProgrammeCatalog
    signer: Ed25519PrivateKey
    said: list[str]
    rest: list[bool]
    path: Path

    async def step(self, seconds: float = 0.0) -> None:
        self.clock.advance(Seconds(seconds))
        await self.catalog.step()

    def sign(self, revision: int, programmes: Sequence[Mapping[str, object]]) -> None:
        self.dashboard.reply = Ok(answer(self.signer, text_of(revision, programmes)))

    def on_disk(self) -> StoreContent:
        parsed = parse_store(self.path.read_text(encoding="utf-8"))
        assert isinstance(parsed, Ok)
        return parsed.value


def make_rig(
    tmp_path: Path,
    *,
    rules: LocalRules = RULES,
    stored: StoreContent | None = None,
    public_key: bytes | Ed25519PrivateKey | None = None,
) -> Rig:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "profiles.json"
    store = ProfileStore(path, SHIPPED_DEFAULTS_PATH)
    assert isinstance(store.load(), Ok)
    if stored is not None:
        store.adopt(stored)
    said: list[str] = []
    rest = [True]
    dashboard = Dashboard()
    clock = ManualClock(Monotonic(100.0), UnixMillis(1_700_000_000_000))
    trusted = (
        public_pem(key)
        if public_key is None
        else public_pem(public_key)
        if isinstance(public_key, Ed25519PrivateKey)
        else public_key
    )
    catalog = ProgrammeCatalog(
        clock=clock,
        transport=dashboard,
        store=store,
        rules=rules,
        at_rest=lambda: rest[0],
        say=said.append,
        public_key=trusted,
        report=screen_stored(store, rules, said.append),
    )
    return Rig(clock, dashboard, store, catalog, key, said, rest, path)


def ids(r: Rig) -> list[str]:
    return [p.profile_id for p in r.store.list_profiles()]


# =========================================================================
# The shared file
# =========================================================================


def test_the_route_cadence_bounds_and_key_are_the_shared_file_s() -> None:
    entry = shared()["programme_catalog"]
    assert isinstance(entry, dict)
    section = cast("dict[str, object]", entry)
    assert section["route"] == f"GET {CATALOG_PATH}"
    assert section["since"] == "1.11"
    assert section["poll_period_s"] == CATALOG_PERIOD
    assert section["max_report_entries"] == MAX_REPORT_ENTRIES
    assert section["max_reason_chars"] == MAX_REASON_CHARS
    assert section["max_payload_bytes"] == MAX_PAYLOAD_BYTES
    # The text, escaped once inside the answer, fits under what the transport reads.
    assert 2 * MAX_PAYLOAD_BYTES + 4096 <= MAX_RESPONSE_BYTES
    assert Path(__file__).resolve().parents[2] / str(section["public_key"]) == PUBLIC_KEY_PATH


def test_the_shipped_key_is_an_ed25519_public_key_and_not_the_medical_one() -> None:
    shipped = read_public_key()
    assert shipped is not None
    assert isinstance(load_pem_public_key(shipped), Ed25519PublicKey)
    assert shipped != MEDICAL_PUBLIC_KEY_PATH.read_bytes()
    assert b"PRIVATE" not in shipped


def test_a_public_key_that_cannot_be_read_is_none(tmp_path: Path) -> None:
    assert read_public_key(tmp_path / "absent.pub") is None


# =========================================================================
# EX-2: at rest only, at the start, then once a minute at most
# =========================================================================


async def test_the_first_request_asks_for_everything_then_since_the_revision_held(
    tmp_path: Path,
) -> None:
    r = make_rig(tmp_path)
    r.sign(4, [programme()])
    await r.step()
    assert r.dashboard.asked == [None]
    await r.step(CATALOG_PERIOD - 1)
    assert len(r.dashboard.asked) == 1
    await r.step(1)
    assert r.dashboard.asked == [None, {"since": "4"}]


async def test_nothing_is_asked_during_a_session(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    r.rest[0] = False
    for _ in range(10):
        await r.step(CATALOG_PERIOD)
    assert r.dashboard.asked == []
    r.rest[0] = True
    await r.step(1)
    assert r.dashboard.asked == [None]


async def test_a_session_that_begins_during_the_request_changes_nothing(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    before = (r.store.rev, r.store.list_profiles(), r.store.catalog)
    r.sign(4, [programme(profile_id="new_one")])

    def session_begins() -> None:
        r.rest[0] = False

    r.dashboard.during = session_begins
    await r.step()
    assert (r.store.rev, r.store.list_profiles(), r.store.catalog) == before
    assert not r.path.exists()
    assert r.catalog.report.revision is None
    # Asked again at the next rest, without waiting a period.
    r.dashboard.during = lambda: None
    r.rest[0] = True
    await r.step(1)
    assert len(r.dashboard.asked) == 2
    assert ids(r) == ["new_one"]


# =========================================================================
# EX-2: the signature, before anything is read
# =========================================================================


async def test_an_invalid_signature_is_said_once_and_ignored(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    shipped = ids(r)
    stranger = Ed25519PrivateKey.generate()
    r.dashboard.reply = Ok(answer(stranger, text_of(4, [programme(profile_id="forged")])))
    await r.step()
    await r.step(CATALOG_PERIOD)
    assert ids(r) == shipped
    assert not r.path.exists()
    assert r.said == [
        "catalogue refuse, programmes inchanges : signature du catalogue invalide : "
        "la signature ne correspond pas au fichier"
    ]
    assert r.catalog.report.revision is None


async def test_one_byte_changed_in_the_text_is_refused(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    good = answer(r.signer, text_of(4, [programme(max_rpm=276)]))
    r.dashboard.reply = Ok({**good, "payload": cast("str", good["payload"]).replace("276", "999")})
    await r.step()
    assert len(r.said) == 1
    assert "signature du catalogue invalide" in r.said[0]
    assert not r.path.exists()


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ({"status": "signed", "signature": "AA=="}, "reponse du catalogue sans texte signe"),
        ({"status": "signed", "payload": "{}"}, "reponse du catalogue sans texte signe"),
        ({"payload": "{}", "signature": "AA=="}, "reponse du catalogue sans texte signe"),
        (
            {"status": "signed", "payload": "{}", "signature": "not base64!"},
            "signature du catalogue invalide : texte ou signature illisible",
        ),
        (
            {"status": "signed", "payload": "\ud800", "signature": "AA=="},
            "signature du catalogue invalide : texte ou signature illisible",
        ),
    ],
)
def test_an_answer_without_a_readable_signed_text_is_refused(
    document: Document, reason: str
) -> None:
    assert signed_text(document, public_pem(Ed25519PrivateKey.generate())) == Err(reason)


def test_without_a_public_key_every_signature_is_refused() -> None:
    key = Ed25519PrivateKey.generate()
    assert signed_text(answer(key, "{}"), None) == Err(
        "cle publique anheart_catalog.pub absente ou illisible"
    )


def test_a_public_key_that_is_not_one_refuses_the_signature() -> None:
    key = Ed25519PrivateKey.generate()
    refused = signed_text(answer(key, "{}"), b"not a key")
    assert isinstance(refused, Err)
    assert refused.error.startswith("signature du catalogue invalide : cle publique illisible")


async def test_nothing_signed_or_no_answer_leaves_the_programmes_and_says_nothing(
    tmp_path: Path,
) -> None:
    r = make_rig(tmp_path)
    shipped = ids(r)
    for reply in (
        Ok({"status": "not_configured"}),
        Err(Unreachable("no route to host")),
        # A dashboard before contract 1.11: the route is unknown.
        Err(Refused(404, "HTTP 404")),
    ):
        r.dashboard.reply = reply
        await r.step(CATALOG_PERIOD)
    assert len(r.dashboard.asked) == 3
    assert ids(r) == shipped
    assert r.said == []
    assert not r.path.exists()


# =========================================================================
# The signed text, read
# =========================================================================


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("not json", "texte du catalogue illisible"),
        ("[]", "texte du catalogue mal forme"),
        (
            '{"catalogRevision": true, "unchanged": false, "programmes": []}',
            "texte du catalogue mal forme",
        ),
        (
            '{"catalogRevision": -1, "unchanged": false, "programmes": []}',
            "texte du catalogue mal forme",
        ),
        (
            '{"catalogRevision": "4", "unchanged": false, "programmes": []}',
            "texte du catalogue mal forme",
        ),
        (
            '{"catalogRevision": 4, "unchanged": 0, "programmes": []}',
            "texte du catalogue mal forme",
        ),
        (
            '{"catalogRevision": 4, "unchanged": false, "programmes": {}}',
            "texte du catalogue mal forme",
        ),
        (
            '{"catalogRevision": 4, "unchanged": false, "programmes": [7]}',
            "texte du catalogue mal forme",
        ),
    ],
)
def test_a_signed_text_of_another_shape_is_refused(text: str, reason: str) -> None:
    assert read_payload(text) == Err(reason)


def test_a_signed_text_is_read_with_its_programmes() -> None:
    assert read_payload(text_of(4, [{"profile_id": "a"}])) == Ok(
        CatalogPayload(revision=4, unchanged=False, programmes=({"profile_id": "a"},))
    )


async def test_a_signed_text_of_another_shape_is_said_and_ignored(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    r.dashboard.reply = Ok(answer(r.signer, "[]"))
    await r.step()
    assert r.said == ["catalogue refuse, programmes inchanges : texte du catalogue mal forme"]
    assert not r.path.exists()


async def test_unchanged_at_the_revision_held_changes_nothing(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    r.sign(4, [programme()])
    await r.step()
    rev = r.store.rev
    r.dashboard.reply = Ok(answer(r.signer, text_of(4, [], unchanged=True)))
    await r.step(CATALOG_PERIOD)
    assert r.store.rev == rev
    assert ids(r) == ["standard_30_min"]
    assert r.said == []


async def test_unchanged_at_another_revision_is_said_and_ignored(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    r.dashboard.reply = Ok(answer(r.signer, text_of(9, [], unchanged=True)))
    await r.step()
    assert r.said == [
        "catalogue refuse, programmes inchanges : catalogue dit inchange a la revision 9"
    ]
    assert not r.path.exists()


# =========================================================================
# EX-3: every programme judged again, the accepted ones written at once
# =========================================================================


async def test_the_accepted_programmes_replace_the_store_and_the_refused_are_named(
    tmp_path: Path,
) -> None:
    r = make_rig(tmp_path)
    assert "standard_45_min" in ids(r)  # the shipped fallback, before any catalogue
    r.sign(
        4,
        [
            programme(profile_id="jog_court", name="Jog court"),
            programme(profile_id="trop_vite", max_rpm=900),
        ],
    )
    await r.step()
    assert ids(r) == ["jog_court"]
    assert r.store.catalog == CatalogStamp(revision=4, medical=VERSION)
    on_disk = r.on_disk()
    assert [p.profile_id for p in on_disk.profiles] == ["jog_court"]
    assert on_disk.catalog == CatalogStamp(revision=4, medical=VERSION)
    assert r.catalog.wire_fields() == {
        "catalogRevision": 4,
        "accepted": ["jog_court"],
        "rejected": [
            {
                "profileId": "trop_vite",
                "reason": "max_rpm 900 tr/min moteur au-dessus du plafond personne a bord "
                "(800 tr/min)",
            }
        ],
    }


async def test_a_withdrawn_programme_goes_at_the_next_update_and_an_empty_catalogue_empties(
    tmp_path: Path,
) -> None:
    r = make_rig(tmp_path)
    r.sign(4, [programme(profile_id="a_one"), programme(profile_id="b_two")])
    await r.step()
    held = r.store.get("a_one")
    assert isinstance(held, Ok)
    frozen = r.store.resolve("a_one", at=UnixMillis(0))
    r.sign(6, [programme(profile_id="b_two")])
    await r.step(CATALOG_PERIOD)
    assert ids(r) == ["b_two"]
    # What a session already froze is its own copy: the withdrawal does not touch it.
    assert isinstance(frozen, Ok)
    assert frozen.value.profile == held.value
    r.sign(7, [])
    await r.step(CATALOG_PERIOD)
    assert ids(r) == []
    assert r.on_disk().profiles == ()
    assert r.catalog.report.revision == 7


async def test_the_same_catalogue_again_is_not_written_again(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    r.sign(4, [programme()])
    await r.step()
    rev = r.store.rev
    written = r.path.stat().st_mtime_ns
    # A console restarted asks for the whole list again: same revision, same programmes.
    restarted = ProgrammeCatalog(
        clock=r.clock,
        transport=r.dashboard,
        store=r.store,
        rules=RULES,
        at_rest=lambda: True,
        say=r.said.append,
        public_key=public_pem(r.signer),
        report=screen_stored(r.store, RULES, r.said.append),
    )
    assert restarted.report.accepted == ("standard_30_min",)
    await restarted.step()
    assert r.dashboard.asked[-1] is None
    assert r.store.rev == rev
    assert r.path.stat().st_mtime_ns == written
    assert r.said == []


async def test_a_store_that_cannot_be_written_keeps_the_programmes_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = make_rig(tmp_path)
    shipped = ids(r)

    def refuse(*_args: object, **_kwargs: object) -> Path:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "replace", refuse)
    r.sign(4, [programme(profile_id="new_one")])
    await r.step()
    assert ids(r) == shipped
    assert r.catalog.report.revision is None
    assert len(r.said) == 1
    assert r.said[0].startswith("catalogue 4 non enregistre, programmes inchanges : ")
    monkeypatch.undo()
    await r.step(CATALOG_PERIOD)
    # Nothing was held: the whole list is asked for again.
    assert r.dashboard.asked[-1] is None
    assert ids(r) == ["new_one"]


async def test_a_programme_the_site_refuses_is_refused_with_the_same_words() -> None:
    for case in CASES["cases"]:
        expected = case["expected"]
        if expected["verdict"] == "accepted":
            continue
        document = {**CASES["base"], **case["changes"], "medical_parameters_version": VERSION}
        assert review(document, RULES) == Err(expected.get("detail")), case["name"]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"medical_parameters_version": "0.0.9"},
            f"valide pour les parametres medicaux '0.0.9', la machine a la version {VERSION}",
        ),
        (
            {},
            None,
        ),
        (
            {"hard_max_bpm": 165, "critical_bpm": 175, "subject_hr_max": 175},
            f"paliers 165/175 bpm, ceux de la machine sont {TIERS.hard_max_bpm}/"
            f"{TIERS.critical_bpm} bpm",
        ),
        (
            {"critical_bpm": 170},
            f"paliers {TIERS.hard_max_bpm}/170 bpm, ceux de la machine sont "
            f"{TIERS.hard_max_bpm}/{TIERS.critical_bpm} bpm",
        ),
        (
            {"min_run_rpm": 30},
            "min_run_rpm 30 sous la plus petite consigne des limites de mouvement signees "
            "(55 tr/min moteur)",
        ),
        (
            {"max_rpm": 1500, "allow_above_nameplate": True},
            "max_rpm 1500 au-dessus de la plaque moteur (1380 tr/min moteur)",
        ),
        (
            {"max_rpm": 801},
            "max_rpm 801 tr/min moteur au-dessus du plafond personne a bord (800 tr/min)",
        ),
    ],
)
def test_each_rule_of_this_machine_refuses_with_its_reason(
    changes: Mapping[str, object], reason: str | None
) -> None:
    reviewed = review(programme(**changes), RULES)
    if reason is None:
        assert isinstance(reviewed, Ok)
    else:
        assert reviewed == Err(reason)


def test_unsigned_medical_parameters_accept_no_programme() -> None:
    bench = LocalRules(
        tiers=TIERS,
        min_run=MotorRpm(55),
        ceiling=Ok(MotorRpm(800)),
        medical_version=None,
        medical_record="unsigned-sha256:0",
    )
    assert review(programme(), bench) == Err(
        "parametres medicaux non signes : aucun programme du catalogue n'est accepte"
    )


def test_no_ceiling_with_a_person_on_board_accepts_no_programme() -> None:
    closed = LocalRules(
        tiers=TIERS,
        min_run=MotorRpm(55),
        ceiling=Err("personne a bord refusee : OCCUPANCY_OCCUPIED_ENABLED=false"),
        medical_version=VERSION,
        medical_record=VERSION,
    )
    assert review(programme(), closed) == Err(
        "personne a bord refusee : OCCUPANCY_OCCUPIED_ENABLED=false"
    )


def test_an_integer_beyond_every_float_is_a_malformed_programme_not_an_exception() -> None:
    """``json.loads`` reads an integer of any size; ``float(10**400)`` would raise (ANH-137)."""
    refused = review(programme(max_rpm=10**400), RULES)
    assert isinstance(refused, Err)
    assert "'max_rpm' must be a whole number a float can hold, got int" in refused.error
    assert read_payload('{"catalogRevision": 1, "unchanged": false, "programmes": []}') == Ok(
        CatalogPayload(revision=1, unchanged=False, programmes=())
    )


def test_the_programme_at_the_bounds_is_accepted() -> None:
    accepted = review(programme(max_rpm=800, min_run_rpm=55), RULES)
    assert isinstance(accepted, Ok)
    assert accepted.value.max_rpm == 800


def test_an_id_held_twice_is_refused_twice_and_a_refused_one_is_named() -> None:
    accepted, rejected = review_all(
        [
            programme(profile_id="twice"),
            programme(profile_id="twice", name="other"),
            programme(profile_id="kept"),
            {"profile_id": 7},
            {"profile_id": "x" * 100, "medical_parameters_version": "0"},
            {"profile_id": "jog_\ud800", "medical_parameters_version": "0"},
        ],
        RULES,
    )
    assert [p.profile_id for p in accepted] == ["kept"]
    assert rejected[:2] == (
        RejectedProgramme("twice", DUPLICATE),
        RejectedProgramme("twice", DUPLICATE),
    )
    assert rejected[2].profile_id == "7"
    assert rejected[3].profile_id == "x" * 64
    # Named in the report's JSON: a surrogate has no UTF-8, and is said as U+FFFD.
    assert rejected[4].profile_id == "jog_\ufffd"
    assert rejected[4].profile_id.encode("utf-8") == b"jog_\xef\xbf\xbd"


def test_a_long_reason_is_bounded() -> None:
    _, rejected = review_all([{"medical_parameters_version": VERSION}], RULES)
    assert len(rejected[0].reason) == MAX_REASON_CHARS
    assert rejected[0].reason.endswith("...")
    assert rejected[0].profile_id == "None"


async def test_the_report_said_to_the_dashboard_is_bounded(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    too_fast = [programme(profile_id=f"p{n}", max_rpm=900) for n in range(MAX_REPORT_ENTRIES + 5)]
    r.sign(4, [programme(profile_id="kept"), *too_fast])
    await r.step()
    assert len(r.catalog.report.rejected) == MAX_REPORT_ENTRIES + 5
    fields = r.catalog.wire_fields()
    assert fields["accepted"] == ["kept"]
    assert len(cast("list[object]", fields["rejected"])) == MAX_REPORT_ENTRIES - 1


# =========================================================================
# What a restart finds in the store
# =========================================================================


def test_a_store_never_from_a_catalogue_is_the_fallback(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    assert r.catalog.wire_fields() == {"catalogRevision": None, "accepted": [], "rejected": []}


def test_a_store_from_a_catalogue_under_these_parameters_is_kept(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "x.json", SHIPPED_DEFAULTS_PATH)
    assert isinstance(store.load(), Ok)
    content = StoreContent(
        rev=store.rev,
        profiles=store.list_profiles()[:1],
        catalog=CatalogStamp(revision=3, medical=VERSION),
    )
    r = make_rig(tmp_path, stored=content)
    assert r.catalog.wire_fields() == {
        "catalogRevision": 3,
        "accepted": ["standard_30_min"],
        "rejected": [],
    }
    assert r.said == []


async def test_a_store_from_a_catalogue_under_other_parameters_is_set_aside(
    tmp_path: Path,
) -> None:
    store = ProfileStore(tmp_path / "x.json", SHIPPED_DEFAULTS_PATH)
    assert isinstance(store.load(), Ok)
    content = StoreContent(
        rev=store.rev,
        profiles=store.list_profiles()[:1],
        catalog=CatalogStamp(revision=3, medical="0.0.9"),
    )
    r = make_rig(tmp_path, stored=content)
    reason = f"accepte sous les parametres medicaux 0.0.9, la machine a {VERSION}"
    assert ids(r) == []
    assert r.catalog.report.rejected == (RejectedProgramme("standard_30_min", reason),)
    assert r.said == [f"catalogue 3 ecarte jusqu'au prochain : {reason}"]
    # The next answer is the whole list, judged with this start's rules.
    r.sign(3, [programme()])
    await r.step()
    assert r.dashboard.asked == [None]
    assert ids(r) == ["standard_30_min"]
    assert r.on_disk().catalog == CatalogStamp(revision=3, medical=VERSION)


def test_a_programme_this_start_refuses_is_set_aside_and_said(tmp_path: Path) -> None:
    """A lower ceiling since the catalogue was accepted: never listed, never reported accepted."""
    store = ProfileStore(tmp_path / "x.json", SHIPPED_DEFAULTS_PATH)
    assert isinstance(store.load(), Ok)
    stored = store.list_profiles()
    content = StoreContent(
        rev=store.rev, profiles=stored, catalog=CatalogStamp(revision=3, medical=VERSION)
    )
    lower = LocalRules(
        tiers=TIERS,
        min_run=MotorRpm(55),
        ceiling=Ok(MotorRpm(200)),
        medical_version=VERSION,
        medical_record=VERSION,
    )
    r = make_rig(tmp_path, rules=lower, stored=content)
    reason = "max_rpm 276 tr/min moteur au-dessus du plafond personne a bord (200 tr/min)"
    assert ids(r) == []
    assert r.catalog.wire_fields() == {
        "catalogRevision": 3,
        "accepted": [],
        "rejected": [{"profileId": p.profile_id, "reason": reason} for p in stored],
    }
    assert r.said == [
        f"programme {p.profile_id} ecarte jusqu'au prochain catalogue : {reason}" for p in stored
    ]
    # In memory only: the file keeps what was accepted.
    assert not r.path.exists()


def test_a_programme_this_start_still_accepts_is_kept_beside_one_it_refuses(
    tmp_path: Path,
) -> None:
    store = ProfileStore(tmp_path / "x.json", SHIPPED_DEFAULTS_PATH)
    assert isinstance(store.load(), Ok)
    slow = review(programme(profile_id="slow_one", max_rpm=150), RULES)
    fast = review(programme(profile_id="fast_one", max_rpm=276), RULES)
    assert isinstance(slow, Ok)
    assert isinstance(fast, Ok)
    stamp = CatalogStamp(revision=5, medical=VERSION)
    store.adopt(StoreContent(rev=store.rev, profiles=(slow.value, fast.value), catalog=stamp))
    lower = LocalRules(
        tiers=TIERS,
        min_run=MotorRpm(55),
        ceiling=Ok(MotorRpm(200)),
        medical_version=VERSION,
        medical_record=VERSION,
    )
    report = screen_stored(store, lower, lambda _words: None)
    assert [p.profile_id for p in store.list_profiles()] == ["slow_one"]
    assert store.catalog == stamp
    assert report.accepted == ("slow_one",)
    assert [x.profile_id for x in report.rejected] == ["fast_one"]


def unlinked_env(**changes: str) -> dict[str, str]:
    env = {k: v for k, v in LINKED_ENV.items() if k not in ("MACHINE_API_KEY", "CONVEX_URL")}
    return {**env, **changes}


def stamped_store(path: Path, medical: str) -> None:
    """The shipped profiles, written as a catalogue accepted under ``medical``."""
    decoded: object = json.loads(SHIPPED_DEFAULTS_PATH.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]  # narrowed below
    assert isinstance(decoded, dict)
    document = {
        **cast("dict[str, object]", decoded),
        "rev": 4,
        "catalog": {"revision": 9, "medical": medical},
    }
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("linked", [False, True])
def test_every_start_screens_the_store_linked_or_not(tmp_path: Path, linked: bool) -> None:
    """ANH-138: a console with no dashboard does not keep a catalogue it no longer accepts."""
    path = tmp_path / "p.json"
    stamped_store(path, "0.0.9")
    env = dict(LINKED_ENV) if linked else unlinked_env()
    loaded = load_local_config(env, signed())
    assert isinstance(loaded, Ok), loaded
    panel = build_panel(
        loaded.value,
        clock=ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000)),
        profiles_path=path,
        transport=lambda _c: LinkDashboard(),
    )
    store = panel.services.store
    assert store.catalog == CatalogStamp(revision=9, medical="0.0.9")
    assert store.list_profiles() == ()
    assert (panel.catalog is not None) is linked
    if panel.catalog is not None:
        assert panel.catalog.report.accepted == ()
        assert len(panel.catalog.report.rejected) == 2


async def test_a_console_without_a_person_on_board_keeps_no_programme_and_still_runs_a_bench(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OCCUPANCY_OCCUPIED_ENABLED=false: the catalogue leaves no programme; a bench launch
    from the site still arms, its start reading no programme."""
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(local_panel, "read_public_key", lambda: public_pem(key))
    rig_ = linked_rig(tmp_path, signed(), OCCUPANCY_OCCUPIED_ENABLED="false")
    store = rig_.panel.services.store
    assert [p.profile_id for p in store.list_profiles()] == ["standard_30_min", "standard_45_min"]
    rig_.dashboard.answer(CATALOG_PATH, Ok(answer(key, text_of(2, [programme()]))))
    await rig_.panel.catalog_step()
    assert store.list_profiles() == ()
    catalog = rig_.panel.catalog
    assert catalog is not None
    assert catalog.report.rejected[0].reason.startswith(
        "personne a bord refusee : OCCUPANCY_OCCUPIED_ENABLED=false"
    )
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await rig_.run(0.2)
    attested = await confirm_over_http(rig_, "Console Operator", attested=True)
    assert attested.status_code == HTTPStatus.ACCEPTED
    await rig_.run(1.8)
    assert rig_.panel.runtime.state is RuntimeState.RUNNING
    await rig_.panel.close()


def test_an_unlinked_start_sets_aside_what_its_ceiling_now_refuses(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    stamped_store(path, VERSION)
    loaded = load_local_config(unlinked_env(OCCUPANCY_OCCUPIED_ENABLED="false"), signed())
    assert isinstance(loaded, Ok), loaded
    panel = build_panel(
        loaded.value,
        clock=ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000)),
        profiles_path=path,
    )
    assert panel.catalog is None
    assert panel.services.store.list_profiles() == ()


# =========================================================================
# Nothing published yet, an answer too large, a launch that timed out
# =========================================================================


async def test_a_catalogue_nothing_was_ever_published_in_is_no_catalogue(tmp_path: Path) -> None:
    """A key set before anything is published empties no console of its fallback."""
    r = make_rig(tmp_path)
    shipped = ids(r)
    r.sign(0, [])
    await r.step()
    assert ids(r) == shipped
    assert not r.path.exists()
    assert r.said == []
    assert r.catalog.report.revision is None
    await r.step(CATALOG_PERIOD)
    assert r.dashboard.asked == [None, {"since": "0"}]
    # The first publication is then taken as any catalogue.
    r.sign(1, [programme(profile_id="first_one")])
    await r.step(CATALOG_PERIOD)
    assert ids(r) == ["first_one"]


LEFT: Final[str] = (
    "catalogue 5 retire : ce tableau de bord n'en publie aucun, retour aux programmes de secours"
)


async def test_a_console_moved_to_a_deployment_that_published_nothing_goes_back_to_its_fallback(
    tmp_path: Path,
) -> None:
    """Revision 5 of one deployment, then revision 0 of another signed by the same key: the
    first one's programmes are neither kept nor reported, and a restart keeps the fallback."""
    r = make_rig(tmp_path)
    shipped = ids(r)
    r.sign(5, [programme(profile_id="dev_only")])
    await r.step()
    assert ids(r) == ["dev_only"]
    r.sign(0, [])
    await r.step(CATALOG_PERIOD)
    assert ids(r) == shipped
    assert r.store.catalog is None
    on_disk = r.on_disk()
    assert (sorted(p.profile_id for p in on_disk.profiles), on_disk.catalog) == (shipped, None)
    assert r.catalog.wire_fields()["catalogRevision"] is None
    assert r.said == [LEFT]
    # Said once: the next answer finds no catalogue to leave.
    await r.step(CATALOG_PERIOD)
    assert r.said == [LEFT]
    assert r.dashboard.asked[-1] == {"since": "0"}
    # Restarted: a store without a catalogue is left as it is.
    again = make_rig(tmp_path, stored=r.on_disk())
    again.sign(0, [])
    await again.step()
    assert ids(again) == shipped
    assert (again.said, again.catalog.report.revision) == ([], None)


async def test_restarted_with_another_deployments_catalogue_the_first_answer_takes_it_back(
    tmp_path: Path,
) -> None:
    r = make_rig(tmp_path)
    shipped = ids(r)
    r.sign(5, [programme(profile_id="dev_only")])
    await r.step()
    again = make_rig(tmp_path, stored=r.on_disk())
    assert ids(again) == ["dev_only"]
    assert again.catalog.wire_fields()["catalogRevision"] == 5
    again.sign(0, [])
    await again.step()
    assert ids(again) == shipped
    assert again.catalog.wire_fields()["catalogRevision"] is None
    assert again.said == [LEFT]


async def test_a_return_to_the_fallback_waits_for_the_rest_and_for_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = make_rig(tmp_path)
    shipped = ids(r)
    r.sign(5, [programme(profile_id="dev_only")])
    await r.step()
    r.sign(0, [])

    def session_begins() -> None:
        r.rest[0] = False

    r.dashboard.during = session_begins
    await r.step(CATALOG_PERIOD)
    assert ids(r) == ["dev_only"]
    r.dashboard.during = lambda: None
    r.rest[0] = True

    def refuse(*_args: object, **_kwargs: object) -> Path:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "replace", refuse)
    await r.step(1)
    assert ids(r) == ["dev_only"]
    assert r.catalog.wire_fields()["catalogRevision"] == 5
    # Said when decided, then that the disk refused it.
    assert len(r.said) == 2
    assert r.said[0] == LEFT
    assert r.said[1].startswith("catalogue 0 non enregistre, programmes inchanges : ")
    monkeypatch.undo()
    await r.step(CATALOG_PERIOD)
    assert ids(r) == shipped
    assert r.said[2:] == [LEFT]


SHIPPED_IDS: Final[list[str]] = ["standard_30_min", "standard_45_min"]


async def test_without_readable_fallback_programmes_the_console_keeps_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = make_rig(tmp_path)
    r.sign(5, [programme(profile_id="dev_only")])
    await r.step()
    unusable = DefaultsUnusable(path="profiles.default.json", detail="absent")

    def unreadable(_store: ProfileStore) -> Result[StoreContent, DefaultsUnusable]:
        return Err(unusable)

    monkeypatch.setattr(ProfileStore, "read_defaults", unreadable)
    r.sign(0, [])
    await r.step(CATALOG_PERIOD)
    assert ids(r) == []
    assert r.catalog.wire_fields()["catalogRevision"] is None
    said = [
        "catalogue 5 retire : ce tableau de bord n'en publie aucun, aucun programme jusqu'au "
        "prochain demarrage, ceux de secours illisibles : absent"
    ]
    assert r.said == said
    # Nothing written: the disk keeps the catalogue. Said once.
    assert [p.profile_id for p in r.on_disk().profiles] == ["dev_only"]
    await r.step(CATALOG_PERIOD)
    assert r.said == said
    # Restarted under a repaired image: the catalogue is judged again, and left.
    monkeypatch.undo()
    again = make_rig(tmp_path, stored=r.on_disk())
    assert ids(again) == ["dev_only"]
    again.sign(0, [])
    await again.step()
    assert ids(again) == SHIPPED_IDS
    assert again.on_disk().catalog is None
    assert again.said == [LEFT]


async def test_an_answer_too_large_to_read_is_refused_once_in_the_logbook(tmp_path: Path) -> None:
    r = make_rig(tmp_path)
    shipped = ids(r)
    r.dashboard.reply = Err(Unreachable(TOO_LARGE_ANSWER, 200))
    await r.step()
    await r.step(CATALOG_PERIOD)
    assert ids(r) == shipped
    assert r.said == [
        f"catalogue refuse, programmes inchanges : reponse du catalogue au-dela de "
        f"{MAX_RESPONSE_BYTES} octets"
    ]


async def test_a_launch_that_timed_out_no_longer_holds_the_catalogue(tmp_path: Path) -> None:
    """Its id is cleared by the timeout: the catalogue is asked for again at rest."""
    r = cloud_rig(tmp_path)
    cloud = r.panel.cloud
    assert cloud is not None
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    r.dashboard.answer(START, ok())
    await r.step()
    r.confirm_offer()
    await r.step()
    held = r.sync.launch_in_hand
    r.panel.surface.take_command()  # the loop took it, then said nothing
    r.dashboard.answer(POLL_PATH, launch_answer(None))
    await r.step(float(LAUNCH_TIMEOUT) + 1.0)
    assert [held, r.sync.launch_in_hand] == [True, False]
    assert r.dashboard.to("/api/machine/training/end") != []


# =========================================================================
# A launch offered while the catalogue is being written (EX-5)
# =========================================================================


async def test_a_launch_offered_during_the_write_keeps_the_programmes_it_was_offered_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk takes the new catalogue; memory keeps the programmes the offer saw, until rest.

    The real interleaving: the write holds its worker thread while the console's own
    link step offers a dashboard launch, then lands.
    """
    r = cloud_rig(tmp_path)
    store = r.panel.services.store
    cloud = r.panel.cloud
    assert cloud is not None
    dashboard = Dashboard()
    key = Ed25519PrivateKey.generate()
    said: list[str] = []
    catalog = ProgrammeCatalog(
        clock=r.clock,
        transport=dashboard,
        store=store,
        rules=RULES,
        at_rest=r.panel.catalog_at_rest,
        say=said.append,
        public_key=public_pem(key),
        report=screen_stored(store, RULES, said.append),
    )
    before = store.list_profiles()
    rev = store.rev
    dashboard.reply = Ok(answer(key, text_of(7, [programme(profile_id="new_one")])))
    loop = asyncio.get_running_loop()
    in_write = asyncio.Event()
    release = threading.Event()
    write = ProfileStore._write_atomically  # pyright: ignore[reportPrivateUsage]  # held, then let through

    def held_write(self: ProfileStore, document: Mapping[str, JsonValue]) -> object:
        loop.call_soon_threadsafe(in_write.set)
        assert release.wait(5)
        return write(self, document)

    monkeypatch.setattr(ProfileStore, "_write_atomically", held_write)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))

    async def offer_meanwhile() -> None:
        await asyncio.wait_for(in_write.wait(), 5)
        await r.panel.cloud_step()  # the poll offers the dashboard's launch
        assert cloud.launch_in_hand
        release.set()

    await asyncio.gather(catalog.step(), offer_meanwhile())
    assert store.list_profiles() == before
    assert store.rev == rev
    on_disk = parse_store((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert isinstance(on_disk, Ok)
    assert [p.profile_id for p in on_disk.value.profiles] == ["new_one"]
    assert catalog.report.revision is None
    # A restart now would screen what the disk holds before offering it.
    reopened = ProfileStore(tmp_path / "p.json", SHIPPED_DEFAULTS_PATH)
    assert isinstance(reopened.load(), Ok)
    assert screen_stored(reopened, RULES, said.append).accepted == ("new_one",)
    # At the next rest, the whole list is asked for again and put in effect.
    monkeypatch.undo()
    r.dashboard.answer(POLL_PATH, launch_answer(None))
    r.clock.advance(Seconds(5.0))
    await r.panel.cloud_step()
    assert r.panel.catalog_at_rest()
    await catalog.step()
    assert dashboard.asked[-1] is None
    assert [p.profile_id for p in store.list_profiles()] == ["new_one"]
    assert catalog.report.revision == 7


async def test_a_session_armed_during_the_write_keeps_the_programmes_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same, the rest ended from inside the write itself."""
    r = make_rig(tmp_path)
    shipped = ids(r)
    r.sign(4, [programme(profile_id="new_one")])
    write = ProfileStore.replace_catalog

    def ending_rest(
        self: ProfileStore, profiles: Sequence[TrainingProfile], catalog: CatalogStamp
    ) -> object:
        r.rest[0] = False
        return write(self, profiles, catalog)

    monkeypatch.setattr(ProfileStore, "replace_catalog", ending_rest)
    await r.step()
    monkeypatch.undo()
    assert ids(r) == shipped
    assert [p.profile_id for p in r.on_disk().profiles] == ["new_one"]
    r.rest[0] = True
    await r.step(1)
    assert r.dashboard.asked == [None, None]
    assert ids(r) == ["new_one"]


def test_a_reason_and_an_id_are_bounded_in_code_points_as_the_dashboard_counts_them() -> None:
    """500 and 64 at most, an emoji counting one, as Convex counts them (ANH-138)."""
    emoji = "\U0001f6b4"
    _, rejected = review_all(
        [{"profile_id": emoji * 80, "medical_parameters_version": emoji * 600}], RULES
    )
    assert rejected[0].profile_id == emoji * MAX_ID_CHARS
    reason = rejected[0].reason
    assert len(reason) == MAX_REASON_CHARS
    # Twice as many UTF-16 units: what Convex no longer counts.
    assert len(reason.encode("utf-16-le")) // 2 > MAX_REASON_CHARS


# =========================================================================
# This console's rules, from its configuration
# =========================================================================


def test_the_rules_are_this_console_s_configuration() -> None:
    config = config_of(signed())
    rules = rules_of(config)
    assert rules.tiers == config.tiers
    assert rules.min_run == config.motion.min_run
    assert rules.medical_version == VERSION
    assert rules.medical_record == VERSION
    ceiling = config.ceiling_for(Occupancy.OCCUPIED)
    assert isinstance(ceiling, Ok)
    assert rules.ceiling == Ok(ceiling.value)


def test_without_a_person_on_board_the_rules_say_why() -> None:
    rules = rules_of(config_of(signed(), OCCUPANCY_OCCUPIED_ENABLED="false"))
    assert isinstance(rules.ceiling, Err)
    assert "OCCUPANCY_OCCUPIED_ENABLED=false" in rules.ceiling.error


def test_unsigned_parameters_give_no_signed_version() -> None:
    rules = rules_of(config_of(unsigned()))
    assert rules.medical_version is None
    assert rules.medical_record.startswith("unsigned-sha256:")


# =========================================================================
# The dashboard told, with the programmes (EX-4)
# =========================================================================


def linked(r: CloudRig, fields: list[Mapping[str, JsonValue]]) -> CloudSync:
    """A link of ``r``'s panel that says ``fields[0]`` of the catalogue with its programmes."""
    return CloudSync(
        clock=r.clock,
        transport=r.dashboard,
        runtime=r.runtime,
        surface=r.panel.surface,
        store=r.panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
        catalog_fields=lambda: fields[0],
    )


async def test_the_programmes_are_sent_with_the_catalogue_report_and_again_when_it_changes(
    tmp_path: Path,
) -> None:
    r = cloud_rig(tmp_path)
    fields: list[Mapping[str, JsonValue]] = [
        {"catalogRevision": None, "accepted": [], "rejected": []}
    ]
    sync = linked(r, fields)
    await sync.step()
    await sync.step()
    sent = r.dashboard.to("/api/machine/profiles")
    assert len(sent) == 1
    assert sent[0]["catalogRevision"] is None
    assert sent[0]["accepted"] == []
    assert sent[0]["storeRev"] == 0
    assert count(sent[0]["profiles"]) == 2
    fields[0] = {
        "catalogRevision": 4,
        "accepted": [],
        "rejected": [{"profileId": "trop_vite", "reason": "plafond"}],
    }
    await sync.step()
    sent = r.dashboard.to("/api/machine/profiles")
    assert len(sent) == 2
    assert sent[1]["catalogRevision"] == 4
    assert sent[1]["rejected"] == [{"profileId": "trop_vite", "reason": "plafond"}]


async def test_a_launch_offered_holds_the_catalogue(tmp_path: Path) -> None:
    r = cloud_rig(tmp_path)
    cloud = r.panel.cloud
    assert cloud is not None
    assert [cloud.launch_in_hand, r.panel.catalog_at_rest()] == [False, True]
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.panel.cloud_step()
    assert [cloud.launch_in_hand, r.panel.catalog_at_rest()] == [True, False]


async def test_a_start_waiting_in_the_mailbox_holds_the_catalogue(tmp_path: Path) -> None:
    r = cloud_rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    queued = r.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(queued, Ok)
    assert not r.panel.catalog_at_rest()


async def test_a_session_running_holds_the_catalogue_and_an_unlinked_console_has_none(
    tmp_path: Path,
) -> None:
    rig, _ = failure_rig(tmp_path)
    try:
        assert rig.panel.catalog is None
        assert rig.panel.catalog_at_rest()
        await rig.panel.catalog_step()
        await cruising(rig)
        assert not rig.panel.catalog_at_rest()
    finally:
        await rig.panel.close()


async def test_on_the_console_an_invalid_signature_is_a_warning_of_the_logbook(
    tmp_path: Path,
) -> None:
    r = cloud_rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    catalog = r.panel.catalog
    assert catalog is not None
    store = r.panel.services.store
    shipped = store.list_profiles()
    # The shipped key has no private half: whatever signs, the console refuses.
    r.dashboard.answer(
        CATALOG_PATH,
        Ok(answer(Ed25519PrivateKey.generate(), text_of(4, [programme(profile_id="x")]))),
    )
    await r.panel.catalog_step()
    assert store.list_profiles() == shipped
    r.journal.drain()
    logbook = (r.journal.root / "logbook" / "events.jsonl").read_text(encoding="utf-8")
    line: object = json.loads(logbook.splitlines()[-1])  # pyright: ignore[reportAny]  # narrowed below
    assert isinstance(line, dict)
    entry = cast("dict[str, object]", line)
    assert entry["kind"] == "warning"
    assert entry["detail"] == (
        "catalogue refuse, programmes inchanges : signature du catalogue invalide : "
        "la signature ne correspond pas au fichier"
    )
    assert catalog.wire_fields()["catalogRevision"] is None


async def test_without_a_record_the_warning_goes_to_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = cloud_rig(tmp_path)
    r.dashboard.answer(CATALOG_PATH, Ok(answer(Ed25519PrivateKey.generate(), "{}")))
    await r.panel.catalog_step()
    assert "catalogue refuse, programmes inchanges" in caplog.text


async def test_a_bug_in_the_catalogue_step_leaves_the_console_running(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = cloud_rig(tmp_path)

    async def broken(_self: ProgrammeCatalog) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(ProgrammeCatalog, "step", broken)
    await r.panel.catalog_step()
    assert "programme catalogue step failed; the programmes are unchanged" in caplog.text
