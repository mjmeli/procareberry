"""Isolated compatibility wrapper for the pinned Huckleberry client."""

import contextvars
import time
import uuid
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from google.cloud.firestore_v1 import LastUpdateOption
from google.cloud.firestore_v1.base_query import FieldFilter
from huckleberry_api import HuckleberryAPI
from huckleberry_api import firebase_types as ft
from huckleberry_api.models import SolidsFoodReference

from .mapping import ACTIVITY_MODES, number
from .models import Event, MappingError, Prepared, Record, SyncError
from .storage import private_json, read_json

_historical_time = contextvars.ContextVar("procareberry_historical_time", default=None)
LOCATIONS = {
    "sleep": ("sleep", "intervals"),
    "bottle": ("feed", "intervals"),
    "nursing": ("feed", "intervals"),
    "solids": ("feed", "intervals"),
    "diaper": ("diaper", "intervals"),
    "potty": ("diaper", "intervals"),
    "temperature": ("health", "data"),
    "medicine": ("health", "data"),
    "growth": ("health", "data"),
    "pump": ("pump", "intervals"),
    "activity": ("activities", "intervals"),
}
MODELS = {
    "sleep": ft.FirebaseSleepIntervalData,
    "bottle": ft.FirebaseBottleFeedIntervalData,
    "nursing": ft.FirebaseBreastFeedIntervalData,
    "solids": ft.FirebaseSolidsFeedIntervalData,
    "diaper": ft.FirebaseDiaperData,
    "potty": ft.FirebaseDiaperData,
    "temperature": ft.FirebaseTemperatureData,
    "medicine": ft.FirebaseMedicationData,
    "growth": ft.FirebaseGrowthData,
    "pump": ft.FirebasePumpIntervalData,
    "activity": ft.FirebaseActivityIntervalData,
}
MODES = {
    "bottle": "bottle",
    "nursing": "breast",
    "solids": "solids",
    "medicine": "medication",
    "temperature": "temperature",
    "growth": "growth",
}
LAST_FIELDS = {
    "sleep": "lastSleep",
    "bottle": "lastBottle",
    "nursing": "lastNursing",
    "solids": "lastSolid",
    "diaper": "lastDiaper",
    "potty": "lastPotty",
    "temperature": "lastTemperature",
    "medicine": "lastMedication",
    "growth": "lastGrowthEntry",
    "pump": "lastPump",
}
LAST_ACTIVITIES = {
    "bath": "lastBath",
    "brushTeeth": "lastBrushTeeth",
    "indoorPlay": "lastIndoorPlay",
    "outdoorPlay": "lastOutdoorPlay",
    "screenTime": "lastScreenTime",
    "skinToSkin": "lastSkinToSkin",
    "storyTime": "lastStoryTime",
    "tummyTime": "lastTummyTime",
}


def offset(at: datetime) -> float:
    return -at.utcoffset().total_seconds() / 60


class HistoricalAPI(HuckleberryAPI):
    """Supply event-date offsets to logging without changing the installed library."""

    async def _get_timezone_offset_minutes(self):
        at = _historical_time.get()
        return offset(at) if at is not None else await super()._get_timezone_offset_minutes()


