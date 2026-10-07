"""A name received from outside designates something under its directory, or nothing."""

from pathlib import Path

import pytest

from src.record.containment import resolve_under


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A records directory, with a neighbour whose name starts the same way."""
    records = tmp_path / "records"
    records.mkdir()
    (tmp_path / "records-old").mkdir()
    return records


@pytest.mark.parametrize("name", ["2026-10-07T101500Z_ab12", "out/2026-10-07T101500Z_ab12"])
def test_a_name_under_the_directory_is_its_resolved_path(root: Path, name: str) -> None:
    (root / name).mkdir(parents=True)
    assert resolve_under(root, name) == (root / name).resolve()


def test_a_name_that_does_not_exist_yet_is_still_located(root: Path) -> None:
    assert resolve_under(root, "absent") == root.resolve() / "absent"


def test_a_detour_that_comes_back_is_resolved_to_where_it_ends(root: Path) -> None:
    (root / "kept").mkdir()
    assert resolve_under(root, "elsewhere/../kept") == root.resolve() / "kept"


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "kept/../../outside",
        "../records-old",
        "../records-old/kept",
        "/etc/hostname",
    ],
)
def test_a_name_that_leaves_the_directory_designates_nothing(root: Path, name: str) -> None:
    assert resolve_under(root, name) is None


@pytest.mark.parametrize("name", ["", ".", "kept/.."])
def test_the_directory_itself_is_not_under_itself(root: Path, name: str) -> None:
    assert resolve_under(root, name) is None


def test_the_filesystem_root_is_not_under_itself() -> None:
    top = Path(Path.cwd().anchor)
    assert resolve_under(top, "") is None


def test_a_link_out_of_the_directory_designates_nothing(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert resolve_under(root, "link") is None
    assert resolve_under(root, "link/manifest.json") is None


def test_a_link_that_stays_in_the_directory_is_followed(root: Path) -> None:
    (root / "kept").mkdir()
    (root / "alias").symlink_to(root / "kept", target_is_directory=True)
    assert resolve_under(root, "alias") == root.resolve() / "kept"


def test_a_directory_given_through_a_link_is_the_one_the_link_names(
    root: Path, tmp_path: Path
) -> None:
    (root / "kept").mkdir()
    through = tmp_path / "through"
    through.symlink_to(root, target_is_directory=True)
    assert resolve_under(through, "kept") == root.resolve() / "kept"
    assert resolve_under(through, "../records-old") is None


def test_a_name_no_file_can_carry_designates_nothing(root: Path) -> None:
    assert resolve_under(root, "with\x00nul") is None
