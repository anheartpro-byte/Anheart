"""ANH-152: the signed operator list, the sign-in, the sessions and the lockout.

Every key here is made at run time and forgotten with the test: no private key
is written anywhere. A list read from a signed text carries the contract's scrypt
parameters, the only ones taken; a list held in memory carries cheap ones
(:mod:`tests.access_support`), so that a sign-in costs nothing.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Final, TypedDict

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import TypeAdapter

from src.clock import ManualClock
from src.console_access import (
    CODE_CHANGED,
    CONSOLE_LOCKOUT_FAILURES,
    DEVICE_LOCKOUT_FAILURES,
    HASH_BYTES,
    LIST_KIND,
    LOCAL_DEVICE,
    LOCKOUT_FAILURES,
    LOCKOUT_PERIOD,
    LOOPBACK_DEVICE,
    MAX_PAYLOAD_BYTES,
    NO_FAILURES,
    OPERATORS_PATH,
    OPERATORS_PERIOD,
    SALT_BYTES,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    SESSION_DROPPED,
    SESSION_IDLE,
    SESSION_LIFETIME,
    STORED_LIST,
    AccessNote,
    ConsoleAccess,
    Locked,
    NoList,
    ScryptCode,
    StoredList,
    StoredLockout,
    UnknownOperator,
    WrongCode,
    code_matches,
    describe_refusal,
    device_actor,
    load_failures,
    load_list,
    read_list,
    replace_file,
    signed_text,
)
from src.result import Err, Ok
from src.units import Seconds
from tests.access_support import (
    CODE,
    OPERATOR_ID,
    OPERATOR_NAME,
    held_list,
    listed,
    scrypt_code,
    tablet,
)

ROOT = Path(__file__).resolve().parents[2]


class _Code(TypedDict):
    n: int
    r: int
    p: int
    salt_bytes: int
    hash_bytes: int


class _Section(TypedDict):
    kind: str
    route: str
    poll_period_s: float
    max_payload_bytes: int
    device_actor_prefix: str
    code: _Code


class _Contract(TypedDict):
    operator_list: _Section


SECTION: Final[_Section] = TypeAdapter(_Contract).validate_json(
    (ROOT / "contracts" / "machine-api.json").read_bytes()
)["operator_list"]


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@functools.cache
def _hashed(code: str) -> bytes:
    """``code`` hashed with the contract's parameters: once per code, as they are slow."""
    salt = b"sel-de-test-1234"
    return hashlib.scrypt(
        code.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 2**20, dklen=32
    )


def code_document(code: str = CODE) -> dict[str, object]:
    return {
        "algorithm": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": b64(b"sel-de-test-1234"),
        "hash": b64(_hashed(code)),
    }


def list_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "kind": LIST_KIND,
        "machineId": "machine-1",
        "issuedAt": 1_790_000_000_000,
        "operators": [{"userId": OPERATOR_ID, "name": OPERATOR_NAME, "code": code_document()}],
        "devices": [
            {"deviceId": "tablette-1", "name": "Tablette", "tokenSha256": "ab" * 32},
        ],
    }
    document.update(overrides)
    return document


def text(document: Mapping[str, object]) -> str:
    return json.dumps(document)


class Signer:
    """An Ed25519 key pair made for one test."""

    def __init__(self) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )

    def answer(self, payload: str) -> dict[str, object]:
        return {
            "status": "signed",
            "payload": payload,
            "signature": b64(self.private.sign(payload.encode("utf-8"))),
            "server_contract_version": "1.13",
        }


# =========================================================================
# The contract and the list
# =========================================================================