class Huckleberry:
    def __init__(self, api, timezone_name, options=None):
        self.api, self.tz, self.options = api, ZoneInfo(timezone_name), options or {}
        self.food_catalog, self.medicine_catalog = {}, {}

    async def authenticate(self, token_file):
        saved = read_json(token_file)
        if saved and saved.get("email") == self.api.email:
            for name in ("id_token", "refresh_token", "user_uid", "token_expires_at"):
                if name in saved:
                    setattr(self.api, name, saved[name])
        await self.api._ensure_authenticated()
        self.save_tokens(token_file)

    def save_tokens(self, token_file):
        # Save only the fields consumed by the pinned client; no account password.
        data = {"email": self.api.email}
        for name in ("id_token", "refresh_token", "user_uid", "token_expires_at"):
            value = getattr(self.api, name, None)
            if value is not None:
                data[name] = value
        private_json(token_file, data)

    async def children(self):
        user = await self.api.get_user()
        return [{"id": c.cid, "name": c.nickname or ""} for c in user.childList]

    async def _foods(self, child, foods):
        if not isinstance(foods, list) or not foods:
            raise MappingError("solids need at least one food with an exact catalog match")
        if child not in self.food_catalog:
            curated = await self.api.list_solids_curated_foods()
            custom = await self.api.list_solids_custom_foods(child)
            self.food_catalog[child] = [(f.id, f.name, "curated") for f in curated] + [
                (f.id, f.name, "custom") for f in custom
            ]
        refs = []
        for food in foods:
            food = {"name": food} if isinstance(food, str) else food
            if not isinstance(food, dict) or not isinstance(food.get("name"), str):
                raise MappingError("invalid food name")
            name = food["name"].strip()
            configured = self.options.get("foods", {}).get(name)
            matches = [f for f in self.food_catalog[child] if f[1].casefold() == name.casefold()]
            if configured:
                matches = [
                    f
                    for f in self.food_catalog[child]
                    if f[0] == configured.get("id") and f[2] == configured.get("source")
                ]
            if len(matches) != 1:
                raise MappingError("food needs one exact catalog match or an explicit foods mapping")
            ident, actual, source = matches[0]
            # Empty string preserves an unspecified portion; never invent a numeric serving.
            refs.append(
                SolidsFoodReference(id=ident, name=actual, source=source, amount=food.get("amount", ""))
            )
        if len({f.id for f in refs}) != len(refs):
            raise MappingError("duplicate food references in one meal")
        return refs

    async def _medicine(self, child, name):
        if not isinstance(name, str) or not name.strip():
            raise MappingError("medicine name required")
        if child not in self.medicine_catalog:
            self.medicine_catalog[child] = await self.api.list_medicine_types(child)
        configured = self.options.get("medicines", {}).get(name)
        matches = [
            m
            for m in self.medicine_catalog[child]
            if m.active
            and (m.id_ == configured if configured else m.name.casefold() == name.strip().casefold())
        ]
        if len(matches) != 1:
            raise MappingError("medicine needs one exact catalog match or an explicit medicines mapping")
        return matches[0]

    async def prepare(self, event: Event) -> Prepared:
        values = dict(event.values)
        notes = values.pop("notes", "")
        if not isinstance(notes, str):
            raise MappingError("notes must be text")
        start = event.start.timestamp()
        base = {"start": start, "offset": offset(event.start)}
        args = {"start_time": event.start}
        kind = event.kind
        if kind in {"activity", "pump"}:
            if event.end:
                duration = event.end.timestamp() - event.start.timestamp()
                if "duration" in values and number(values["duration"], "duration") != duration:
                    raise MappingError("duration does not match recorded interval")
                values["duration"] = duration
            elif "duration" in values:
                end_timestamp = start + number(values["duration"], "duration")
                event = replace(event, end=datetime.fromtimestamp(end_timestamp, self.tz))
        try:
            if kind == "sleep":
                if not event.end:
                    raise MappingError("nap is still in progress")
                if values:
                    raise MappingError("unsupported sleep field")
                base.update(
                    duration=event.end.timestamp() - start,
                    end_offset=offset(event.end),
                    details={"notes": notes},
                )
                args.update(end_time=event.end, details=ft.FirebaseSleepDetails(notes=notes))
            elif kind == "bottle":
                if set(values) - {"amount", "bottle_type", "units"}:
                    raise MappingError("unsupported bottle field")
                base.update(
                    mode="bottle",
                    bottleType=values["bottle_type"],
                    units=values["units"],
                    end_offset=offset(event.start),
                )
                if "amount" in values:
                    base["amount"] = number(values["amount"], "bottle amount")
                if notes:
                    base["notes"] = notes
                args.update(bottle_type=base["bottleType"], units=base["units"], amount=base.get("amount"))
            elif kind == "solids":
                if set(values) - {"foods", "reaction"}:
                    raise MappingError("unsupported solids field")
                refs = await self._foods(event.child_uid, values["foods"])
                base.update(
                    mode="solids",
                    foods={
                        f.id: {"id": f.id, "created_name": f.name, "source": f.source, "amount": f.amount}
                        for f in refs
                    },
                    notes=notes,
                )
                args.update(foods=refs, notes=notes)
                if "reaction" in values:
                    base["reactions"] = {values["reaction"]: True}
                    args["reaction"] = values["reaction"]
            elif kind in {"diaper", "potty"}:
                allowed = {
                    "mode",
                    "pee_amount",
                    "poo_amount",
                    "color",
                    "consistency",
                    "diaper_rash",
                    "how_it_happened",
                }
                if set(values) - allowed or kind == "diaper" and "how_it_happened" in values:
                    raise MappingError("unsupported bathroom field")
                base.update(mode=values["mode"])
                if notes:
                    base["notes"] = notes
                for name in ("color", "consistency"):
                    if name in values:
                        base[name] = values[name]
                amount_values = {"little": 0.0, "medium": 50.0, "big": 100.0}
                quantity = {
                    side: amount_values[values[side + "_amount"]]
                    for side in ("pee", "poo")
                    if side + "_amount" in values
                }
                if quantity:
                    base["quantity"] = quantity
                if kind == "potty":
                    base.update(isPotty=True, howItHappened=values["how_it_happened"])
                    if "diaper_rash" in values:
                        raise MappingError("potty does not support diaper rash")
                else:
                    if "diaper_rash" in values and not isinstance(values["diaper_rash"], bool):
                        raise MappingError("diaper_rash must be boolean")
                    if values.get("diaper_rash"):
                        base["diaperRash"] = True
                args.update(values, notes=notes)
            elif kind == "temperature":
                if set(values) - {"amount", "units"}:
                    raise MappingError("unsupported temperature field")
                base.update(
                    mode="temperature",
                    amount=number(values["amount"], "temperature"),
                    units=values["units"],
                    notes=notes,
                    lastUpdated=0,
                )
                args.update(amount=base["amount"], units=base["units"], notes=notes)
            elif kind == "medicine":
                if set(values) - {"medicine", "amount", "units"}:
                    raise MappingError("unsupported medicine field")
                medicine = await self._medicine(event.child_uid, values["medicine"])
                if "amount" in values and "units" not in values:
                    raise MappingError("medicine amount requires units")
                base.update(
                    mode="medication",
                    medication_id=medicine.id_,
                    medication_name=medicine.name,
                    amount=number(values["amount"], "medicine amount") if "amount" in values else 0,
                    notes=notes,
                )
                if "units" in values:
                    base["units"] = values["units"]
                args.update(
                    medicine_type=medicine,
                    notes=notes,
                    **{k: values[k] for k in ("amount", "units") if k in values},
                )
            elif kind == "growth":
                if set(values) - {"weight", "height", "head", "units"}:
                    raise MappingError("unsupported growth field")
                units = values["units"]
                if units not in {"metric", "imperial"}:
                    raise MappingError("growth units must be metric or imperial")
                base["mode"] = "growth"
                unit_map = (
                    {"weight": "kg", "height": "cm", "head": "hcm"}
                    if units == "metric"
                    else {"weight": "lbs", "height": "in", "head": "hin"}
                )
                for name, unit in unit_map.items():
                    if name in values:
                        base[name] = number(values[name], name, positive=True)
                        base[name + "Units"] = unit
                if len(base) == 3:
                    raise MappingError("growth requires a measurement")
                if notes:
                    raise MappingError("Huckleberry growth entries do not support notes")
                args.update(values)
            elif kind == "nursing":
                if set(values) - {"side", "left_duration", "right_duration"} or not event.end:
                    raise MappingError("nursing requires end time and supported side/durations")
                side = values["side"]
                if side not in {"left", "right"}:
                    raise MappingError("nursing side must be explicit")
                if ("left_duration" in values) != ("right_duration" in values):
                    raise MappingError("provide both nursing side durations")
                duration = event.end.timestamp() - start
                left = (
                    number(values["left_duration"], "left duration")
                    if "left_duration" in values
                    else duration
                    if side == "left"
                    else 0
                )
                right = (
                    number(values["right_duration"], "right duration")
                    if "right_duration" in values
                    else duration
                    if side == "right"
                    else 0
                )
                if abs(left + right - duration) > 1:
                    raise MappingError("nursing durations do not match its recorded interval")
                base.update(
                    mode="breast",
                    lastSide=side,
                    leftDuration=left,
                    rightDuration=right,
                    end_offset=offset(event.end),
                )
                if notes:
                    base["notes"] = notes
                args.update(end_time=event.end, side=side, left_duration=left, right_duration=right)
            elif kind == "pump":
                if set(values) - {"units", "total_amount", "left_amount", "right_amount", "duration"}:
                    raise MappingError("unsupported pumping field")
                base["units"] = values["units"]
                if "total_amount" in values and not ({"left_amount", "right_amount"} & values.keys()):
                    total = number(values["total_amount"], "pump amount")
                    base.update(entryMode="total", leftAmount=total / 2, rightAmount=total / 2)
                elif "total_amount" not in values and {"left_amount", "right_amount"} <= values.keys():
                    base.update(
                        entryMode="leftright",
                        leftAmount=number(values["left_amount"], "left amount"),
                        rightAmount=number(values["right_amount"], "right amount"),
                    )
                else:
                    raise MappingError("pump needs a total or both side amounts")
                if "duration" in values:
                    base.update(
                        duration=number(values["duration"], "pump duration"),
                        end_offset=offset(event.end or event.start),
                    )
                base["notes"] = notes
                args.update(values, notes=notes)
            elif kind == "activity":
                if set(values) - {"mode", "duration"}:
                    raise MappingError("unsupported activity field")
                mode = values["mode"]
                if mode not in ACTIVITY_MODES:
                    raise MappingError("unknown Huckleberry activity mode")
                base.update(mode=mode, notes=notes)
                if "duration" in values:
                    base.update(
                        duration=number(values["duration"], "activity duration"),
                        end_offset=offset(event.end or event.start),
                    )
                args.update(mode=mode, duration=base.get("duration"), notes=notes)
            else:
                raise MappingError("unsupported destination category")
            model = MODELS[kind].model_validate(base)
            payload = ft.to_firebase_dict(model)
            payload.pop("lastUpdated", None)
            return Prepared(event, payload, args)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, MappingError):
                raise
            raise MappingError(f"missing or invalid {kind} fields; check the configured mapping") from exc

    async def _client(self):
        return await self.api._get_firestore_client()

    @staticmethod
    def _record(snapshot, payload, nested_key=None):
        version = snapshot.update_time.rfc3339()
        return Record(snapshot.reference.path, payload, version, nested_key, snapshot.update_time)

    async def scan(self, prepared: Prepared):
        """Read complete relevant history, retaining addresses and propagating errors."""
        event = prepared.event
        top, sub = LOCATIONS[event.kind]
        collection = (await self._client()).collection(top).document(event.child_uid).collection(sub)
        query = (
            collection.where(filter=FieldFilter("start", "<", event.end.timestamp()))
            if event.kind == "sleep"
            else collection.where(filter=FieldFilter("start", "==", event.start.timestamp()))
        )
        records = []
        for stream in (query.stream(), collection.where(filter=FieldFilter("multi", "==", True)).stream()):
            async for snapshot in stream:
                raw = snapshot.to_dict()
                if not isinstance(raw, dict):
                    raise SyncError("Huckleberry returned an unreadable history record")
                if raw.get("multi") is True:
                    nested = raw.get("data")
                    if not isinstance(nested, dict):
                        raise SyncError("Huckleberry returned an unreadable batched history record")
                    entries = nested.items()
                else:
                    entries = [(None, raw)]
                for key, payload in entries:
                    if not isinstance(payload, dict):
                        raise SyncError("Huckleberry returned an unreadable history entry")
                    if event.kind in MODES and payload.get("mode") != MODES[event.kind]:
                        continue
                    if event.kind in {"diaper", "potty"} and bool(payload.get("isPotty")) != (
                        event.kind == "potty"
                    ):
                        continue
                    if event.kind == "activity" and payload.get("mode") != prepared.payload["mode"]:
                        continue
                    model = MODELS[event.kind].model_validate(payload)
                    when = float(model.start)
                    if event.kind == "sleep":
                        relevant = (
                            when < event.end.timestamp()
                            and when + float(model.duration) > event.start.timestamp()
                        )
                    else:
                        relevant = when == event.start.timestamp()
                    if relevant:
                        records.append(self._record(snapshot, payload, key))
        return records

    async def read(self, path, nested_key=None):
        snapshot = await (await self._client()).document(path).get()
        if not snapshot.exists:
            return None
        raw = snapshot.to_dict()
        payload = (raw.get("data") or {}).get(nested_key) if nested_key else raw
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise SyncError("Huckleberry returned invalid destination data")
        return self._record(snapshot, payload, nested_key)

    @staticmethod
    def matches(record, payload, *, ignore_offsets=False):
        ignored = {"lastUpdated", "offset", "end_offset", "notes"} if ignore_offsets else {"lastUpdated"}
        return all(record.payload.get(k) == v for k, v in payload.items() if k not in ignored)

    async def log(self, prepared):
        kind, args = prepared.event.kind, dict(prepared.kwargs)
        token = _historical_time.set(prepared.event.start)
        try:
            if kind == "activity":
                await self.api._log_activity(prepared.event.child_uid, **args, update_timer=False)
            elif kind == "bottle" and args.get("amount") is None:
                # The destination supports blank volume, but the convenience method doesn't.
                await self._blank_bottle(prepared)
            else:
                method = {
                    "sleep": "log_sleep",
                    "bottle": "log_bottle",
                    "solids": "log_solids",
                    "diaper": "log_diaper",
                    "potty": "log_potty",
                    "temperature": "log_temperature",
                    "medicine": "log_medicine",
                    "growth": "log_growth",
                    "nursing": "log_nursing",
                    "pump": "log_pump",
                }[kind]
                await getattr(self.api, method)(prepared.event.child_uid, **args)
        finally:
            _historical_time.reset(token)

    async def _blank_bottle(self, prepared):
        client = await self._client()
        root = client.collection("feed").document(prepared.event.child_uid)
        snapshot = await root.get()
        raw = snapshot.to_dict() or {}
        last = (raw.get("prefs") or {}).get("lastBottle") or {}
        ref = root.collection("intervals").document(uuid.uuid4().hex)
        payload = {**prepared.payload, "lastUpdated": time.time()}
        batch = client.batch()
        batch.create(ref, payload)
        if float(last.get("start", -1)) <= payload["start"]:
            summary = {
                "mode": "bottle",
                "start": payload["start"],
                "offset": payload["offset"],
                "bottleType": payload["bottleType"],
                "bottleUnits": payload["units"],
            }
            if snapshot.exists:
                batch.update(
                    root, {"prefs.lastBottle": summary}, option=LastUpdateOption(snapshot.update_time)
                )
            else:
                batch.create(root, {"prefs": {"lastBottle": summary}})
        await batch.commit()

    async def correct(self, previous: Record, prepared: Prepared):
        if previous.nested_key:
            raise SyncError("An imported entry was moved into a batch; review its correction manually")
        old_kind_top = previous.path.split("/")[0]
        if old_kind_top != LOCATIONS[prepared.event.kind][0]:
            raise SyncError("Source category changed; review the existing entry manually")
        client = await self._client()
        ref = client.document(previous.path)
        payload = {**previous.payload, **prepared.payload, "lastUpdated": time.time()}
        # Drop fields we previously wrote but which the corrected schema no longer has.
        known = MODELS[prepared.event.kind].model_fields
        aliases = {f.alias or n for n, f in known.items()}
        payload = {
            k: v
            for k, v in payload.items()
            if k not in aliases or k in prepared.payload or k == "lastUpdated"
        }
        root = client.collection(LOCATIONS[prepared.event.kind][0]).document(prepared.event.child_uid)
        parent = await root.get()
        parent_raw = parent.to_dict() or {}
        last_field = (
            LAST_ACTIVITIES[prepared.payload["mode"]]
            if prepared.event.kind == "activity"
            else LAST_FIELDS[prepared.event.kind]
        )
        last = (parent_raw.get("prefs") or {}).get(last_field) or {}
        # Correct a summary only when it clearly points at this old entry and is not moving before another event.
        summary_changes = {}
        if last and last.get("start") == previous.payload.get("start"):
            if payload["start"] != previous.payload["start"]:
                raise SyncError("Correction moves the latest event; review its summary manually")
            field_map = (
                {"amount": "bottleAmount", "units": "bottleUnits", "bottleType": "bottleType"}
                if prepared.event.kind == "bottle"
                else {}
            )
            for key, value in payload.items():
                summary_key = field_map.get(key, key)
                if summary_key in last and key != "lastUpdated":
                    old_value = previous.payload.get(key)
                    if last[summary_key] != old_value:
                        raise SyncError("Latest-event summary was edited; review the correction manually")
                    summary_changes[f"prefs.{last_field}.{summary_key}"] = value
            if prepared.event.kind == "nursing" and "duration" in last:
                old_duration = previous.payload.get("leftDuration", 0) + previous.payload.get(
                    "rightDuration", 0
                )
                if last["duration"] != old_duration:
                    raise SyncError("Latest nursing duration was edited; review the correction manually")
                summary_changes[f"prefs.{last_field}.duration"] = payload.get(
                    "leftDuration", 0
                ) + payload.get("rightDuration", 0)
        batch = client.batch()
        # set with a version precondition is unavailable; use update plus field deletions.
        from google.cloud.firestore_v1 import DELETE_FIELD

        if last and last.get("start") == previous.payload.get("start"):
            for key in previous.payload.keys() - payload.keys():
                summary_key = (
                    {"amount": "bottleAmount", "units": "bottleUnits"}.get(key, key)
                    if prepared.event.kind == "bottle"
                    else key
                )
                if summary_key in last:
                    if last[summary_key] != previous.payload[key]:
                        raise SyncError("Latest-event summary was edited; review the correction manually")
                    summary_changes[f"prefs.{last_field}.{summary_key}"] = DELETE_FIELD

        changes = {**payload, **{k: DELETE_FIELD for k in previous.payload if k not in payload}}
        batch.update(ref, changes, option=LastUpdateOption(previous.update_time))
        if summary_changes:
            batch.update(root, summary_changes, option=LastUpdateOption(parent.update_time))
        await batch.commit()
        return await self.read(previous.path)

    async def finish_logged_record(self, record, prepared):
        """Preserve comments/actual end offsets unsupported by some convenience signatures."""
        changes = {k: v for k, v in prepared.payload.items() if record.payload.get(k) != v}
        # A source comment and a historical end offset are the only permitted post-log additions.
        if set(changes) - {"notes", "end_offset"}:
            raise SyncError("New destination entry differs from its intended payload; review before retrying")
        if "notes" in changes and record.payload.get("notes") not in (None, ""):
            raise SyncError("New destination notes changed before binding; review manually")
        if "end_offset" in changes and record.payload.get("end_offset") not in (
            None,
            record.payload.get("offset"),
        ):
            raise SyncError("New destination end offset changed before binding; review manually")
        if changes:
            await (
                (await self._client())
                .document(record.path)
                .update(changes, option=LastUpdateOption(record.update_time))
            )
            record = await self.read(record.path)
        return record
