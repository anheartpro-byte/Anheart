"""Every file the console keeps or ships is read as :func:`src.kept_file.read_kept` reads
one (ANH-149): never through a link, never waiting on a FIFO, never an oversize file read
whole. Each reader then takes its own path for an unreadable file, as it did before.

Each case starts from content the reader takes: a link to it, a FIFO in its place, or the
same content padded past the bound with blanks JSON allows. So what refuses is the file,
never what it holds.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

import pytest

from src import service_state
from src.clock import ManualClock
from src.contract import UNKNOWN_SOFTWARE_VERSION, VERSION_FILE_MAX_BYTES, read_software_version
from src.health_counters import COUNTERS, COUNTERS_FILE_MAX_BYTES, Counters, load_counters
from src.kept_file import open_kept
from src.medical import (
    MEDICAL_FILE_MAX_BYTES,
    SIGNATURE_FILE_MAX_BYTES,
    SignatureProblem,
    read_document,
    shipped_document,
    status_of,
)
from src.medical_unusable import MedicalParametersUnusableError
from src.programme_catalog import PUBLIC_KEY_MAX_BYTES, read_public_key
from src.record import cursor, reader, upload
from src.record.configuration import DIRECTORY, WARNING_PREFIX, persist
from src.record.containment import resolve_under
from src.record.deposit_files import MAX_FILE_BYTES, eligible_record, inventory, payload
from src.record.retention import (
    MARKER_MAX_BYTES,
    confirm_deposit,
    confirmed_deposit,
    marker_of,
    records,
)
from src.record.writer import CHECKSUMS_MAX_BYTES, MANIFEST_MAX_BYTES
from src.result import Err, Ok
from src.training.plan import (
    PROFILES_FILE_MAX_BYTES,
    SHIPPED_DEFAULTS_PATH,
    DefaultsUnusable,
    LoadSource,
    ProfileStore,
)
from tests.medical_support import write_signed
from tests.record_support import writer
from tests.test_record_deposit import ready_record
from tests.test_record_retention import CONFIRMED, closed_record

type Odd = Callable[[Path, bytes, int], None]


def linked(path: Path, content: bytes, _limit: int) -> None:
    """A link to a good file beside it: the link, not what it leads to, is refused."""
    real = path.with_name(f"real-{path.name}")
    real.write_bytes(content)
    path.symlink_to(real)


def fifo(path: Path, _content: bytes, _limit: int) -> None:
    """Never waited on: a reader that opened it blocking would hang here."""
    os.mkfifo(path)


def oversize(path: Path, content: bytes, limit: int) -> None:
    path.write_bytes(content + b" " * (limit + 1 - len(content)))


ODD: Final[Mapping[str, Odd]] = {"link": linked, "fifo": fifo, "oversize": oversize}


def placed(directory: Path, name: str, content: bytes, odd: str, limit: int) -> Path:
    """``name`` in a fresh ``directory``, made odd the way ``odd`` says."""
    directory.mkdir()
    path = directory / name
    ODD[odd](path, content, limit)
    return path


@pytest.mark.parametrize("odd", list(ODD))
def test_counters_kept_in_an_odd_file_are_unknown(tmp_path: Path, odd: str) -> None:
    good = COUNTERS.dump_json(Counters(drive_faults_by_code={}))
    regular = tmp_path / "counters.json"
    regular.write_bytes(good + b" " * (COUNTERS_FILE_MAX_BYTES - len(good)))
    assert load_counters(regular) == Counters(drive_faults_by_code={})
    path = placed(tmp_path / "odd", "counters.json", good, odd, COUNTERS_FILE_MAX_BYTES)
    assert load_counters(path) is None


@pytest.mark.parametrize("odd", list(ODD))
def test_a_service_state_in_an_odd_file_is_unreadable(tmp_path: Path, odd: str) -> None:
    regular = tmp_path / "state.json"
    assert service_state.store(regular, service_state.Stored(running=True)) == Ok(None)
    assert service_state.load(regular) == service_state.Stored(running=True)
    good = regular.read_bytes()
    path = placed(tmp_path / "odd", "state.json", good, odd, service_state.STATE_FILE_MAX_BYTES)
    assert isinstance(service_state.load(path), service_state.Unreadable)


@pytest.mark.parametrize("odd", list(ODD))
def test_a_cursor_in_an_odd_file_is_unreadable(tmp_path: Path, odd: str) -> None:
    record = tmp_path / "record"
    assert isinstance(cursor.store(record, cursor.Cursor(session_id="s")), Ok)
    assert cursor.load(record) == cursor.Cursor(session_id="s")
    good = cursor.cursor_of(record).read_bytes()
    (tmp_path / "odd").mkdir()
    other = tmp_path / "odd" / "record"
    ODD[odd](cursor.cursor_of(other), good, cursor.CURSOR_FILE_MAX_BYTES)
    assert isinstance(cursor.load(other), cursor.UnreadableCursor)


@pytest.mark.parametrize("odd", list(ODD))
def test_a_baseline_in_an_odd_file_is_none(tmp_path: Path, odd: str) -> None:
    baseline = cursor.Baseline(left_alone=("a",))
    assert isinstance(cursor.store_baseline(tmp_path, baseline), Ok)
    assert cursor.load_baseline(tmp_path) == baseline
    good = (tmp_path / cursor.BASELINE_NAME).read_bytes()
    path = placed(tmp_path / "odd", cursor.BASELINE_NAME, good, odd, cursor.BASELINE_MAX_BYTES)
    assert cursor.load_baseline(path.parent) is None


@pytest.mark.parametrize("odd", list(ODD))
def test_a_register_of_lost_stops_in_an_odd_file_is_one_loss(tmp_path: Path, odd: str) -> None:
    assert isinstance(cursor.store_lost_stops(tmp_path, ("perdue",), ()), Ok)
    assert cursor.load_lost_stops(tmp_path) == cursor.LostStops(lost=("perdue",))
    good = (tmp_path / cursor.LOST_STOPS_NAME).read_bytes()
    limit = cursor.CURSOR_FILE_MAX_BYTES
    path = placed(tmp_path / "odd", cursor.LOST_STOPS_NAME, good, odd, limit)
    unreadable = cursor.LostStops(lost=(cursor.LOST_STOPS_UNREADABLE,))
    assert cursor.load_lost_stops(path.parent) == unreadable


@pytest.mark.parametrize("odd", list(ODD))
def test_a_profile_store_in_an_odd_file_is_set_aside_for_the_defaults(
    tmp_path: Path, odd: str
) -> None:
    good = SHIPPED_DEFAULTS_PATH.read_bytes()
    regular = tmp_path / "profiles.json"
    regular.write_bytes(good)
    loaded = ProfileStore(regular).load()
    assert isinstance(loaded, Ok)
    assert loaded.value.source is LoadSource.STORE_FILE
    path = placed(tmp_path / "odd", "profiles.json", good, odd, PROFILES_FILE_MAX_BYTES)
    recovered = ProfileStore(path).load()
    assert isinstance(recovered, Ok)
    assert recovered.value.source is LoadSource.DEFAULTS_AFTER_CORRUPTION
    assert recovered.value.quarantined == path.with_name("profiles.json.corrupt-unreadable")
    assert not path.exists()
    assert not path.is_symlink()


@pytest.mark.parametrize("odd", list(ODD))
def test_shipped_defaults_in_an_odd_file_refuse_the_start(tmp_path: Path, odd: str) -> None:
    good = SHIPPED_DEFAULTS_PATH.read_bytes()
    defaults = placed(tmp_path / "odd", "defaults.json", good, odd, PROFILES_FILE_MAX_BYTES)
    loaded = ProfileStore(tmp_path / "profiles.json", defaults).load()
    assert isinstance(loaded, Err)
    assert isinstance(loaded.error, DefaultsUnusable)


@pytest.mark.parametrize("odd", list(ODD))
def test_medical_parameters_in_an_odd_file_stop_the_console(tmp_path: Path, odd: str) -> None:
    """Fail closed: no medical parameters, no console (``shipped_document`` raises)."""
    files = write_signed(tmp_path)
    assert isinstance(read_document(files.parameters), Ok)
    good = files.parameters.read_bytes()
    path = placed(tmp_path / "odd", files.parameters.name, good, odd, MEDICAL_FILE_MAX_BYTES)
    read = read_document(path)
    assert isinstance(read, Err)
    assert read.error[0].startswith("medical_parameters.json illisible")
    with pytest.raises(MedicalParametersUnusableError):
        shipped_document(path)


@pytest.mark.parametrize("odd", list(ODD))
def test_a_signature_or_a_key_in_an_odd_file_is_missing_and_the_console_bench_only(
    tmp_path: Path, odd: str
) -> None:
    """Fail closed: unsigned parameters take nobody on board (the bench mode)."""
    files = write_signed(tmp_path)
    read = read_document(files.parameters)
    assert isinstance(read, Ok)
    document = read.value
    signed = status_of(document, signature_path=files.signature, public_key_path=files.public_key)
    assert signed.signed
    limit = SIGNATURE_FILE_MAX_BYTES
    signature = placed(
        tmp_path / "signature", files.signature.name, files.signature.read_bytes(), odd, limit
    )
    unsigned = status_of(document, signature_path=signature, public_key_path=files.public_key)
    assert unsigned.unsigned is not None
    assert unsigned.unsigned.problem is SignatureProblem.SIGNATURE_MISSING
    key = placed(tmp_path / "key", files.public_key.name, files.public_key.read_bytes(), odd, limit)
    keyless = status_of(document, signature_path=files.signature, public_key_path=key)
    assert keyless.unsigned is not None
    assert keyless.unsigned.problem is SignatureProblem.PUBLIC_KEY_MISSING


@pytest.mark.parametrize("odd", list(ODD))
def test_a_catalogue_key_in_an_odd_file_is_none_and_nothing_is_signed(
    tmp_path: Path, odd: str
) -> None:
    """Fail closed: no key, and no catalogue, configuration or operator list verifies."""
    good = b"-----BEGIN PUBLIC KEY-----\nkey\n-----END PUBLIC KEY-----\n"
    regular = tmp_path / "anheart_catalog.pub"
    regular.write_bytes(good)
    assert read_public_key(regular) == good
    path = placed(tmp_path / "odd", regular.name, good, odd, PUBLIC_KEY_MAX_BYTES)
    assert read_public_key(path) is None


@pytest.mark.parametrize("odd", list(ODD))
def test_a_version_in_an_odd_file_is_unknown(tmp_path: Path, odd: str) -> None:
    good = b"pi-1.0.0\n"
    regular = tmp_path / "VERSION"
    regular.write_bytes(good)
    assert read_software_version(regular) == "pi-1.0.0"
    path = placed(tmp_path / "odd", "VERSION", good, odd, VERSION_FILE_MAX_BYTES)
    assert read_software_version(path) == UNKNOWN_SOFTWARE_VERSION


@pytest.mark.parametrize("odd", list(ODD))
def test_a_deposit_marker_in_an_odd_file_confirms_nothing(tmp_path: Path, odd: str) -> None:
    """No confirmation, so the record is never purged on it."""
    record = closed_record(tmp_path)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    assert confirmed_deposit(record) is not None
    marker = marker_of(record)
    good = marker.read_bytes()
    marker.unlink()
    ODD[odd](marker, good, MARKER_MAX_BYTES)
    assert confirmed_deposit(record) is None


# =========================================================================
# A record's own files, under data/records/: read by the worker threads that send,
# deposit and purge, which a FIFO would stall
# =========================================================================


def made_odd(path: Path, odd: str, limit: int) -> None:
    """``path``, a file of a record, made odd the way ``odd`` says, from what it held."""
    good = path.read_bytes()
    path.unlink()
    ODD[odd](path, good, limit)


def test_a_file_that_cannot_be_examined_is_closed_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ticks.csv"
    path.write_bytes(b"t\n0\n")
    with open_kept(path) as handle:
        assert handle.read() == b"t\n0\n"
    closed: list[int] = []
    real_close = os.close

    def refused(_descriptor: int) -> os.stat_result:
        raise OSError(errno.EIO, "fstat refused")

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    # Only around the call: nothing else of the test run is handed these.
    with monkeypatch.context() as patched:
        patched.setattr(os, "fstat", refused)
        patched.setattr(os, "close", close)
        with pytest.raises(OSError, match="fstat refused"):
            open_kept(path)
    assert len(closed) == 1


@pytest.mark.parametrize("odd", list(ODD))
def test_a_record_s_manifest_in_an_odd_file_is_neither_sent_nor_deposited(
    tmp_path: Path, odd: str
) -> None:
    """``read_head`` (the dashboard) and ``eligible_record`` (the deposit) refuse it: the
    uplink sends nothing of the record and leaves it to the next start, the deposit takes
    another record, and nothing is confirmed."""
    record = ready_record(tmp_path)
    assert isinstance(upload.read_head(record), Ok)
    eligible = eligible_record(record)
    assert isinstance(eligible, Ok)
    assert eligible.value is not None
    made_odd(record / "manifest.json", odd, MANIFEST_MAX_BYTES)
    head = upload.read_head(record)
    assert isinstance(head, Err)
    assert head.error.operation == "read"
    refused = eligible_record(record)
    assert isinstance(refused, Err)
    assert refused.error.operation == "read"


@pytest.mark.parametrize("odd", list(ODD))
def test_a_record_s_checksums_in_an_odd_file_confirm_purge_and_deposit_nothing(
    tmp_path: Path, odd: str
) -> None:
    """``retention``: no confirmation is written on them, and one written before no longer
    holds, so the record is never purged; ``deposit_files``: not deposited."""
    confirmed = ready_record(tmp_path / "confirmed")
    assert isinstance(confirm_deposit(confirmed, "kg2storage", CONFIRMED), Ok)
    assert confirmed_deposit(confirmed) is not None
    made_odd(confirmed / "checksums.sha256", odd, CHECKSUMS_MAX_BYTES)
    assert confirmed_deposit(confirmed) is None
    fresh = ready_record(tmp_path / "fresh")
    made_odd(fresh / "checksums.sha256", odd, CHECKSUMS_MAX_BYTES)
    refused = confirm_deposit(fresh, "kg2storage", CONFIRMED)
    assert isinstance(refused, Err)
    assert refused.error.operation == "close"
    assert not marker_of(fresh).exists()
    deposit = eligible_record(fresh)
    assert isinstance(deposit, Err)
    assert deposit.error.operation == "read"


def fifo_behind_a_link(path: Path, _content: bytes, _limit: int) -> None:
    """A link to a FIFO beside it, inside the record: what the link leads to is refused."""
    target = path.with_name(f"fifo-{path.name}")
    os.mkfifo(target)
    path.symlink_to(target)


DEPOSIT_ODD: Final[Mapping[str, Odd]] = {
    "fifo": fifo,
    "link-to-fifo": fifo_behind_a_link,
    "oversize": oversize,
}


@pytest.mark.parametrize("odd", list(DEPOSIT_ODD))
def test_a_file_to_deposit_in_an_odd_file_is_not_sent(tmp_path: Path, odd: str) -> None:
    """``payload`` reads the path :func:`~src.record.containment.resolve_under` gives back,
    links followed and kept inside the record (a link out of it is refused there, and one
    inside is judged by the checksum of what it leads to). What it opens is a regular file
    or an error read, never waited on; a file over what one deposit sends keeps its own
    refusal, before more than that is read. Nothing is sent, nothing confirmed."""
    record = ready_record(tmp_path)
    eligible = eligible_record(record)
    assert isinstance(eligible, Ok)
    selected = eligible.value
    assert selected is not None
    assert isinstance(payload(selected, "ticks.csv"), Ok)
    path = record / "ticks.csv"
    good = path.read_bytes()
    path.unlink()
    DEPOSIT_ODD[odd](path, good, MAX_FILE_BYTES)
    refused = payload(selected, "ticks.csv")
    assert isinstance(refused, Err)
    assert refused.error.operation == "read"
    if odd == "oversize":
        assert refused.error.detail == "file_exceeds_upload_limit"
    else:
        assert refused.error.detail == "OSError:EINVAL"


@pytest.mark.parametrize("odd", ["link", "fifo"])
def test_ticks_in_an_odd_file_are_an_error_never_waited_on(tmp_path: Path, odd: str) -> None:
    """``read_points`` and ``read_last_tick_ms`` stream the ticks a chunk at a time: they
    are opened as a kept file, and an error leaves the record unsent until the next start.
    No bound of their own: never more than a chunk is read."""
    record = closed_record(tmp_path)
    head = upload.read_head(record)
    assert isinstance(head, Ok)
    start = head.value.start_ms
    assert isinstance(upload.read_points(record, start_ms=start, offset=0, after_t=None), Ok)
    assert isinstance(upload.read_last_tick_ms(record), Ok)
    made_odd(record / "ticks.csv", odd, 0)
    points = upload.read_points(record, start_ms=start, offset=0, after_t=None)
    assert isinstance(points, Err)
    assert points.error.operation == "read"
    last = upload.read_last_tick_ms(record)
    assert isinstance(last, Err)
    assert last.error.operation == "read"


@pytest.mark.parametrize("odd", list(ODD))
@pytest.mark.parametrize("name", ["manifest.json", "checksums.sha256"])
def test_a_record_file_swapped_after_the_reader_s_check_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, odd: str, name: str
) -> None:
    """``reader.read`` judges each file by ``lstat`` before it reads one (refused as
    ``not_a_regular_file`` or ``file_above_its_bound``); each read is a kept read too, so a
    file swapped for a link, a FIFO or a larger one after that check is refused all the
    same, never waited on and never read whole."""
    record = closed_record(tmp_path)
    assert isinstance(reader.read(record), Ok)
    made_odd(record / name, odd, reader.FILE_BOUNDS[name])
    checked = reader.read(record)
    assert isinstance(checked, Err)

    def unchecked(_path: Path) -> str | None:
        return None

    monkeypatch.setattr(reader, "_outside_bounds", unchecked)
    swapped = reader.read(record)
    assert isinstance(swapped, Err)
    assert swapped.error.detail == "OSError"


@pytest.mark.parametrize("odd", list(ODD))
def test_a_raw_block_swapped_after_the_reader_s_check_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, odd: str
) -> None:
    """A raw ECG block is judged by ``lstat`` before the read too, then read as a kept
    file of at most :data:`~src.record.reader.LARGEST_BLOCK_FILE` bytes."""
    record = closed_record(tmp_path)
    block = record / "ecg_raw" / "000000.bin.gz"
    ODD[odd](block, b"not a block", reader.LARGEST_BLOCK_FILE)
    assert isinstance(reader.read(record), Err)

    def unchecked(_path: Path) -> str | None:
        return None

    monkeypatch.setattr(reader, "_outside_bounds", unchecked)
    swapped = reader.read(record)
    assert isinstance(swapped, Err)
    assert swapped.error.detail == "OSError"


def test_a_checksummed_file_that_is_a_link_refuses_the_record_and_a_fifo_is_not_opened(
    tmp_path: Path,
) -> None:
    """Every file ``checksums.sha256`` lists is hashed as it is read: a link in a record,
    which the writer never makes, is refused (it was hashed through, before); a FIFO is
    no file, said missing, and never opened, so never waited on."""
    record = closed_record(tmp_path)
    checksums = record / "checksums.sha256"
    listed = checksums.read_text(encoding="ascii")
    link = record / "extra.txt"
    link.symlink_to(record / "ticks.csv")
    checksums.write_text(listed + f"{'0' * 64}  extra.txt\n", encoding="ascii")
    refused = reader.read(record)
    assert isinstance(refused, Err)
    assert refused.error.detail == "OSError"
    link.unlink()
    os.mkfifo(link)
    read = reader.read(record)
    assert isinstance(read, Ok)
    assert ("extra.txt", "missing_file") in {
        (warning.file, warning.code) for warning in read.value.warnings
    }


def test_the_bounds_of_a_record_s_files_are_the_reader_s() -> None:
    assert reader.FILE_BOUNDS["manifest.json"] == MANIFEST_MAX_BYTES
    assert CHECKSUMS_MAX_BYTES == (reader.MAX_RAW_BLOCKS + 16) * 128


# =========================================================================
# On the journal thread: a configuration kept by its hash, and the close's checksums
# =========================================================================

CONFIGURATION: Final[bytes] = b'{"MOTOR_MAX_RPM": 1500}\n'


@pytest.mark.parametrize("odd", ["link", "fifo"])
def test_a_configuration_already_kept_in_an_odd_file_is_said_never_waited_on(
    tmp_path: Path, odd: str
) -> None:
    """``persist`` reads a document already kept under its hash as a kept file: a link or
    a FIFO in its place is a warning of the session, never a read through it, never a
    journal thread held."""
    assert persist(tmp_path / "regular", CONFIGURATION) is None
    assert persist(tmp_path / "regular", CONFIGURATION) is None, "the same document: kept"
    directory = tmp_path / "odd" / DIRECTORY
    directory.mkdir(parents=True)
    path = directory / (hashlib.sha256(CONFIGURATION).hexdigest() + ".json")
    ODD[odd](path, CONFIGURATION, 0)
    said = persist(tmp_path / "odd", CONFIGURATION)
    assert said == f"{WARNING_PREFIX} " + ("OSError:ELOOP" if odd == "link" else "OSError:EINVAL")


def test_a_link_planted_in_a_record_refuses_its_close_and_a_fifo_is_not_listed(
    tmp_path: Path,
) -> None:
    """The close hashes every file of the record as a kept file: a link, which the writer
    never makes, refuses the close (no checksums: the record is not taken for closed, nor
    deposited); a FIFO is no file, and is neither listed nor opened."""
    linked_writer = writer(tmp_path / "linked")
    (linked_writer.path / "planted").symlink_to(linked_writer.path / "ticks.csv")
    refused = linked_writer.close(ManualClock(), "operator_stop")
    assert isinstance(refused, Err)
    assert refused.error.operation == "close"
    assert not (linked_writer.path / "checksums.sha256").exists()
    fifo_writer = writer(tmp_path / "fifo")
    os.mkfifo(fifo_writer.path / "planted")
    closed = fifo_writer.close(ManualClock(), "operator_stop")
    assert isinstance(closed, Ok)
    listed = (fifo_writer.path / "checksums.sha256").read_text(encoding="ascii")
    assert "planted" not in listed


# =========================================================================
# A link on a directory above the file: O_NOFOLLOW guards only the last component
# =========================================================================


def linked_in_place(record: Path, elsewhere: Path) -> None:
    """``record`` moved aside and a link to ``elsewhere`` put in its place."""
    record.rename(record.with_name(record.name + ".moved"))
    record.symlink_to(elsewhere, target_is_directory=True)


def test_a_record_swapped_for_a_link_while_its_file_is_deposited_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deposit takes minutes: a record made a link between ``resolve_under`` and the
    open would hand over another directory's ``checksums.sha256``, which carries no digest
    of its own to check. Refused, and so is the record already a link."""
    record = ready_record(tmp_path / "records")
    eligible = eligible_record(record)
    assert isinstance(eligible, Ok)
    selected = eligible.value
    assert selected is not None
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "checksums.sha256").write_bytes(b"not this record's checksums\n")
    real = resolve_under

    def swapping(root: Path, name: str) -> Path | None:
        found = real(root, name)
        linked_in_place(root, elsewhere)
        return found

    monkeypatch.setattr("src.record.deposit_files.resolve_under", swapping)
    swapped = payload(selected, "checksums.sha256")
    assert isinstance(swapped, Err)
    assert swapped.error.detail == "OSError:ELOOP"
    monkeypatch.setattr("src.record.deposit_files.resolve_under", real)
    for name in ("checksums.sha256", "ticks.csv"):
        linked = payload(selected, name)
        assert isinstance(linked, Err)
        assert linked.error.detail == "OSError:ELOOP"


