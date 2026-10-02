"""Reading untrusted JSON into typed values, reporting every problem at once.

``json.loads`` returns ``Any``; this is the one module that turns it into
``object`` and then into typed values, so nothing else in the package touches
an untyped JSON node. Modelled on ``src.training.plan._Reader``: a scenario
author editing a file wants every mistake listed, not the first one.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import cast, final


def as_mapping(value: object) -> Mapping[str, object] | None:
    """``value`` as a string-keyed mapping, or ``None`` if it is not a JSON object."""
    if not isinstance(value, dict):
        return None
    return cast("Mapping[str, object]", value)


def as_sequence(value: object) -> Sequence[object] | None:
    """``value`` as a sequence, or ``None`` if it is not a JSON array."""
    if not isinstance(value, list):
        return None
    return cast("Sequence[object]", value)


def load_object(raw: str) -> Mapping[str, object] | str:
    """The top-level JSON object, or a sentence saying why there is none."""
    try:
        parsed: object = json.loads(raw)  # pyright: ignore[reportAny]  # narrowed below
    except json.JSONDecodeError as error:
        return f"not JSON: {error}"
    document = as_mapping(parsed)
    if document is None:
        return "the top level must be a JSON object"
    return document


@final
class Reader:
    """Typed reads from one JSON object, accumulating problems. Mutable, single-use."""

    __slots__ = ("_document", "_problems", "_where")

    def __init__(self, document: Mapping[str, object], where: str) -> None:
        self._document: Mapping[str, object] = document
        self._where: str = where
        self._problems: list[str] = []

    @property
    def problems(self) -> tuple[str, ...]:
        """Every problem noted so far, in reading order."""
        return tuple(self._problems)

    def note(self, problem: str) -> None:
        """Record a problem found by the caller."""
        self._problems.append(f"{self._where}: {problem}")

    def has(self, key: str) -> bool:
        """Whether ``key`` is present (even as ``null``)."""
        return key in self._document

    def raw(self, key: str) -> object:
        """The untyped node, ``None`` when absent. For the caller's own dispatch."""
        return self._document.get(key)

    def text(self, key: str, default: str | None = None) -> str:
        """A string; ``default`` when absent (a problem when there is no default)."""
        value = self._document.get(key)
        if value is None:
            if default is None:
                self.note(f"missing '{key}'")
                return ""
            return default
        if not isinstance(value, str):
            self.note(f"'{key}' must be a string")
            return default or ""
        return value

    def number(self, key: str, default: float | None = None) -> float:
        """A finite number; ``default`` when absent (a problem when there is no default)."""
        value = self._document.get(key)
        if value is None:
            if default is None:
                self.note(f"missing '{key}'")
                return 0.0
            return default
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.note(f"'{key}' must be a number")
            return 0.0 if default is None else default
        result = float(value)
        if not math.isfinite(result):
            self.note(f"'{key}' must be finite")
            return 0.0 if default is None else default
        return result

    def optional_number(self, key: str) -> float | None:
        """A finite number, or ``None`` when absent."""
        if self._document.get(key) is None:
            return None
        return self.number(key)

    def integer(self, key: str, default: int | None = None) -> int:
        """A whole number; ``default`` when absent (a problem when there is no default)."""
        value = self._document.get(key)
        if value is None:
            if default is None:
                self.note(f"missing '{key}'")
                return 0
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            self.note(f"'{key}' must be an integer")
            return 0 if default is None else default
        return value

    def flag(self, key: str, *, default: bool) -> bool:
        """A boolean; ``default`` when absent."""
        value = self._document.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            self.note(f"'{key}' must be true or false")
            return default
        return value

    def strings(self, key: str) -> tuple[str, ...]:
        """A list of strings; empty when absent."""
        value = self._document.get(key)
        if value is None:
            return ()
        items = as_sequence(value)
        if items is None or not all(isinstance(item, str) for item in items):
            self.note(f"'{key}' must be a list of strings")
            return ()
        return tuple(str(item) for item in items)

    def mapping(self, key: str) -> Mapping[str, object]:
        """A nested object; empty when absent."""
        value = self._document.get(key)
        if value is None:
            return {}
        nested = as_mapping(value)
        if nested is None:
            self.note(f"'{key}' must be an object")
            return {}
        return nested

    def objects(self, key: str) -> tuple[Mapping[str, object], ...]:
        """A list of objects; empty when absent."""
        value = self._document.get(key)
        if value is None:
            return ()
        items = as_sequence(value)
        if items is None:
            self.note(f"'{key}' must be a list of objects")
            return ()
        found: list[Mapping[str, object]] = []
        for index, item in enumerate(items):
            nested = as_mapping(item)
            if nested is None:
                self.note(f"'{key}'[{index}] must be an object")
                continue
            found.append(nested)
        return tuple(found)

    def unknown_keys(self, allowed: frozenset[str]) -> None:
        """Note every key not in ``allowed``: a typo must not be silently ignored."""
        for key in self._document:
            if key not in allowed:
                self.note(f"unknown key '{key}'")
