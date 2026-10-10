"""The viewer page: what it asks for, what it shows, what it may load.

The page (``viewer/index.html``) reads its own address: ``?live=`` starts a
stream, ``?trace=`` plays a record or a JSONL export back; a file can also be
picked with its file button. These tests run the page's script as it is shipped
(``viewer_requests.mjs``, with Node) and read three things:

* the addresses it requests: only paths of the server that served it;
* what it puts in its document: whatever a trace, a record or a stream holds is
  shown as the text it is, in elements the page builds itself, and nothing is
  handed over to be read as markup;
* the policy the page declares for itself (a ``meta`` tag): its own style and
  script, each named by its hash, and requests to its own server.

The same page is served by ``simulation.live`` on the loopback address and by
the hosted application: both are tried for the requests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import fields
from html.parser import HTMLParser
from pathlib import Path
from typing import Final, TypedDict, override
from urllib.parse import quote, urlsplit

import pytest
from pydantic import TypeAdapter

from simulation.live import ROOT, VIEWER
from simulation.tracefile import Event, FinalState, JsonValue, Row
from src.record.schema import GeometrySnapshot

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


class Element(TypedDict):
    """An element of the page's document: its tag, its class, what it holds."""

    tag: str
    classes: str
    children: list[Node]


type Node = str | Element
"""A text, or an element."""


class Visit(TypedDict):
    address: str
    requests: list[Request]
    source: str
    shown: dict[str, Element]
    """Every element the script reached by its id, as the script left it."""

    markup: list[str]
    """Everything the script handed over to be read as markup."""


VISITS: Final[TypeAdapter[list[Visit]]] = TypeAdapter(list[Visit])