def test_a_record_swapped_for_a_link_after_it_is_listed_is_neither_read_nor_confirmed(
    tmp_path: Path,
) -> None:
    """Listed as a directory (``records`` leaves a link out), then a link to a copy of it:
    not deposited, no confirmation written or held, not read by the replay, nor its head by
    the dashboard. The copy is the same record: what refuses is the link."""
    root = tmp_path / "records"
    record = ready_record(root)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    assert records(root) == (record,)
    copy = tmp_path / "copy"
    shutil.copytree(record, copy)
    marker_of(record).rename(marker_of(record).with_name("kept-marker"))
    linked_in_place(record, copy)
    assert records(root) == ()
    refused = eligible_record(record)
    assert isinstance(refused, Err)
    assert refused.error.detail == "OSError:ELOOP"
    marker_of(record).with_name("kept-marker").rename(marker_of(record))
    assert confirmed_deposit(record) is None
    marker_of(record).unlink()
    confirmed = confirm_deposit(record, "kg2storage", CONFIRMED)
    assert isinstance(confirmed, Err)
    assert confirmed.error.operation == "close"
    assert not marker_of(record).exists()
    replayed = reader.read(record)
    assert isinstance(replayed, Err)
    assert replayed.error.detail == reader.LINKED_DIRECTORY
    head = upload.read_head(record)
    assert isinstance(head, Err)
    assert head.error.detail == "OSError:ELOOP"


