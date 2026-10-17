"""Environment settings; credentials are never included in representations."""

import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import KINDS, SyncError


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/data")
    timezone: str = "America/New_York"
    write_enabled: bool = False
    procare_email: str = field(default="", repr=False)
    procare_password: str = field(default="", repr=False)
    huckleberry_email: str = field(default="", repr=False)
    huckleberry_password: str = field(default="", repr=False)
    child_map: dict[str, str] = field(default_factory=dict)
    categories: frozenset[str] = KINDS
    interval_minutes: int = 15
    health_max_age_hours: int = 36

    @classmethod
    def from_env(cls):
        try:
            mapping = json.loads(os.getenv("CHILD_UID_MAP", "{}"))
            if not isinstance(mapping, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and k and v for k, v in mapping.items()
            ):
                raise ValueError
            if len(set(mapping.values())) != len(mapping):
                raise ValueError
            kinds = frozenset(
                x.strip()
                for x in os.getenv("SYNC_CATEGORIES", ",".join(sorted(KINDS))).split(",")
                if x.strip()
            )
            if not kinds or not kinds <= KINDS:
                raise ValueError
            timezone = os.getenv("TZ", "America/New_York")
            ZoneInfo(timezone)
            enabled = os.getenv("WRITE_ENABLED", "false").lower()
            if enabled not in {"true", "false"}:
                raise ValueError
            interval = int(os.getenv("INTERVAL_MINUTES", "15"))
            if not 5 <= interval <= 1440:
                raise ValueError
            age = int(os.getenv("HEALTH_MAX_AGE_HOURS", "36"))
            if age < 1:
                raise ValueError
            return cls(
                data_dir=Path(os.getenv("DATA_DIR", "/data")),
                timezone=timezone,
                write_enabled=enabled == "true",
                child_map=mapping,
                categories=kinds,
                interval_minutes=interval,
                health_max_age_hours=age,
                procare_email=os.getenv("PROCARE_EMAIL", ""),
                procare_password=os.getenv("PROCARE_PASSWORD", ""),
                huckleberry_email=os.getenv("HUCKLEBERRY_EMAIL", ""),
                huckleberry_password=os.getenv("HUCKLEBERRY_PASSWORD", ""),
            )
        except (ValueError, KeyError) as exc:
            raise SyncError(
                "Invalid configuration: check timezone, child map, categories, interval and booleans"
            ) from exc

    def require_credentials(self):
        if not all(
            (self.procare_email, self.procare_password, self.huckleberry_email, self.huckleberry_password)
        ):
            raise SyncError("Set PROCARE_EMAIL, PROCARE_PASSWORD, HUCKLEBERRY_EMAIL and HUCKLEBERRY_PASSWORD")


def check_window(start: date, end: date, today: date) -> None:
    if start > end or end > today or start < today - timedelta(days=89):
        raise SyncError("Dates must be ordered, not in the future, and inside the rolling 90-day window")
