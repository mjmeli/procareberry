"""Small, source-independent event and destination records."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

KINDS = frozenset({"sleep", "bottle", "solids", "diaper"})


class SyncError(Exception):
    """An actionable error safe to display without upstream response bodies."""


class MappingError(SyncError):
    pass


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


@dataclass(frozen=True)
class Event:
    source_key: str
    kind: str
    child_uid: str
    start: datetime
    end: datetime | None = None
    values: dict = field(default_factory=dict)
    source_ids: tuple[str, ...] = ()

    @property
    def signature(self) -> str:
        return fingerprint(
            {
                "kind": self.kind,
                "child": self.child_uid,
                "start": self.start.isoformat(),
                "end": self.end.isoformat() if self.end else None,
                "values": self.values,
            }
        )


@dataclass(frozen=True)
class Record:
    path: str
    payload: dict
    version: str
    nested_key: str | None = None
    update_time: Any = field(default=None, compare=False, repr=False)

    @property
    def identity(self) -> str:
        return self.path + ("#" + self.nested_key if self.nested_key else "")


@dataclass(frozen=True)
class Prepared:
    event: Event
    payload: dict
    kwargs: dict


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)
    source_types: dict[str, int] = field(default_factory=dict)
    ignored_types: dict[str, int] = field(default_factory=dict)

    def add(self, result: str, reason: str = "") -> None:
        self.counts[result] = self.counts.get(result, 0) + 1
        if reason:
            # At most 50 categories of diagnostics; no source comments or child details.
            key = reason if reason in self.reasons or len(self.reasons) < 50 else "additional reasons"
            self.reasons[key] = self.reasons.get(key, 0) + 1
