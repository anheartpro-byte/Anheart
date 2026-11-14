"""ANH-141 EX-4, EX-6, EX-8: the console runs the configuration it applied, restarts at rest for it.

What the startup does with ``data/config.applied.json`` (verified again, the
``.env``'s keys for the dashboard left out, the locks held to the level signed
with it), the controlled restart that takes a revision just applied, at rest
only, and the hash the heartbeat and the records carry.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from src import local_panel
from src.clock import ManualClock
from src.config_sync import MACHINE_UNASSIGNED, AppliedConfig, ConfigSync, load_applied
from src.local_config import LocalConfig
from src.local_panel import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RESTART,
    RUNTIME_LIMITS,
    LocalPanel,
    build_panel,
    main,
    run_console,
)
from src.record.session import applied_configuration
from src.result import Ok
from src.service_state import ServiceGuard
from src.training.motion import default_motion_limits
from src.training.safety import SafetyLimits
from src.units import Bpm
from tests.test_config_sync import BASE, payload
from tests.test_local_panel import (
    BASE_ENV,
    FakeWeb,
    _fake_runner,  # pyright: ignore[reportPrivateUsage]  # the panel tests' own double
    _recording_env,  # pyright: ignore[reportPrivateUsage]
    config_from,
    sim_panel,
)

NAMED: Final[Mapping[str, str]] = {**BASE_ENV, "RECORD_MACHINE_ID": "machine-1"}
"""A console whose machine is named: the one whose configuration :func:`payload` signs."""


def write_applied(path: Path, text: str) -> bytes:
    """``text`` signed by a key made here, kept as the console keeps it; its public key."""
    key = Ed25519PrivateKey.generate()
    signature = base64.b64encode(key.sign(text.encode("utf-8"))).decode("ascii")
    path.write_text(json.dumps({"schema_version": 1, "payload": text, "signature": signature}))
    return key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


Seen = list[tuple[LocalConfig, AppliedConfig | None, Mapping[str, str] | None]]


def capture(monkeypatch: pytest.MonkeyPatch) -> Seen:
    seen: Seen = []

    async def fake(
        config: LocalConfig,
        *,
        stopped: Callable[[int], object],
        notify_socket: str | None,
        applied: AppliedConfig | None = None,
        local_env: Mapping[str, str] | None = None,
    ) -> int:
        del stopped, notify_socket
        seen.append((config, applied, local_env))
        return EXIT_OK

    monkeypatch.setattr(local_panel, "run_console", fake)
    return seen


def test_main_runs_the_revision_applied_and_leaves_out_the_env_s_keys_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.applied.json"
    public = write_applied(path, payload({**BASE, "MOTOR_MAX_RPM": 280}, revision=4))
    monkeypatch.setattr(local_panel, "APPLIED_CONFIG_PATH", path)
    monkeypatch.setattr(local_panel, "read_public_key", lambda: public)
    seen = capture(monkeypatch)
    assert main({**NAMED, "MOTOR_MAX_RPM": "999"}) == EXIT_OK
    config, applied, local_env = seen[0]
    assert int(config.motor_max_rpm) == 280
    assert config.machine_config is not None
    assert config.machine_config.revision == 4
    assert applied is not None
    assert applied.ref == config.machine_config
    assert local_env is not None
    assert local_env["MOTOR_MAX_RPM"] == "999"
    assert "configuration: 'MOTOR_MAX_RPM' du .env ignoree" in caplog.text


def test_main_holds_a_lock_its_signed_level_does_not_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.applied.json"
    public = write_applied(path, payload({**BASE, "PROGRAMS_ENABLED": True}, level="bench"))
    monkeypatch.setattr(local_panel, "APPLIED_CONFIG_PATH", path)
    monkeypatch.setattr(local_panel, "read_public_key", lambda: public)
    seen = capture(monkeypatch)
    assert main(NAMED) == EXIT_OK
    assert seen[0][0].programs_enabled is False
    assert "'PROGRAMS_ENABLED' au dessus du niveau de validation signe : mis a faux" in caplog.text


def test_main_refuses_to_start_on_a_file_that_does_not_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.applied.json"
    _ = write_applied(path, payload(BASE))
    monkeypatch.setattr(local_panel, "APPLIED_CONFIG_PATH", path)
    other = Ed25519PrivateKey.generate().public_key()
    monkeypatch.setattr(
        local_panel,
        "read_public_key",
        lambda: other.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo),
    )
    seen = capture(monkeypatch)
    assert main(NAMED) == EXIT_CONFIG
    assert seen == []
    assert "le retirer pour revenir aux cles du .env" in caplog.text


def test_r2_main_on_an_unnamed_machine_runs_its_env_whatever_file_is_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R2: no ``RECORD_MACHINE_ID``, a signed file of another machine with both locks at
    ``occupied``: never read, the .env runs, and the journal says why."""
    path = tmp_path / "config.applied.json"
    values = {**BASE, "PROGRAMS_ENABLED": True, "OCCUPANCY_OCCUPIED_ENABLED": True}
    public = write_applied(path, payload(values, level="occupied", machine="machine-OTHER"))
    monkeypatch.setattr(local_panel, "APPLIED_CONFIG_PATH", path)
    monkeypatch.setattr(local_panel, "read_public_key", lambda: public)
    seen = capture(monkeypatch)
    assert main(BASE_ENV) == EXIT_OK
    config, applied, _ = seen[0]
    assert (config.machine_config, applied) == (None, None)
    assert (config.programs_enabled, config.occupied_enabled) == (False, False)
    assert MACHINE_UNASSIGNED in caplog.text