def test_constants_are_the_contract_s() -> None:
    section = SECTION
    assert section["kind"] == LIST_KIND
    assert section["route"] == f"GET {OPERATORS_PATH}"
    assert section["poll_period_s"] == OPERATORS_PERIOD
    assert section["max_payload_bytes"] == MAX_PAYLOAD_BYTES
    assert section["device_actor_prefix"] == device_actor("")
    code = section["code"]
    assert (code["n"], code["r"], code["p"]) == (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    assert (code["salt_bytes"], code["hash_bytes"]) == (SALT_BYTES, HASH_BYTES)


def test_a_code_hashed_with_the_contract_s_parameters_is_checked() -> None:
    code = SECTION["code"]
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(
        b"739154", salt=salt, n=code["n"], r=code["r"], p=code["p"], maxmem=64 * 2**20, dklen=32
    )
    held = ScryptCode(n=code["n"], r=code["r"], p=code["p"], salt=salt, digest=digest)
    assert code_matches("739154", held)
    assert not code_matches("739155", held)


def test_reads_a_well_formed_list() -> None:
    read = read_list(text(list_document()), None)
    assert isinstance(read, Ok)
    held = read.value
    assert (held.machine_id, held.issued_at) == ("machine-1", 1_790_000_000_000)
    assert held.operators[0].user_id == OPERATOR_ID
    assert held.operator(OPERATOR_ID) is held.operators[0]
    assert held.operator("someone-else") is None
    assert held.devices[0].token_sha256 == bytes.fromhex("ab" * 32)


def test_a_list_of_another_machine_is_refused_when_the_console_knows_its_own() -> None:
    payload = text(list_document())
    assert isinstance(read_list(payload, "machine-1"), Ok)
    refused = read_list(payload, "machine-2")
    assert refused == Err("liste d'une autre machine ('machine-1', cette console est 'machine-2')")


@pytest.mark.parametrize(
    ("why", "payload"),
    [
        ("not JSON", "{"),
        ("not an object", "[]"),
        ("a catalogue", text({"catalogRevision": 3, "unchanged": False, "programmes": []})),
        ("no machine", text(list_document(machineId=None))),
        ("a machine id with a space", text(list_document(machineId="a b"))),
        ("a date that is a boolean", text(list_document(issuedAt=True))),
        ("a negative date", text(list_document(issuedAt=-1))),
        ("operators not a list", text(list_document(operators={}))),
        ("devices not a list", text(list_document(devices=None))),
        ("an operator not an object", text(list_document(operators=["x"]))),
        ("an operator without a name", text(list_document(operators=[{"userId": "a"}]))),
        (
            "an empty name",
            text(list_document(operators=[{"userId": "a", "name": "", "code": code_document()}])),
        ),
        (
            "a code not an object",
            text(list_document(operators=[{"userId": "a", "name": "A", "code": "x"}])),
        ),
        (
            "two entries of one operator",
            text(
                list_document(
                    operators=[
                        {"userId": "a", "name": "A", "code": code_document()},
                        {"userId": "a", "name": "B", "code": code_document()},
                    ]
                )
            ),
        ),
        ("a device not an object", text(list_document(devices=[1]))),
        (
            "a device named like the console's own",
            text(
                list_document(
                    devices=[{"deviceId": LOCAL_DEVICE, "name": "T", "tokenSha256": "ab" * 32}]
                )
            ),
        ),
        (
            "a device of the loopback",
            text(
                list_document(
                    devices=[{"deviceId": LOOPBACK_DEVICE, "name": "T", "tokenSha256": "ab" * 32}]
                )
            ),
        ),
        (
            "a digest that is not text",
            text(list_document(devices=[{"deviceId": "t", "name": "T", "tokenSha256": 1}])),
        ),
        (
            "a digest that is not hexadecimal",
            text(list_document(devices=[{"deviceId": "t", "name": "T", "tokenSha256": "z" * 64}])),
        ),
        (
            "two entries of one device",
            text(
                list_document(
                    devices=[
                        {"deviceId": "t", "name": "T", "tokenSha256": "ab" * 32},
                        {"deviceId": "t", "name": "U", "tokenSha256": "cd" * 32},
                    ]
                )
            ),
        ),
    ],
)
def test_refuses_a_list_that_is_not_one_whole(why: str, payload: str) -> None:
    assert isinstance(read_list(payload, None), Err), why


@pytest.mark.parametrize(
    ("why", "change"),
    [
        ("another algorithm", {"algorithm": "argon2id"}),
        ("n missing", {"n": None}),
        ("r missing", {"r": "8"}),
        ("p a boolean", {"p": True}),
        ("salt not base64", {"salt": "*"}),
        ("salt not ASCII", {"salt": "é"}),
        ("hash missing", {"hash": None}),
        ("another n", {"n": SCRYPT_N // 2}),
        ("another r", {"r": 1}),
        ("another p, hours of hashing", {"p": 1000}),
        ("r of 1 with a large n, refused by OpenSSL", {"n": 2**16, "r": 1}),
        ("an empty salt", {"salt": ""}),
        ("a salt of another length", {"salt": b64(b"x" * 8)}),
        ("a hash of another length", {"hash": b64(b"x" * 31)}),
    ],
)
def test_refuses_a_code_hashed_otherwise_than_by_the_contract(
    why: str, change: Mapping[str, object]
) -> None:
    code = {**code_document(), **change}
    operator = {"userId": OPERATOR_ID, "name": OPERATOR_NAME, "code": code}
    assert isinstance(read_list(text(list_document(operators=[operator])), None), Err), why


# =========================================================================
# The signature
# =========================================================================


def test_reads_nothing_unsigned_and_only_a_verified_text() -> None:
    signer = Signer()
    payload = text(list_document())
    assert signed_text({"status": "not_configured"}, signer.public) == Ok(None)
    assert signed_text(signer.answer(payload), signer.public) == Ok(payload)
    for document in (
        {"status": "unsigned", "payload": payload, "signature": "x"},
        {"status": "signed", "payload": 1, "signature": "x"},
        {"status": "signed", "payload": payload, "signature": None},
    ):
        assert signed_text(document, signer.public) == Err("reponse sans liste signee")
    refused = signed_text(signer.answer(payload), None)
    assert refused == Err("cle publique du catalogue absente ou illisible")
    for document in (
        {**signer.answer(payload), "signature": "%%"},
        {**signer.answer(payload), "payload": "\ud800"},
    ):
        assert signed_text(document, signer.public) == Err(
            "signature de la liste invalide : texte ou signature illisible"
        )
    forged = {**signer.answer(payload), "payload": payload.replace("machine-1", "machine-2")}
    assert isinstance(signed_text(forged, signer.public), Err)
    assert isinstance(signed_text(signer.answer(payload), Signer().public), Err)


def stored(path: Path, answer: Mapping[str, object]) -> None:
    envelope = StoredList(payload=str(answer["payload"]), signature=str(answer["signature"]))
    assert replace_file(path, STORED_LIST.dump_json(envelope)) == Ok(None)


def test_a_stored_list_is_verified_again_at_the_start(tmp_path: Path) -> None:
    signer = Signer()
    path = tmp_path / "data" / "operators.json"
    assert load_list(path, signer.public, None) == Ok(None)
    stored(path, signer.answer(text(list_document())))
    assert isinstance(load_list(path, signer.public, "machine-1"), Ok)
    assert isinstance(load_list(path, signer.public, "machine-2"), Err)
    loaded = load_list(path, signer.public, None)
    assert isinstance(loaded, Ok)
    assert loaded.value is not None
    assert loaded.value.operators[0].user_id == OPERATOR_ID
    # Another key: refused, nothing opens.
    assert isinstance(load_list(path, Signer().public, None), Err)
    # Verified but not a list.
    stored(path, signer.answer(text(list_document(kind="catalogue"))))
    assert isinstance(load_list(path, signer.public, None), Err)
    path.write_text("{}", encoding="utf-8")
    assert load_list(path, signer.public, None) == Err(
        "liste des operateurs enregistree mal formee"
    )
    path.unlink()
    path.mkdir()
    refused = load_list(path, signer.public, None)
    assert isinstance(refused, Err)
    assert refused.error.startswith("liste des operateurs illisible")


def test_lockout_counts_are_read_back_or_counted_again(tmp_path: Path) -> None:
    path = tmp_path / "operator_lockout.json"
    assert load_failures(path) == NO_FAILURES
    path.write_text(
        '{"schema_version": 1, "failures": {"a": 3}, "devices": {"t": 4}, "console": 7}',
        encoding="utf-8",
    )
    assert load_failures(path) == StoredLockout(failures={"a": 3}, devices={"t": 4}, console=7)
    path.write_text("not json", encoding="utf-8")
    assert load_failures(path) == NO_FAILURES


def test_a_file_the_disk_refuses_is_said_and_leaves_no_temporary(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("", encoding="utf-8")
    refused = replace_file(blocked / "inside.json", b"{}")
    assert isinstance(refused, Err)
    assert not list(tmp_path.glob(".*.tmp"))


# =========================================================================
# The sign-in and the lockout
# =========================================================================


def access_of(
    clock: ManualClock,
    *,
    held: bool = True,
    lockout: Path | None = None,
    failures: StoredLockout | None = None,
) -> tuple[ConsoleAccess, list[AccessNote]]:
    notes: list[AccessNote] = []
    access = ConsoleAccess(
        clock=clock,
        held=held_list(devices=(tablet("tablette-1", "jeton-de-la-tablette"),)) if held else None,
        lockout_path=lockout,
        failures=failures,
        note=notes.append,
    )
    return access, notes


async def test_nobody_signs_in_without_a_list() -> None:
    access, _ = access_of(ManualClock(), held=False)
    assert access.held is None
    assert await access.sign_in(OPERATOR_ID, CODE, LOCAL_DEVICE) == Err(NoList())
    assert access.device_of("jeton-de-la-tablette") is None


async def test_an_operator_of_the_list_signs_in_with_their_code() -> None:
    clock = ManualClock()
    access, notes = access_of(clock)
    assert await access.sign_in("someone-else", CODE, LOCAL_DEVICE) == Err(UnknownOperator())
    signed = await access.sign_in(OPERATOR_ID, CODE, LOCAL_DEVICE)
    assert isinstance(signed, Ok)
    cookie, session = signed.value
    assert (session.user_id, session.name, session.device_id) == (
        OPERATOR_ID,
        OPERATOR_NAME,
        LOCAL_DEVICE,
    )
    assert access.session(cookie, LOCAL_DEVICE, touch=False) == session
    assert notes == [AccessNote("console: connexion", OPERATOR_ID, False)]
    assert all(CODE not in note.detail for note in notes)


async def test_five_wrong_codes_lock_the_operator_out_and_the_lock_ends(tmp_path: Path) -> None:
    clock = ManualClock()
    path = tmp_path / "operator_lockout.json"
    access, notes = access_of(clock, lockout=path)
    for left in range(LOCKOUT_FAILURES - 1, 0, -1):
        assert await access.sign_in(OPERATOR_ID, "000000", "tablette-1") == Err(WrongCode(left))
    assert await access.sign_in(OPERATOR_ID, "000000", "tablette-1") == Err(
        Locked(retry_after=LOCKOUT_PERIOD)
    )
    assert load_failures(path) == StoredLockout(
        failures={OPERATOR_ID: LOCKOUT_FAILURES},
        devices={"tablette-1": LOCKOUT_FAILURES},
        console=LOCKOUT_FAILURES,
    )
    assert notes[-1].actor == device_actor("tablette-1")
    assert notes[-1].warning
    # Locked: even the right code is not checked.
    clock.advance(Seconds(60.0))
    refused = await access.sign_in(OPERATOR_ID, CODE, LOCAL_DEVICE)
    assert refused == Err(Locked(retry_after=Seconds(LOCKOUT_PERIOD - 60.0)))
    assert access.locked(OPERATOR_ID) == Seconds(LOCKOUT_PERIOD - 60.0)
    clock.advance(LOCKOUT_PERIOD)
    assert access.locked(OPERATOR_ID) is None
    # The period over: a wrong code counts from one again, the right one clears the
    # operator's count, on the disk too.
    assert await access.sign_in(OPERATOR_ID, "000000", LOCAL_DEVICE) == Err(
        WrongCode(LOCKOUT_FAILURES - 1)
    )
    assert isinstance(await access.sign_in(OPERATOR_ID, CODE, LOCAL_DEVICE), Ok)
    assert load_failures(path).failures == {}
    assert all(CODE not in note.detail and "000000" not in note.detail for note in notes)


async def test_a_lock_that_ended_is_cleared_on_the_disk_and_a_restart_finds_none(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    path = tmp_path / "operator_lockout.json"
    access, _ = access_of(clock, lockout=path)
    for _ in range(LOCKOUT_FAILURES):
        assert isinstance(await access.sign_in(OPERATOR_ID, "000000", LOCAL_DEVICE), Err)
    clock.advance(Seconds(LOCKOUT_PERIOD + 1.0))
    assert isinstance(await access.sign_in(OPERATOR_ID, CODE, LOCAL_DEVICE), Ok)
    assert load_failures(path) == NO_FAILURES
    restarted, _ = access_of(clock, lockout=path, failures=load_failures(path))
    assert isinstance(await restarted.sign_in(OPERATOR_ID, CODE, LOCAL_DEVICE), Ok)


async def test_a_lock_that_ended_is_cleared_on_the_disk_before_a_wrong_code(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    path = tmp_path / "operator_lockout.json"
    access, _ = access_of(clock, lockout=path, failures=load_failures(path))
    for _ in range(LOCKOUT_FAILURES):
        assert isinstance(await access.sign_in(OPERATOR_ID, "000000", LOCAL_DEVICE), Err)
    clock.advance(Seconds(LOCKOUT_PERIOD + 1.0))
    assert access.locked(OPERATOR_ID) is None
    assert await access.sign_in(OPERATOR_ID, "000000", LOCAL_DEVICE) == Err(
        WrongCode(LOCKOUT_FAILURES - 1)
    )
    assert load_failures(path) == StoredLockout(
        failures={OPERATOR_ID: 1}, devices={LOCAL_DEVICE: 1}, console=1
    )


async def test_one_device_trying_codes_across_operators_is_locked_out() -> None:
    clock = ManualClock()
    operators = tuple(listed(user_id=f"op-{i}", code=f"48291{i}") for i in range(4))
    notes: list[AccessNote] = []
    access = ConsoleAccess(
        clock=clock, held=held_list(operators=operators), lockout_path=None, note=notes.append
    )
    tried = 0
    for operator in operators[:2]:
        for _ in range(LOCKOUT_FAILURES):
            assert isinstance(await access.sign_in(operator.user_id, "000001", "tablette-1"), Err)
            tried += 1
    assert tried == DEVICE_LOCKOUT_FAILURES
    refused = await access.sign_in("op-2", "482912", "tablette-1")
    assert refused == Err(Locked(retry_after=LOCKOUT_PERIOD, scope="device"))
    assert notes[-1] == AccessNote(
        "console: connexions depuis device:tablette-1 verrouillees 15 min apres 10 codes faux",
        device_actor("tablette-1"),
        True,
    )
    # Another device is not this one's lock.
    assert isinstance(await access.sign_in("op-2", "482912", LOCAL_DEVICE), Ok)
    # A whole period: the device may try again.
    clock.advance(LOCKOUT_PERIOD)
    assert isinstance(await access.sign_in("op-3", "482913", "tablette-1"), Ok)


async def test_a_device_s_count_clears_after_a_period_without_a_wrong_code() -> None:
    clock = ManualClock()
    operators = tuple(listed(user_id=f"op-{i}", code=f"48291{i}") for i in range(3))
    access = ConsoleAccess(clock=clock, held=held_list(operators=operators), lockout_path=None)
    for operator in operators[:2]:
        for _ in range(LOCKOUT_FAILURES - 1):
            assert isinstance(await access.sign_in(operator.user_id, "000001", "tablette-1"), Err)
    clock.advance(LOCKOUT_PERIOD)
    remaining = LOCKOUT_FAILURES - 1
    assert await access.sign_in("op-2", "000001", "tablette-1") == Err(WrongCode(remaining))


async def test_the_whole_console_has_a_ceiling_of_wrong_codes(tmp_path: Path) -> None:
    clock = ManualClock()
    path = tmp_path / "operator_lockout.json"
    operators = tuple(listed(user_id=f"op-{i}", code=f"48291{i}") for i in range(5))
    devices = tuple(tablet(f"tablette-{i}", f"jeton-{i}") for i in range(4))
    access = ConsoleAccess(
        clock=clock,
        held=held_list(operators=operators, devices=devices),
        lockout_path=path,
    )
    tried = 0
    for i in range(4):
        for _ in range(LOCKOUT_FAILURES):
            refused = await access.sign_in(f"op-{i}", "000001", f"tablette-{i}")
            assert isinstance(refused, Err)
            tried += 1
    assert tried == CONSOLE_LOCKOUT_FAILURES
    refused = await access.sign_in("op-4", "482914", LOCAL_DEVICE)
    assert refused == Err(Locked(retry_after=LOCKOUT_PERIOD, scope="console"))
    # A restart does not lift it.
    restarted = ConsoleAccess(
        clock=clock,
        held=held_list(operators=operators, devices=devices),
        lockout_path=path,
        failures=load_failures(path),
    )
    clock.advance(Seconds(60.0))
    refused = await restarted.sign_in("op-4", "482914", LOCAL_DEVICE)
    assert refused == Err(Locked(retry_after=Seconds(LOCKOUT_PERIOD - 60.0), scope="console"))


async def test_a_restart_does_not_lift_a_lock() -> None:
    clock = ManualClock()
    found = StoredLockout(
        failures={OPERATOR_ID: LOCKOUT_FAILURES, "other": 2}, devices={}, console=0
    )
    access, _ = access_of(clock, failures=found)
    assert access.locked(OPERATOR_ID) == LOCKOUT_PERIOD
    assert access.locked("other") is None
    clock.advance(Seconds(LOCKOUT_PERIOD - 1.0))
    assert isinstance(await access.sign_in(OPERATOR_ID, CODE, LOCAL_DEVICE), Err)


async def test_counts_the_disk_refuses_are_kept_in_memory(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("", encoding="utf-8")
    access, _ = access_of(ManualClock(), lockout=blocked / "operator_lockout.json")
    assert await access.sign_in(OPERATOR_ID, "000000", LOCAL_DEVICE) == Err(
        WrongCode(LOCKOUT_FAILURES - 1)
    )
    assert await access.sign_in(OPERATOR_ID, "000000", LOCAL_DEVICE) == Err(
        WrongCode(LOCKOUT_FAILURES - 2)
    )


async def test_one_sign_in_at_a_time_and_nothing_else_waits_for_it() -> None:
    """A code being hashed holds the next sign-in, and never a device check nor a session."""
    release = threading.Event()
    started = threading.Event()

    def slow(code: str, held: ScryptCode) -> bool:
        started.set()
        release.wait(5.0)
        return code_matches(code, held)

    clock = ManualClock()
    access = ConsoleAccess(clock=clock, held=held_list(), lockout_path=None, verify=slow)
    first = asyncio.create_task(access.sign_in(OPERATOR_ID, CODE, LOCAL_DEVICE))
    assert await asyncio.to_thread(started.wait, 5.0)
    second = asyncio.create_task(access.sign_in(OPERATOR_ID, "000000", LOCAL_DEVICE))
    await asyncio.sleep(0.01)
    assert not second.done()
    # What the emergency stop depends on answers at once.
    assert access.device_of("unknown token") is None
    assert access.session("no-such-cookie", LOCAL_DEVICE, touch=False) is None
    release.set()
    assert isinstance(await first, Ok)
    assert await second == Err(WrongCode(LOCKOUT_FAILURES - 1))


# =========================================================================
# The sessions
# =========================================================================


async def signed_cookie(access: ConsoleAccess, device: str = LOCAL_DEVICE) -> str:
    signed = await access.sign_in(OPERATOR_ID, CODE, device)
    assert isinstance(signed, Ok)
    return signed.value[0]


async def test_a_session_is_its_device_s_and_ends_idle() -> None:
    clock = ManualClock()
    access, notes = access_of(clock)
    cookie = await signed_cookie(access, "tablette-1")
    assert access.session(None, "tablette-1", touch=True) is None
    assert access.session("other", "tablette-1", touch=True) is None
    assert access.session(cookie, LOCAL_DEVICE, touch=True) is None
    clock.advance(Seconds(SESSION_IDLE - 1.0))
    # Not touched: the idle period runs on.
    held = access.session(cookie, "tablette-1", touch=False)
    assert held is not None
    assert access.remaining(held) == (Seconds(1.0), Seconds(SESSION_LIFETIME - SESSION_IDLE + 1.0))
    touched = access.session(cookie, "tablette-1", touch=True)
    assert touched is not None
    assert touched.last_active == clock.monotonic()
    clock.advance(Seconds(SESSION_IDLE))
    assert access.session(cookie, "tablette-1", touch=True) is None
    assert notes[-1] == AccessNote("console: session expiree", OPERATOR_ID, False)
    assert access.session(cookie, "tablette-1", touch=True) is None


async def test_a_session_ends_after_its_lifetime_whatever_is_done() -> None:
    clock = ManualClock()
    access, _ = access_of(clock)
    cookie = await signed_cookie(access)
    step = Seconds(SESSION_IDLE / 2)
    while clock.monotonic() + step < SESSION_LIFETIME:
        clock.advance(step)
        assert access.session(cookie, LOCAL_DEVICE, touch=True) is not None
    clock.advance(step)
    assert access.session(cookie, LOCAL_DEVICE, touch=True) is None


async def test_sign_out_ends_the_session() -> None:
    access, notes = access_of(ManualClock())
    cookie = await signed_cookie(access)
    access.sign_out(None)
    access.sign_out("unknown")
    access.sign_out(cookie)
    assert access.session(cookie, LOCAL_DEVICE, touch=False) is None
    assert notes[-1] == AccessNote("console: deconnexion", OPERATOR_ID, False)


async def test_a_new_list_ends_the_sessions_it_no_longer_opens() -> None:
    clock = ManualClock()
    access, notes = access_of(clock)
    kept = await signed_cookie(access, LOCAL_DEVICE)
    on_tablet = await signed_cookie(access, "tablette-1")
    access.adopt(held_list(issued_at=1_790_000_000_001))
    assert access.session(kept, LOCAL_DEVICE, touch=False) is not None
    assert access.session(on_tablet, "tablette-1", touch=False) is None
    assert notes[-1] == AccessNote(SESSION_DROPPED, OPERATOR_ID, False)
    access.adopt(held_list(operators=(listed(user_id="someone-else"),)))
    assert access.session(kept, LOCAL_DEVICE, touch=False) is None


async def test_a_new_code_ends_the_sessions_opened_with_the_old_one() -> None:
    access, notes = access_of(ManualClock())
    cookie = await signed_cookie(access)
    renewed = (listed(code="739154"),)
    access.adopt(held_list(operators=renewed, issued_at=1_790_000_000_001))
    assert access.session(cookie, LOCAL_DEVICE, touch=False) is None
    assert notes[-1] == AccessNote(CODE_CHANGED, OPERATOR_ID, False)
    assert renewed[0].code == scrypt_code("739154")


def test_a_tablet_is_known_by_its_token_only() -> None:
    access, _ = access_of(ManualClock())
    found = access.device_of("jeton-de-la-tablette")
    assert found is not None
    assert found.device_id == "tablette-1"
    assert access.device_of("autre-jeton") is None
    assert access.device_of(None) is None


@pytest.mark.parametrize(
    ("refusal", "words"),
    [
        (NoList(), "aucune liste d'operateurs"),
        (UnknownOperator(), "operateur inconnu"),
        (Locked(retry_after=Seconds(30.0)), "operateur verrouille encore 1 min"),
        (Locked(retry_after=LOCKOUT_PERIOD), "encore 15 min"),
        (Locked(retry_after=LOCKOUT_PERIOD, scope="device"), "sur cet appareil encore 15 min"),
        (Locked(retry_after=LOCKOUT_PERIOD, scope="console"), "sur cette console encore"),
        (WrongCode(remaining=2), "2 essai(s)"),
    ],
)
def test_every_refusal_is_said(
    refusal: NoList | UnknownOperator | Locked | WrongCode, words: str
) -> None:
    assert words in describe_refusal(refusal)


def test_an_impossible_refusal_is_not_said() -> None:
    with pytest.raises(AssertionError):
        describe_refusal(object())  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