def test_a_linked_ecg_raw_directory_is_refused_by_the_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another record's blocks, through a linked ``ecg_raw``: refused before a byte is read,
    and still refused when the link is put there after that check."""
    record = closed_record(tmp_path / "a")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "000000.bin.gz").write_bytes(b"another record's block")
    raw = record / "ecg_raw"
    shutil.rmtree(raw)
    raw.symlink_to(outside, target_is_directory=True)
    refused = reader.read(record)
    assert isinstance(refused, Err)
    assert refused.error.detail == reader.LINKED_DIRECTORY

    def unchecked(_path: Path) -> str | None:
        return None

    monkeypatch.setattr(reader, "_outside_bounds", unchecked)
    swapped = reader.read(record)
    assert isinstance(swapped, Err)
    assert swapped.error.detail == "OSError"


def test_a_checksummed_file_is_hashed_within_the_bound_of_its_name(tmp_path: Path) -> None:
    """A file the checksums list is hashed only when no larger than a file of its name can
    be: its own bound, a raw block's under ``ecg_raw/``, the largest for any other."""
    record = closed_record(tmp_path)
    checksums = record / "checksums.sha256"
    listed = checksums.read_text(encoding="ascii")
    (record / "extra.txt").write_bytes(b"listed, of a digest it does not have\n")
    large = record / "ecg_raw" / "extra.dat"
    large.write_bytes(b" " * (reader.LARGEST_BLOCK_FILE + 1))
    checksums.write_text(listed + f"{'0' * 64}  extra.txt\n", encoding="ascii")
    read = reader.read(record)
    assert isinstance(read, Ok)
    assert ("extra.txt", "checksum_mismatch") in {
        (warning.file, warning.code) for warning in read.value.warnings
    }
    checksums.write_text(listed + f"{'0' * 64}  ecg_raw/extra.dat\n", encoding="ascii")
    refused = reader.read(record)
    assert isinstance(refused, Err)
    assert refused.error.detail == reader.FILE_ABOVE_ITS_BOUND