def test_main_without_a_file_runs_the_env_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_panel, "APPLIED_CONFIG_PATH", tmp_path / "none.json")
    seen = capture(monkeypatch)
    assert main(BASE_ENV) == EXIT_OK
    assert seen[0][0].machine_config is None
    assert seen[0][1] is None


async def test_a_restart_asked_is_taken_at_rest_with_its_own_code(tmp_path: Path) -> None:
    panel, _ = sim_panel(tmp_path)
    panel.request_restart()
    await panel.config_step()  # not running yet: nothing to stop
    assert await panel.run(asyncio.Event(), FakeWeb()) == EXIT_RESTART


async def test_a_restart_waits_for_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel, _ = sim_panel(tmp_path)

    def moving(_self: LocalPanel) -> bool:
        return False

    monkeypatch.setattr(LocalPanel, "catalog_at_rest", moving)
    stop = asyncio.Event()
    panel.request_restart()
    asyncio.get_running_loop().call_later(0.5, stop.set)
    # Never at rest here: the stop event, not the restart, ends the run; the code still
    # says a restart, which the next rest would have taken.
    assert await panel.run(stop, FakeWeb()) == EXIT_RESTART


async def test_a_failing_configuration_step_stops_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(_self: ConfigSync) -> None:
        raise RuntimeError("injected")

    panel = build_panel(
        config_from({**NAMED, "MACHINE_API_KEY": "k", "CONVEX_URL": "http://localhost"}),
        clock=ManualClock(),
        profiles_path=tmp_path / "p.json",
    )
    monkeypatch.setattr(ConfigSync, "step", boom)
    await panel.config_step()
    assert "machine configuration step failed" in caplog.text
    await panel.close()


async def test_a_controlled_restart_is_written_as_a_clean_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def restarting(_self: LocalPanel, _stop: asyncio.Event, _web: object) -> int:
        return EXIT_RESTART

    written: list[int] = []
    monkeypatch.setattr(LocalPanel, "run", restarting)

    def stopped(_self: ServiceGuard) -> None:
        written.append(1)

    monkeypatch.setattr(ServiceGuard, "stopped", stopped)
    monkeypatch.setattr(local_panel, "UvicornRunner", _fake_runner)
    assert await run_console(config_from(_recording_env(tmp_path))) == EXIT_RESTART
    assert written == [1]


def test_the_record_carries_the_revision_it_runs(tmp_path: Path) -> None:
    """EX-6: the record's configuration hash covers the revision applied, and stays what
    it always was on a console that never applied one."""
    path = tmp_path / "config.applied.json"
    public = write_applied(path, payload(BASE, revision=5))
    loaded = load_applied(path, public, "machine-1")
    assert isinstance(loaded, Ok)
    assert isinstance(loaded.value, AppliedConfig)
    ref = loaded.value.ref
    plain = config_from(BASE_ENV)
    safety = SafetyLimits(hard_max_bpm=Bpm(148), critical_bpm=Bpm(158))
    limits = (RUNTIME_LIMITS, safety, default_motion_limits())
    linked = replace(plain, machine_config=ref)
    document = cast("dict[str, object]", json.loads(applied_configuration(linked, *limits)))
    assert document["machine_config"] == {"revision": 5, "config_hash": ref.digest}
    assert b'"machine_config"' not in applied_configuration(plain, *limits)
