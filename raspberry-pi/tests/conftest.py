"""Shared fixtures.

The ``clean_env`` fixture below is autouse and deliberately aggressive. Two
leaks made the previous suite order-dependent:

1. ``tests/test_config.py`` wrote ``os.environ`` directly and ``pop``ed
   ``CONVEX_URL``, so whichever test ran next saw whatever the previous one
   left behind.
2. ``src/config.py`` calls ``load_dotenv()`` at **import** time and caches a
   module-level singleton. A developer with a real ``raspberry-pi/.env`` on
   disk was therefore feeding their own machine credentials into the test run,
   and the first test to build a ``Config`` fixed it for every later test.

Clearing the variables and resetting the singleton around every test closes
both.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import src.config

#: Every environment variable the client reads. Listed explicitly rather than
#: pattern-matched, so adding a config knob without adding it here shows up as
#: a test that mysteriously depends on the developer's shell.
ANHEART_ENV_VARS: tuple[str, ...] = (
    # existing
    "CONVEX_URL",
    "MACHINE_API_KEY",
    "BITALINO_MAC",
    "SAMPLE_RATE",
    "OUTPUT_SAMPLE_RATE",
    "BATCH_INTERVAL_MS",
    "HEARTBEAT_INTERVAL_S",
    "LOG_LEVEL",
    "BUFFER_DB_PATH",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate each test from the ambient environment and the config singleton."""
    for name in ANHEART_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    src.config.reset_config()
    yield
    src.config.reset_config()
