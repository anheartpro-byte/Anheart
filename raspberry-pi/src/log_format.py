"""The console's log lines: one JSON object per line, for the system journal.

On the Pi the console's output is the journal of its service
(``journalctl -u anheart``), kept on disk and bounded by the installation
(``docs/pi-image.md``). Each line is one JSON object with four fields:

* ``ts``: when, UTC, from the wall clock (so no better than that clock:
  :mod:`src.wall_clock`);
* ``level``: ``INFO``, ``WARNING``, ``ERROR`` or ``CRITICAL``;
* ``logger``: the module that speaks;
* ``event``: what happened, the sentence the module wrote.

A fifth, ``exception``, carries the traceback when there is one. A line break
inside a sentence or a traceback is escaped: one record is always one line.

What the lines must NOT carry is not decided here: no heart rate, no ECG
value, no rider identity is ever handed to a logger
(``tests/test_log_format.py`` holds every call in ``src/`` to that). This
module only gives the lines their shape.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import final, override


@final
class JsonLines(logging.Formatter):
    """Formats a record as one line of JSON whose ``event`` field is its message."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        line: dict[str, str] = {
            "ts": stamp.replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False)


def json_handler() -> logging.Handler:
    """The handler the console's entry point installs: standard error, one JSON line a record."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLines())
    return handler
