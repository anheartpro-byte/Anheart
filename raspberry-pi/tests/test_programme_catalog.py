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

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from src.clock import ManualClock
from src.cloud_sync import CloudSync, Document, Refused, Unreachable
from src.local_config import CardiacTiers
from src.medical import PUBLIC_KEY_PATH as MEDICAL_PUBLIC_KEY_PATH
from src.medical import SHIPPED_DOCUMENT, SHIPPED_PARAMETERS, MedicalVersion
from src.programme_catalog import (
    CATALOG_PATH,
    CATALOG_PERIOD,
    DUPLICATE,
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
    signed_text,
)
from src.result import Err, Ok, Result
from src.training.plan import (
    SHIPPED_DEFAULTS_PATH,
    CatalogStamp,
    JsonValue,
    ProfileStore,
    StoreContent,
    parse_store,
)
from src.training.types import Occupancy
from src.units import Monotonic, MotorRpm, Seconds, UnixMillis
from tests.medical_support import public_pem, signed, unsigned
from tests.test_cloud_sync import LAUNCH, OPERATOR, POLL_PATH, config_of, count, launch_answer
from tests.test_cloud_sync import Rig as CloudRig
from tests.test_cloud_sync import rig as cloud_rig
from tests.test_contract import shared
from tests.test_failure_process import cruising
from tests.test_failure_rig import make_rig as failure_rig
from tests.test_programme_validation_parity import CASES

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
