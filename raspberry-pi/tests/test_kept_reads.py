"""Every file the console keeps or ships is read as :func:`src.kept_file.read_kept` reads
one (ANH-149): never through a link, never waiting on a FIFO, never an oversize file read
whole. Each reader then takes its own path for an unreadable file, as it did before.

Each case starts from content the reader takes: a link to it, a FIFO in its place, or the
same content padded past the bound with blanks JSON allows. So what refuses is the file,
never what it holds.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

import pytest

from src import service_state
from src.health_counters import COUNTERS, COUNTERS_FILE_MAX_BYTES, Counters, load_counters
from src.medical import (
    MEDICAL_FILE_MAX_BYTES,
    SIGNATURE_FILE_MAX_BYTES,
    SignatureProblem,
    read_document,
    shipped_document,
    status_of,
)
from src.medical_unusable import MedicalParametersUnusableError
from src.record import cursor
from src.result import Err, Ok
from src.training.plan import (
    PROFILES_FILE_MAX_BYTES,
    SHIPPED_DEFAULTS_PATH,
    DefaultsUnusable,
    LoadSource,
    ProfileStore,
)
from tests.medical_support import write_signed

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