def linked_ecg_raw(record: Path, outside: Path) -> None:
    """``record``'s ``ecg_raw`` made a link to ``outside``, which holds its blocks."""
    raw = record / "ecg_raw"
    shutil.copytree(raw, outside)
    shutil.rmtree(raw)
    raw.symlink_to(outside, target_is_directory=True)


def test_a_linked_ecg_raw_refuses_the_close_never_an_index_without_its_blocks(
    tmp_path: Path,
) -> None:
    """The index does not walk a linked directory: closed, the record would leave its ECG
    out of its manifest and its checksums. The close fails instead, as on any disk error."""
    made = writer(tmp_path / "record")
    linked_ecg_raw(made.path, tmp_path / "outside")
    refused = made.close(ManualClock(), "operator_stop")
    assert isinstance(refused, Err)
    assert refused.error.operation == "close"
    assert not (made.path / "checksums.sha256").exists()


def test_a_linked_ecg_raw_is_neither_selected_nor_inventoried_for_the_deposit(
    tmp_path: Path,
) -> None:
    """Deposited and confirmed without its ECG otherwise: ``eligible_record`` refuses the
    record, and so does ``inventory`` asked directly."""
    record = ready_record(tmp_path / "records")
    eligible = eligible_record(record)
    assert isinstance(eligible, Ok)
    selected = eligible.value
    assert selected is not None
    linked_ecg_raw(record, tmp_path / "outside")
    refused = eligible_record(record)
    assert isinstance(refused, Err)
    assert refused.error.detail == "OSError:ELOOP"
    with pytest.raises(OSError, match="a link in a directory's place"):
        inventory(record, selected.session_id, selected.files)
