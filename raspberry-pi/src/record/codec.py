import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import TypeAdapter

from src.record.rows import JsonValue
from src.record.schema import Manifest

JSON: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
EMAIL: Final = re.compile(r"[^\s\"<>@]+@[^\s\"<>@]+\.[^\s\"<>@]+")
IDENTIFIER: Final = re.compile(r"[A-Za-z0-9_-]+")
SHA256: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class Privacy:
    """Names supplied at the boundary are removed before encoding any file or path."""

    names: tuple[str, ...] = ()

    def text(self, value: str) -> str:
        cleaned = EMAIL.sub("[redacted]", value)
        for name in self.names:
            if name:
                cleaned = re.sub(re.escape(name), "[redacted]", cleaned, flags=re.IGNORECASE)
        return cleaned


def encode(value: JsonValue, privacy: Privacy) -> str:
    return json.dumps(
        redact(value, privacy), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def redact(value: JsonValue, privacy: Privacy) -> JsonValue:
    if isinstance(value, str):
        return privacy.text(value)
    if isinstance(value, Mapping):
        return {key: redact(item, privacy) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(redact(item, privacy) for item in value)
    return value


def document[T](adapter: TypeAdapter[T], value: T) -> JsonValue:
    raw = adapter.dump_json(value, round_trip=True, warnings="error")
    return JSON.validate_json(raw)


def validate_manifest(value: Manifest, privacy: Privacy) -> None:
    identifiers = (
        value.record_id,
        value.machine_id,
        value.organization_id,
        value.local_ref,
        value.operator,
        value.session_id,
        value.subject_id,
    )
    for identifier in identifiers:
        if identifier is not None and (
            IDENTIFIER.fullmatch(identifier) is None or privacy.text(identifier) != identifier
        ):
            raise ValueError("record identifiers must be opaque path-safe IDs")
    if SHA256.fullmatch(value.config_hash) is None:
        raise ValueError("config_hash must be SHA-256")
    for stamp in (value.started_at, value.clocks.utc_start, value.ended_at):
        if stamp is not None:
            parsed = datetime.fromisoformat(stamp)
            if not stamp.endswith("Z") or parsed.utcoffset() is None:
                raise ValueError("timestamps must use UTC Z")


def mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError("expected a JSON object")
    return value