def visit(
    origin: str,
    query: str,
    *,
    served: Path | None = None,
    listed: Path | None = None,
    chosen: Path | None = None,
    streamed: Path | None = None,
    click: int | None = None,
) -> Visit:
    """Open the page at ``origin`` with ``query``; return what it requested and shows.

    ``served`` is what the server answers a trace or a record with, ``listed``
    what it answers the scenario list with (an empty list without it),
    ``chosen`` the file picked with the file button, ``streamed`` what the live
    stream plays; ``click`` then clicks that line of the event list.
    """
    node = shutil.which("node")
    # Not a skip: without Node nothing here would be checked.
    assert node is not None, "Node is needed to run the viewer page"
    options: list[str] = []
    for name, path in (
        ("served", served),
        ("listed", listed),
        ("chosen", chosen),
        ("streamed", streamed),
    ):
        if path is not None:
            options += [f"--{name}", str(path)]
    if click is not None:
        options += ["--click", str(click)]
    done = subprocess.run(  # noqa: S603  # our own harness, on the shipped page
        [node, str(HARNESS), str(PAGE), *options, f"{origin}{VIEWER}?{query}"],
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


# ------------------------------------------------------------------ what it asks for


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


# ------------------------------------------------------------------ what it shows
#
# A trace reaches the page four ways: a JSONL export or a record its server
# answers with, a file picked with the file button, a live stream. Whichever
# way, what the trace holds is text.

type Line = Mapping[str, JsonValue]
"""One line of a trace, as ``Trace.lines`` and ``view_record`` write them."""


def marked(field: str) -> str:
    """A value that names its field, and would be an element if read as markup."""
    return f'<b id="{field}">&amp;</b>'


def marked_fields(kind: type[Row | Event | FinalState | GeometrySnapshot]) -> dict[str, JsonValue]:
    """Every field of one kind of line, each holding its own marked value."""
    return {field.name: marked(field.name) for field in fields(kind)}


MARKED: Final[tuple[Line, ...]] = (
    {
        "type": "meta",
        **marked_fields(GeometrySnapshot),
        **{key: marked(key) for key in ("scenario", "kind")},
        **{key: marked(key) for key in ("zone_low_bpm", "zone_high_bpm")},
        **{key: marked(key) for key in ("hard_max_bpm", "critical_bpm")},
    },
    {"type": "row", **marked_fields(Row)},
    {"type": "event", **marked_fields(Event)},
    {"type": "final", **marked_fields(FinalState)},
    {"type": "warning", "file": marked("file"), "code": marked("code")},
)
"""A trace whose every field holds markup: one line of each kind the page reads."""

MARKED_WARNINGS: Final[str] = f": {marked('file')}: {marked('code')}"
"""What the page writes after the name of ``MARKED``: its one warning, as it was read."""

MARKED_EVENT: Final[Element] = {
    "tag": "div",
    "classes": "",
    "children": [
        # A time that is not a number is shown as no number at all.
        {"tag": "span", "classes": "t", "children": ["NaN s"]},
        {"tag": "b", "classes": "", "children": [marked("kind")]},
        f" {marked('detail')}",
    ],
}
"""The one event of ``MARKED``, as the list shows it: the page's three own nodes."""


def written(folder: Path, name: str, lines: Sequence[Line]) -> Path:
    """``lines`` as a file of ``folder``, one JSON document per line."""
    path = folder / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def through(way: str, trace: Path) -> Visit:
    """Give ``trace`` to the page one of the four ways it can receive one."""
    if way == "export":
        return visit(LOCAL, "trace=out/session.jsonl", served=trace)
    if way == "record":
        return visit(LOCAL, "trace=out/20261007T101500Z_ab12", served=trace)
    if way == "file":
        return visit(LOCAL, "", chosen=trace)
    assert way == "stream", way
    return visit(LOCAL, "live=manual_27_rpm", streamed=trace)


WAYS: Final[tuple[str, ...]] = ("export", "record", "file", "stream")
LOADED: Final[tuple[str, ...]] = ("export", "record", "file")
"""The ways that load a whole trace at once: the page then draws its first row."""


def text_of(seen: Visit, element: str) -> str:
    """What one element shows, checked to be a single text and nothing else."""
    (text,) = seen["shown"][element]["children"]
    assert isinstance(text, str), text
    return text


@pytest.mark.parametrize("way", WAYS)
def test_ex1_an_event_is_shown_as_the_text_it_holds(way: str, tmp_path: Path) -> None:
    seen = through(way, written(tmp_path, "session.jsonl", MARKED))
    assert seen["shown"]["events"]["children"] == [MARKED_EVENT]


@pytest.mark.parametrize("way", WAYS)
def test_ex1_a_trace_without_event_shows_the_page_s_own_line(way: str, tmp_path: Path) -> None:
    lines = [line for line in MARKED if line["type"] != "event"]
    seen = through(way, written(tmp_path, "session.jsonl", lines))
    empty: Element = {"tag": "div", "classes": "empty", "children": ["Aucun evenement."]}
    assert seen["shown"]["events"]["children"] == [empty]


@pytest.mark.parametrize("way", WAYS)
def test_ex1_nothing_a_trace_holds_is_handed_over_as_markup(way: str, tmp_path: Path) -> None:
    assert through(way, written(tmp_path, "session.jsonl", MARKED))["markup"] == []


@pytest.mark.parametrize("way", LOADED)
def test_ex1_the_labels_and_the_geometry_note_show_the_meta_as_text(
    way: str, tmp_path: Path
) -> None:
    seen = through(way, written(tmp_path, "session.jsonl", MARKED))
    # A radius that is not a number is shown as no number; the ratio as it is.
    assert text_of(seen, "kGref") == "g reference (NaN m)"
    assert text_of(seen, "kGleg") == "g pointe du pied (NaN m)"
    assert text_of(seen, "geomNote") == (
        "Axe au centre. Bras NaN m, capsule NaN m (cote oppose) a NaN m, "
        f"i = {marked('gear_ratio')}. "
    )


@pytest.mark.parametrize("way", LOADED)
def test_ex1_the_tiles_show_a_row_and_the_end_as_text(way: str, tmp_path: Path) -> None:
    seen = through(way, written(tmp_path, "session.jsonl", MARKED))
    tiles = {name: text_of(seen, name) for name in seen["shown"] if name.startswith("r")}
    assert tiles == {
        "rOut": "NaN",
        "rSet": "NaN",
        "rMotor": marked("measured_motor_rpm"),
        "rHz": "NaN",
        "rGref": "NaN",
        "rGleg": "NaN",
        "rHr": f"{marked('hr_true')} / {marked('hr_live')}",
        "rZone": (
            f"{marked('zone_low_bpm')}-{marked('zone_high_bpm')} / cible {marked('target_bpm')}"
        ),
        "rPhase": f"{marked('phase')} / {marked('mode')} / {marked('state')}",
        "rDrive": f"{marked('drive_state')} - {marked('sim_state')}",
        "rSafety": f"{marked('safety_rule')} : {marked('safety_action')}",
        "rFinal": (
            f"{marked('end_reason')} - variateur {marked('sim_state')}, "
            f"arbre {marked('shaft_motor_rpm')} tr/min (SOUS COUPLE)"
        ),
    }
    assert text_of(seen, "clock") == "t = NaN s"
    # The class of a tile is one the page chose, never a value of the trace.
    classes = {seen["shown"][name]["classes"] for name in tiles}
    assert classes == {"v", "v warn"}


def test_ex1_the_scenario_list_shows_each_served_name_as_the_text_of_an_option(
    tmp_path: Path,
) -> None:
    """ANH-183 EX-16: the list the server answers fills the menu, name by name, as text."""
    names = ["manual_27_rpm", marked("scenario")]
    listed = tmp_path / "scenarios.json"
    listed.write_text(json.dumps(names), encoding="utf-8")
    seen = visit(LOCAL, "", listed=listed)
    assert paths(seen, LOCAL) == [SCENARIOS]
    # One option per name, in the order served, each holding the name as its only text:
    # the one written like markup is not an element, and nothing was handed over as markup.
    menu = seen["shown"]["scenario"]
    assert menu["children"] == [
        {"tag": "option", "classes": "", "children": [name]} for name in names
    ]
    assert seen["markup"] == []
    # Without a list from the server the menu gains nothing.
    assert visit(LOCAL, "")["shown"]["scenario"]["children"] == []


def test_ex1_the_source_line_shows_a_served_name_and_the_warnings_as_text(
    tmp_path: Path,
) -> None:
    name = f"{marked('name')}.jsonl".replace("/", "")
    seen = visit(
        LOCAL, f"trace={quote(name, safe='')}", served=written(tmp_path, "session.jsonl", MARKED)
    )
    assert text_of(seen, "source") == name + MARKED_WARNINGS


def test_ex1_the_source_line_shows_the_name_of_a_chosen_file_as_text(tmp_path: Path) -> None:
    name = f"{marked('name')}.jsonl".replace("/", "")
    seen = visit(LOCAL, "", chosen=written(tmp_path, name, MARKED))
    assert text_of(seen, "source") == name + MARKED_WARNINGS


def test_ex1_the_source_line_shows_a_live_name_and_a_refusal_as_text(tmp_path: Path) -> None:
    name = marked("scenario")
    refusal: Line = {"type": "error", "detail": marked("detail")}
    live = f"live={quote(name, safe='')}&speed=20"
    assert text_of(visit(LOCAL, live), "source") == f"live: {name} a 20x (horloge manual)"
    refused = visit(LOCAL, live, streamed=written(tmp_path, "stream.jsonl", [refusal]))
    assert text_of(refused, "source") == f"erreur: {marked('detail')}"


MARKUP_READERS: Final[tuple[str, ...]] = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "setHTMLUnsafe",
    "srcdoc",
    "document.write",
    "createContextualFragment",
    "DOMParser",
)
"""What a script names to have a text read as markup."""


