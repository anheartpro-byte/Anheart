"""What the viewer page asks for: only paths of the server that served it.

The page (``viewer/index.html``) reads its own address: ``?live=`` starts a
stream, ``?trace=`` plays a record or a JSONL export back. These tests run the
page's script as it is shipped (``viewer_requests.mjs``, with Node) and read
the addresses it requests. The same page is served by ``simulation.live`` on
the loopback address and by the hosted application: both are tried.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Final, TypedDict
from urllib.parse import quote, urlsplit

import pytest
from pydantic import TypeAdapter

from simulation.live import ROOT, VIEWER

HARNESS: Final[Path] = Path(__file__).resolve().parent / "viewer_requests.mjs"
PAGE: Final[Path] = ROOT / "viewer" / "index.html"

LOCAL: Final[str] = "http://127.0.0.1:8765"
HOSTED: Final[str] = "https://simulation.example.test"
ORIGINS: Final[tuple[str, ...]] = (LOCAL, HOSTED)

SCENARIOS: Final[str] = "/api/scenarios"
"""Asked once by every page, to fill the list of live scenarios."""


class Request(TypedDict):
    kind: str
    asked: str
    resolved: str


class Visit(TypedDict):
    address: str
    requests: list[Request]
    source: str


VISITS: Final[TypeAdapter[list[Visit]]] = TypeAdapter(list[Visit])


def visit(origin: str, query: str) -> Visit:
    """Open the page at ``origin`` with ``query`` and return what it requested."""
    node = shutil.which("node")
    # Not a skip: without Node nothing here would be checked.
    assert node is not None, "Node is needed to run the viewer page"
    done = subprocess.run(  # noqa: S603  # our own harness, on the shipped page
        [node, str(HARNESS), str(PAGE), f"{origin}{VIEWER}?{query}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    (seen,) = VISITS.validate_json(done.stdout)
    return seen


def paths(seen: Visit, origin: str) -> list[str]:
    """Path and query of every request, each checked to stay on ``origin``."""
    found: list[str] = []
    for request in seen["requests"]:
        parts = urlsplit(request["resolved"])
        assert f"{parts.scheme}://{parts.netloc}" == origin, request
        found.append(parts.path + (f"?{parts.query}" if parts.query else ""))
    return found


@pytest.mark.parametrize("origin", ORIGINS)
def test_ex4_the_page_alone_asks_only_for_the_scenario_list(origin: str) -> None:
    assert paths(visit(origin, ""), origin) == [SCENARIOS]


@pytest.mark.parametrize("origin", ORIGINS)
@pytest.mark.parametrize(
    ("trace", "expected"),
    [
        # A JSONL export, relative to the server's root or from it.
        ("out/manual_27_rpm.jsonl", "/out/manual_27_rpm.jsonl"),
        ("/out/manual_27_rpm.jsonl", "/out/manual_27_rpm.jsonl"),
        # A name is a name, whatever it holds.
        ("out/a b?c#d.jsonl", "/out/a%20b%3Fc%23d.jsonl"),
        ("/\\elsewhere.example.test/x.jsonl", "/%5Celsewhere.example.test/x.jsonl"),
        ("\\\\elsewhere.example.test\\x.jsonl", "/%5C%5Celsewhere.example.test%5Cx.jsonl"),
        # A record, by the path the server resolves under its root.
        ("out/20261007T101500Z_ab12", "/api/record?path=out%2F20261007T101500Z_ab12"),
        ("out/20261007T101500Z_ab12.tar.gz", "/api/record?path=out%2F20261007T101500Z_ab12.tar.gz"),
        (
            "//elsewhere.example.test/record",
            "/api/record?path=%2F%2Felsewhere.example.test%2Frecord",
        ),
    ],
)
def test_ex4_a_trace_is_read_from_the_server_of_the_page(
    origin: str, trace: str, expected: str
) -> None:
    seen = visit(origin, f"trace={quote(trace, safe='')}")
    assert paths(seen, origin) == [SCENARIOS, expected]


@pytest.mark.parametrize("origin", ORIGINS)
@pytest.mark.parametrize(
    "trace",
    [
        "//elsewhere.example.test/x.jsonl",
        "https://elsewhere.example.test/x.jsonl",
        "out//x.jsonl",
        "../x.jsonl",
        "out/../../x.jsonl",
        "./out/x.jsonl",
    ],
)
def test_ex4_a_trace_that_is_not_a_file_under_the_root_is_not_requested(
    origin: str, trace: str
) -> None:
    seen = visit(origin, f"trace={quote(trace, safe='')}")
    assert paths(seen, origin) == [SCENARIOS]
    assert seen["source"] == "trace illisible: Error: hors du dossier servi"


@pytest.mark.parametrize("origin", ORIGINS)
def test_ex4_a_live_run_is_streamed_from_the_server_of_the_page(origin: str) -> None:
    name = "//elsewhere.example.test/x?y=1&speed=1"
    seen = visit(origin, f"live={quote(name, safe='')}&speed=20")
    stream = f"/stream?scenario={quote(name, safe='')}&speed=20&clock=manual"
    assert paths(seen, origin) == [SCENARIOS, stream]
    assert [request["kind"] for request in seen["requests"]] == ["fetch", "stream"]
