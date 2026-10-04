"""The hosted wrapper's HTTP contract and Git-checkout import boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter

from app import app

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", timeout=30
    ) as http:
        yield http


@pytest.mark.parametrize("query", ["", "?live=manual_27_rpm&speed=20"])
async def test_root_preserves_the_viewer_query(client: AsyncClient, query: str) -> None:
    # Given a viewer query, when opening the root, then it redirects unchanged.
    response = await client.get("/" + query, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/viewer/index.html" + query


async def test_viewer_is_served_from_the_simulation_package(client: AsyncClient) -> None:
    # Given the shipped viewer, when requesting it, then the real asset is served.
    expected = (ROOT / "simulation/viewer/index.html").read_bytes()
    response = await client.get("/viewer/index.html")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.content == expected


async def test_catalogue_lists_shipped_scenarios_and_their_availability(
    client: AsyncClient,
) -> None:
    # Given the shipped battery, when listing it, then manual and DSP modes differ.
    response = await client.get("/api/catalogue")
    catalogue = TypeAdapter(dict[str, bool]).validate_json(response.content)
    assert response.status_code == 200
    assert catalogue["manual_32_rpm_refused"] is True
    assert any(not runnable for runnable in catalogue.values())


async def test_scenario_list_excludes_unavailable_modes(client: AsyncClient) -> None:
    # Given the hosted catalogue, when listing runnable names, then DSP is absent.
    listing = await client.get("/api/catalogue")
    catalogue = TypeAdapter(dict[str, bool]).validate_json(listing.content)
    response = await client.get("/api/scenarios")
    names = TypeAdapter(list[str]).validate_json(response.content)
    assert response.status_code == 200
    assert set(names) == {name for name, runnable in catalogue.items() if runnable}


@pytest.mark.parametrize("name", ["missing_scenario", "../../simulation/scenarios/manual_27_rpm"])
async def test_unshipped_names_end_with_an_error(client: AsyncClient, name: str) -> None:
    # Given an unshipped name or a path, when streaming, then no run is started.
    response = await client.get("/stream", params={"scenario": name})
    events = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert response.status_code == 200
    assert events == ["event: error", "event: end"]


async def test_dsp_scenarios_are_refused_by_the_hosted_stream(client: AsyncClient) -> None:
    # Given a shipped DSP scenario, when streaming, then it ends without running.
    listing = await client.get("/api/catalogue")
    catalogue = TypeAdapter(dict[str, bool]).validate_json(listing.content)
    name = next(name for name, runnable in catalogue.items() if not runnable)
    response = await client.get("/stream", params={"scenario": name})
    events = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert events == ["event: error", "event: end"]


async def test_a_real_scenario_streams_until_final_and_end(client: AsyncClient) -> None:
    # Given the real simulation runtime, when streaming, then it completes in SSE.
    response = await client.get(
        "/stream", params={"scenario": "manual_32_rpm_refused", "speed": "200"}
    )
    events = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert events[0] == "event: meta"
    assert events[-2:] == ["event: final", "event: end"]
    assert events.count("event: row") > 100


def test_git_entrypoint_imports_without_an_inherited_pythonpath() -> None:
    # Given a clean Git-style process, when loading the entrypoint, then it imports.
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    response = subprocess.run(
        [sys.executable, "-c", "from simulation_app import app"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert response.returncode == 0, response.stderr