def test_ex1_the_script_names_nothing_that_reads_markup() -> None:
    # The harness sees the paths a test takes; this reads every line of the script.
    script = read_page().text["script"]
    assert [name for name in MARKUP_READERS if name in script] == []


# An ordinary trace: a programme whose heart-rate rule ends the session. What
# the page shows of it must not change with the way the page builds it.
ORDINARY: Final[tuple[Line, ...]] = (
    {
        "type": "meta",
        "schema": 1,
        "scenario": "hr_above_hard_max",
        "kind": "auto",
        "reference_radius_m": 1.5,
        "leg_tip_radius_m": 2.4254,
        "leg_tip_measured": False,
        "arm_tip_radius_m": 2.609,
        "capsule_near_radius_m": 0.412,
        "capsule_far_radius_m": 2.75,
        "counterweight_radius_m": 0.97,
        "gear_ratio": 51.3,
        "zone_low_bpm": 120,
        "zone_high_bpm": 140,
        "hard_max_bpm": 170,
        "critical_bpm": 185,
    },
    {
        "type": "row",
        "t": 0.1,
        "state": "running",
        "mode": "auto",
        "phase": "warmup",
        "drive_state": "RUN",
        "sim_state": "RUN",
        "measured_motor_rpm": 612,
        "measured_fresh": True,
        "output_rpm": 11.9298,
        "hertz": 22.1,
        "g_reference": 0.2387,
        "g_leg_tip": 0.386,
        "setpoint_output_rpm": 12.0,
        "hr_true": 118,
        "hr_live": 117,
        "target_bpm": 130,
        "safety_action": "none",
        "safety_rule": None,
    },
    {
        "type": "row",
        "t": 30.0,
        "state": "running",
        "mode": "auto",
        "phase": "hold",
        "drive_state": "RUN",
        "sim_state": "RUN",
        "measured_motor_rpm": 1385,
        "measured_fresh": False,
        "output_rpm": 26.9981,
        "hertz": 50.02,
        "g_reference": 1.2224,
        "g_leg_tip": 2.0117,
        "setpoint_output_rpm": 27.0,
        "hr_true": 131,
        "hr_live": 130,
        "target_bpm": 130,
        "safety_action": "none",
        "safety_rule": None,
    },
    {
        "type": "row",
        "t": 61.5,
        "state": "stopping",
        "mode": "auto",
        "phase": "cooldown",
        "drive_state": "FAULT_RESET",
        "sim_state": "DECEL",
        "measured_motor_rpm": 0,
        "measured_fresh": True,
        "output_rpm": 0.0,
        "hertz": 0.0,
        "g_reference": 0.0,
        "g_leg_tip": 0.0,
        "setpoint_output_rpm": 0.0,
        "hr_true": 172,
        "hr_live": None,
        "target_bpm": None,
        "safety_action": "stop",
        "safety_rule": "hr_above_hard_max",
    },
    {"type": "event", "t": 30.0, "kind": "phase", "detail": "warmup -> hold", "actor": "system"},
    {
        "type": "event",
        "t": 61.52,
        "kind": "verdict",
        "detail": "hr_above_hard_max: stop (172 > 170 bpm, R&D)",
        "actor": "system",
    },
    {
        "type": "final",
        "end_reason": "hr_above_hard_max",
        "sim_state": "READY",
        "shaft_motor_rpm": 0,
        "energised": False,
    },
)

