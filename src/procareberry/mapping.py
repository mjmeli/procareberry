"""Mappings for supported Procare parent-feed payloads."""

import math
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import get_args
from zoneinfo import ZoneInfo

from huckleberry_api.firebase_types import BottleType

from .models import Event, MappingError, Report, fingerprint

IGNORED = frozenset(
    {
        "sign_in_activity",
        "sign_out_activity",
        "photo_activity",
        "video_activity",
        "note_activity",
        "incident_activity",
        "name_to_face_activity",
    }
)
BOTTLE_TYPES = {name.casefold(): name for name in get_args(BottleType)}

DIAPERS = {
    "wet": "pee",
    "pee": "pee",
    "bm": "poo",
    "dirty": "poo",
    "poo": "poo",
    "bm & wet": "both",
    "wet & bm": "both",
    "both": "both",
    "dry": "dry",
}


def timestamp(value, tz: ZoneInfo) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MappingError("missing timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MappingError("invalid timestamp") from exc
    if parsed.tzinfo is None:
        candidates = []
        for fold in (0, 1):
            candidate = parsed.replace(tzinfo=tz, fold=fold)
            if candidate.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == parsed:
                candidates.append(candidate)
        if not candidates or len({c.utcoffset() for c in candidates}) != 1:
            raise MappingError("ambiguous or nonexistent local timestamp; source offset required")
        parsed = candidates[0]
    return parsed.astimezone(tz)


def number(value, label, *, positive=False):
    if isinstance(value, bool):
        raise MappingError(f"invalid {label}")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise MappingError(f"missing or invalid {label}") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise MappingError(f"invalid {label}")
    return result


class Mapper:
    def __init__(self, timezone_name):
        self.tz = ZoneInfo(timezone_name)

    def _base(self, raw, context, source_child, child_uid, kind):
        identifier = raw.get("id")
        if identifier is None or isinstance(identifier, (dict, list)) or not str(identifier):
            raise MappingError("missing stable activity ID")
        if raw.get("kid_id") is not None and str(raw["kid_id"]) != source_child:
            raise MappingError("activity belongs to another child")
        kid_ids = raw.get("kid_ids")
        if kid_ids and source_child not in {str(x) for x in kid_ids}:
            raise MappingError("activity belongs to another child")
        start = timestamp(raw.get("activity_time"), self.tz)
        key = fingerprint([context, source_child, str(identifier)])
        return Event(key, kind, child_uid, start, source_ids=(str(identifier),))

    def _default(self, raw, context, source_child, child_uid):
        atype = raw.get("activity_type")
        data = raw.get("data") or {}
        if not isinstance(data, dict):
            raise MappingError("invalid activity data")
        comment = raw.get("comment") or ""
        if not isinstance(comment, str):
            raise MappingError("invalid comment")
        if atype == "nap_activity":
            event = self._base(raw, context, source_child, child_uid, "sleep")
            start = timestamp(data["start_time"], self.tz) if data.get("start_time") else None
            end = timestamp(data["end_time"], self.tz) if data.get("end_time") else None
            # Temporarily carry a missing start for deterministic, child-scoped pairing.
            return replace(
                event,
                start=start or event.start,
                end=end,
                values={"notes": comment, "_missing_start": start is None, "_open": end is None},
            )
        if atype == "bottle_activity":
            event = self._base(raw, context, source_child, child_uid, "bottle")
            description = data.get("desc")
            if not isinstance(description, str):
                raise MappingError("missing bottle description")
            bottle_type = BOTTLE_TYPES.get(description.strip().casefold())
            if not bottle_type:
                raise MappingError("unrecognized bottle description")
            # The supported parent feed reports bottle amounts in ounces.
            values = {"bottle_type": bottle_type, "units": "oz", "notes": comment}
            if data.get("amount") not in (None, ""):
                values["amount"] = number(data["amount"], "bottle amount")
            return replace(event, values=values)
        if atype == "bathroom_activity":
            event = self._base(raw, context, source_child, child_uid, "diaper")
            category = str(data.get("type", "")).strip().lower()
            if category not in {"diaper", "diaper change", "potty", "toilet"}:
                raise MappingError("unsupported bathroom type")
            subtype = str(data.get("sub_type", "")).strip().lower()
            if subtype not in DIAPERS:
                raise MappingError("unrecognized bathroom subtype")
            values = {"mode": DIAPERS[subtype], "notes": comment}
            if category in {"potty", "toilet"}:
                raise MappingError("potty records are not supported")
            return replace(event, values=values)
        if atype == "meal_activity":
            event = self._base(raw, context, source_child, child_uid, "solids")
            description = data.get("desc")
            if not isinstance(description, str) or not description.strip():
                raise MappingError("meal has no food description")
            # Do not split free text or turn 'most'/'some' into invented numeric portions.
            foods = [{"name": description.strip()}]
            return replace(
                event,
                values={
                    "foods": foods,
                    "notes": "; ".join(
                        str(x) for x in (data.get("type"), data.get("quantity"), comment) if x
                    ),
                },
            )
        raise MappingError("unsupported activity type")

    def map(self, records, context, source_child, child_uid, report: Report):
        events, open_naps, end_naps = [], [], []
        seen = set()
        for raw in records:
            atype = raw.get("activity_type", "unknown")
            label = (
                atype
                if isinstance(atype, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,80}", atype)
                else "invalid_type"
            )
            if label not in report.source_types and len(report.source_types) >= 50:
                label = "additional_types"
            report.source_types[label] = report.source_types.get(label, 0) + 1
            try:
                if atype in IGNORED:
                    report.add("ignored")
                    report.ignored_types[label] = report.ignored_types.get(label, 0) + 1
                    continue
                event = self._default(raw, context, source_child, child_uid)
                if event.source_key in seen:
                    report.add("skipped", "duplicate source activity ID")
                    continue
                seen.add(event.source_key)
                if event.kind == "sleep" and event.values.get("_missing_start"):
                    end_naps.append(event)
                elif event.kind == "sleep" and event.values.get("_open"):
                    open_naps.append(event)
                else:
                    events.append(event)
            except (MappingError, TypeError, ValueError) as exc:
                reason = str(exc) if isinstance(exc, MappingError) else "invalid source structure"
                report.add("skipped", reason)
        # Accept only one preceding open start within this child and school.
        for end_event in sorted(end_naps, key=lambda e: e.start):
            if end_event.end is None:
                report.add("skipped", "nap has no start or end")
                continue
            matches = [e for e in open_naps if e.start.timestamp() < end_event.end.timestamp()]
            if len(matches) != 1:
                report.add("skipped", "ambiguous or missing nap start")
                continue
            start_event = matches[0]
            open_naps.remove(start_event)
            events.append(
                replace(
                    start_event,
                    end=end_event.end,
                    values={"notes": start_event.values.get("notes") or end_event.values.get("notes", "")},
                    source_ids=start_event.source_ids + end_event.source_ids,
                )
            )
        for _ in open_naps:
            report.add("skipped", "nap is still in progress")
        result = []
        for event in events:
            if event.end and (
                event.end.timestamp() < event.start.timestamp()
                or event.end.timestamp() == event.start.timestamp()
                and event.kind == "sleep"
            ):
                report.add("skipped", "event end must follow start")
                continue
            result.append(
                replace(event, values={k: v for k, v in event.values.items() if not k.startswith("_")})
            )
        return sorted(result, key=lambda e: e.start)