ORDINARY_EVENTS: Final[list[Node]] = [
    {
        "tag": "div",
        "classes": "",
        "children": [
            {"tag": "span", "classes": "t", "children": ["30.0 s"]},
            {"tag": "b", "classes": "", "children": ["phase"]},
            " warmup -> hold",
        ],
    },
    {
        "tag": "div",
        "classes": "",
        "children": [
            {"tag": "span", "classes": "t", "children": ["61.5 s"]},
            {"tag": "b", "classes": "", "children": ["verdict"]},
            " hr_above_hard_max: stop (172 > 170 bpm, R&D)",
        ],
    },
]


def shown_text(seen: Visit) -> dict[str, tuple[str, str]]:
    """The text and the class of every element the page wrote one text in."""
    return {
        name: (text_of(seen, name), element["classes"])
        for name, element in seen["shown"].items()
        if len(element["children"]) == 1 and isinstance(element["children"][0], str)
    }


@pytest.mark.parametrize("way", WAYS)
def test_ex4_the_events_of_an_ordinary_trace_are_listed_as_they_were(
    way: str, tmp_path: Path
) -> None:
    # One line an event: its time, its kind in bold, a space and its detail.
    seen = through(way, written(tmp_path, "session.jsonl", ORDINARY))
    assert seen["shown"]["events"]["children"] == ORDINARY_EVENTS


@pytest.mark.parametrize("way", LOADED)
def test_ex4_an_ordinary_trace_shows_the_labels_tiles_and_clock_it_showed(
    way: str, tmp_path: Path
) -> None:
    seen = through(way, written(tmp_path, "session.jsonl", ORDINARY))
    shown = shown_text(seen)
    del shown["source"]  # the name of the trace: its own tests
    assert shown == {
        "kGref": ("g reference (1.50 m)", ""),
        "kGleg": ("g pointe du pied (2.43 m)", ""),
        "geomNote": (
            "Axe au centre. Bras 2.61 m, capsule 0.41 m (cote oppose) a 2.75 m, i = 51.3. "
            "Rayon de la pointe du pied NON mesure (borne CAO) : a mesurer.",
            "",
        ),
        "rOut": ("11.93", "v"),
        "rSet": ("12.00", "v"),
        "rMotor": ("612", "v"),
        "rHz": ("22.1", "v"),
        "rGref": ("0.239", "v"),
        "rGleg": ("0.386", "v"),
        "rHr": ("118 / 117", "v"),
        "rZone": ("120-140 / cible 130", "v"),
        "rPhase": ("warmup / auto / running", "v"),
        "rDrive": ("RUN - RUN", "v"),
        "rSafety": ("aucun verdict", "v ok"),
        "rFinal": ("hr_above_hard_max - variateur READY, arbre 0 tr/min", "v"),
        "clock": ("t = 0.1 s", ""),
    }


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # The change of phase, at 30 s: a stale measure, the heart in its zone.
        (
            0,
            {
                "clock": ("t = 30.0 s", ""),
                "rDrive": ("RUN (perimee) - RUN", "v"),
                "rGleg": ("2.012", "v warn"),
                "rZone": ("120-140 / cible 130", "v ok"),
                "rSafety": ("aucun verdict", "v ok"),
            },
        ),
        # The verdict, at 61.52 s: the row of 61.5 s is the one on screen.
        (
            1,
            {
                "clock": ("t = 61.5 s", ""),
                "rDrive": ("FAULT_RESET - DECEL", "v warn"),
                "rHr": ("172 / -", "v"),
                "rZone": ("120-140 / cible -", "v"),
                "rSafety": ("hr_above_hard_max : stop", "v warn"),
            },
        ),
    ],
)
def test_ex4_a_click_on_an_event_still_goes_to_its_time(
    line: int, expected: Mapping[str, tuple[str, str]], tmp_path: Path
) -> None:
    seen = visit(LOCAL, "", chosen=written(tmp_path, "session.jsonl", ORDINARY), click=line)
    shown = shown_text(seen)
    assert {name: shown[name] for name in expected} == expected
    assert shown["play"] == ("Lecture", "")


# ------------------------------------------------------------------ what it may load

POLICY: Final[str] = "content-security-policy"


class Page(HTMLParser):
    """The shipped page: its tags in the order they come, its style and its script."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: dict[str, str] = {"style": "", "script": ""}
        self._opened: str = ""

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._opened = tag

    @override
    def handle_endtag(self, tag: str) -> None:
        self._opened = ""

    @override
    def handle_data(self, data: str) -> None:
        # The text of a style or of a script comes right after its opening tag.
        if self._opened in self.text:
            self.text[self._opened] += data


def read_page() -> Page:
    page = Page()
    page.feed(PAGE.read_text(encoding="utf-8"))
    page.close()
    return page


def policy_of(page: Page) -> dict[str, list[str]]:
    """The page's policy, each directive with what it allows."""
    (content,) = (
        attrs["content"] or ""
        for tag, attrs in page.tags
        if tag == "meta" and (attrs.get("http-equiv") or "").lower() == POLICY
    )
    directives = [part.split() for part in content.split(";") if part.strip()]
    assert len({name for name, *_ in directives}) == len(directives), "a directive given twice"
    return {name: allowed for name, *allowed in directives}


def hash_of(text: str) -> str:
    """How a policy names an inline style or script: by the hash of its text."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


@pytest.mark.parametrize("inline", ["style", "script"])
def test_ex2_the_policy_names_the_page_s_own_style_and_script(inline: str) -> None:
    directive = f"{inline}-src"
    page = read_page()
    expected = hash_of(page.text[inline])
    # The page was changed and its policy was not: write `expected` in the policy.
    assert policy_of(page)[directive] == [expected]


def test_ex2_the_policy_allows_the_page_its_server_and_nothing_else() -> None:
    allowed = policy_of(read_page())
    del allowed["style-src"], allowed["script-src"]  # the two hashes: the test above
    assert allowed == {
        "default-src": ["'none'"],
        "connect-src": ["'self'"],
        # The page has no image: this is the site's icon, which a browser asks
        # the server of a page for by itself.
        "img-src": ["'self'"],
        "object-src": ["'none'"],
        "base-uri": ["'none'"],
        "form-action": ["'none'"],
    }


def test_ex2_the_policy_is_declared_before_anything_it_governs() -> None:
    names = [
        POLICY if (attrs.get("http-equiv") or "").lower() == POLICY else tag
        for tag, attrs in read_page().tags
    ]
    # Only the character set, which a browser must read first, comes before it.
    assert names[: names.index(POLICY) + 1] == ["html", "head", "meta", POLICY]


def test_ex2_the_page_holds_nothing_its_policy_refuses() -> None:
    page = read_page()
    # One style and one script, both written in the page; no other thing to load.
    loaded = {"script", "style", "link", "img", "iframe", "object", "embed", "base", "form"}
    assert sorted(tag for tag, _ in page.tags if tag in loaded) == ["script", "style"]
    assert [attrs for tag, attrs in page.tags if tag in {"script", "style"}] == [{}, {}]
    # A style or a handler written on an element is refused: the page has none.
    written_on = {name for _, attrs in page.tags for name in attrs}
    assert [name for name in sorted(written_on) if name == "style" or name.startswith("on")] == []
    # Nor does its script turn a text into code.
    assert [name for name in ("eval(", "Function(") if name in page.text["script"]] == []


@pytest.mark.parametrize("origin", ORIGINS)
def test_ex2_every_request_of_the_page_is_one_its_policy_allows(
    origin: str, tmp_path: Path
) -> None:
    # `connect-src 'self'`: the list, a trace, a record, a stream, all on its server.
    trace = written(tmp_path, "session.jsonl", ORDINARY)
    for query in ("trace=out/session.jsonl", "trace=out/20261007T101500Z_ab12", "live=x"):
        seen = visit(origin, query, served=trace, streamed=trace)
        assert len(paths(seen, origin)) == 2
        assert {request["kind"] for request in seen["requests"]} <= {"fetch", "stream"}
