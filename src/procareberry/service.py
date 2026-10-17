"""Single-writer orchestration, durable import tracking, and safe retries."""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from .config import check_window
from .huckleberry import HistoricalAPI, Huckleberry
from .mapping import Mapper
from .models import Event, MappingError, Prepared, Report, SyncError, fingerprint
from .procare import AiohttpTransport, ProcareClient
from .storage import Ledger, private_json, read_json

LOG = logging.getLogger("procareberry")


def serialize(event):
    return {
        "source_key": event.source_key,
        "kind": event.kind,
        "child_uid": event.child_uid,
        "start": event.start.isoformat(),
        "end": event.end.isoformat() if event.end else None,
        "values": event.values,
        "source_ids": event.source_ids,
    }


def deserialize(raw):
    return Event(
        raw["source_key"],
        raw["kind"],
        raw["child_uid"],
        datetime.fromisoformat(raw["start"]),
        datetime.fromisoformat(raw["end"]) if raw["end"] else None,
        raw["values"],
        tuple(raw["source_ids"]),
    )


class Synchronizer:
    def __init__(self, destination, ledger, *, write, preview=None):
        self.destination, self.ledger, self.write = destination, ledger, write
        self.preview = preview or (lambda event, action, payload: None)

    def _put(self, event, state, body, signature=None):
        self.ledger.put(
            event.source_key, event.start.date().isoformat(), state, signature or event.signature, body
        )

    async def _reconcile(self, row, report):
        body = row["body"]
        event = deserialize(body["event"])
        prepared = Prepared(event, body["payload"], {})
        records = await self.destination.scan(prepared)
        new = [
            r
            for r in records
            if r.identity not in body["baseline"]
            and self.destination.matches(r, prepared.payload, ignore_offsets=True)
        ]
        if len(new) != 1:
            report.add("uncertain", "pending write needs manual review; it will not be blindly retried")
            return None
        if not self.write:
            report.add("preview_recovery")
            return None
        record = await self.destination.finish_logged_record(new[0], prepared)
        self._put(
            event,
            "owned",
            {
                "path": record.path,
                "nested_key": record.nested_key,
                "payload": record.payload,
                "version": record.version,
                "event": serialize(event),
            },
            row["signature"],
        )
        report.add("recovered")
        return self.ledger.get(event.source_key)

    async def sync(self, event, report):
        try:
            row = self.ledger.get(event.source_key)
            if row and row["state"] in {"released", "existing"}:
                report.add("skipped", "entry was manually changed/deleted or already existed")
                return
            if row and row["state"] == "pending":
                row = await self._reconcile(row, report)
                if not row:
                    return
            if row and row["state"] == "owned":
                old = row["body"]
                # Inspect owned records even without source changes, preserving deletions through backfill.
                record = await self.destination.read(old["path"], old.get("nested_key"))
                if record is None or record.version != old["version"] or record.payload != old["payload"]:
                    if self.write:
                        self._put(event, "released", {"reason": "manual edit or deletion"})
                    report.add("skipped", "imported entry was manually edited or deleted")
                    return
                if row["signature"] == event.signature:
                    report.add("unchanged")
                    return
                original = deserialize(old["event"])
                if original.child_uid != event.child_uid or original.kind != event.kind:
                    report.add("conflict", "source category or child mapping changed; review manually")
                    return
                prepared = await self.destination.prepare(event)
                overlaps = await self.destination.scan(prepared) if event.kind == "sleep" else []
                if any(r.identity != record.identity for r in overlaps):
                    report.add("conflict", "corrected sleep overlaps another entry")
                    return
                self.preview(event, "update", prepared.payload)
                if not self.write:
                    report.add("preview_update")
                    return
                corrected = await self.destination.correct(record, prepared)
                if corrected is None:
                    raise SyncError("Corrected destination record disappeared; review before retrying")
                self._put(
                    event,
                    "owned",
                    {
                        "path": corrected.path,
                        "nested_key": corrected.nested_key,
                        "payload": corrected.payload,
                        "version": corrected.version,
                        "event": serialize(event),
                    },
                )
                report.add("updated")
                return
            prepared = await self.destination.prepare(event)
            before = await self.destination.scan(prepared)
            if event.kind == "sleep" and before:
                report.add("conflict", "sleep overlaps existing Huckleberry history")
                return
            equivalent = [r for r in before if self.destination.matches(r, prepared.payload)]
            if equivalent:
                if self.write:
                    self._put(event, "existing", {"reason": "equivalent destination entry"})
                report.add("skipped", "equivalent entry already exists; no ownership acquired")
                return
            self.preview(event, "create", prepared.payload)
            if not self.write:
                report.add("preview_create")
                return
            # This row commits before any remote call. Failed/ambiguous requests remain pending.
            body = {
                "event": serialize(event),
                "payload": prepared.payload,
                "baseline": [r.identity for r in before],
            }
            self._put(event, "pending", body)
            await self.destination.log(prepared)
            recovered = await self._reconcile(self.ledger.get(event.source_key), report)
            if recovered:
                report.counts["recovered"] -= 1
                report.add("created")
        except MappingError as exc:
            report.add("skipped", str(exc))
        except Exception as exc:
            # Upstream exceptions may contain patient data or authentication response bodies.
            reason = (
                str(exc)
                if isinstance(exc, SyncError)
                else f"destination operation failed ({type(exc).__name__}); retry or inspect status"
            )
            report.add("failed", reason)


def child_pairs(source, destination, configured):
    source_ids, target_ids = {str(c["id"]) for c in source}, {c["id"] for c in destination}
    if configured:
        if not set(configured) <= source_ids or not set(configured.values()) <= target_ids:
            raise SyncError("CHILD_UID_MAP references an unavailable child; run children to inspect IDs")
        return configured
    if len(source_ids) == len(target_ids) == 1:
        return {next(iter(source_ids)): next(iter(target_ids))}
    raise SyncError("Set CHILD_UID_MAP when either account has multiple children")


async def connected(settings, callback):
    settings.require_credentials()
    async with aiohttp.ClientSession() as session:
        source = ProcareClient(
            AiohttpTransport(session),
            settings.procare_email,
            settings.procare_password,
            settings.data_dir / "procare_tokens.json",
        )
        api = HistoricalAPI(
            settings.huckleberry_email, settings.huckleberry_password, settings.timezone, session
        )
        destination = Huckleberry(api, settings.timezone)
        await destination.authenticate(settings.data_dir / "huckleberry_tokens.json")
        try:
            return await callback(source, destination)
        finally:
            destination.save_tokens(settings.data_dir / "huckleberry_tokens.json")
            client = api._firestore_client
            if client is not None:
                await client.close()


async def run_window(settings, start, end, *, preview=None, now=None):
    now = now or datetime.now(ZoneInfo(settings.timezone))
    today = now.astimezone(ZoneInfo(settings.timezone)).date()
    check_window(start, end, today)
    started = now.isoformat()

    async def work(source, destination):
        pairs = child_pairs(await source.children(), await destination.children(), settings.child_map)
        # Binding includes account and selected children, so a configuration change cannot redirect ownership.
        binding = fingerprint(
            [settings.procare_email.casefold(), destination.api.user_uid, source.base, pairs]
        )
        ledger = Ledger(settings.data_dir / "sync.sqlite3", binding)
        report = Report()
        try:
            ledger.prune(today)
            mapper = Mapper(settings.timezone)
            syncer = Synchronizer(destination, ledger, write=settings.write_enabled, preview=preview)
            for source_child, target_child in pairs.items():
                records = await source.activities(source_child, start, end)
                events = mapper.map(records, source.base, source_child, target_child, report)
                for event in events:
                    if event.kind not in settings.categories:
                        report.add("disabled")
                    elif event.start.date() < start or event.start.date() > end:
                        report.add("skipped", "event is outside requested dates")
                    elif (
                        event.start.timestamp() > now.timestamp()
                        or event.end is not None
                        and event.end.timestamp() > now.timestamp()
                    ):
                        report.add("skipped", "event is not yet completed")
                    else:
                        await syncer.sync(event, report)
            result = {
                "started": started,
                "finished": datetime.now(ZoneInfo(settings.timezone)).isoformat(),
                "ok": not any(report.counts.get(k) for k in ("failed", "uncertain")),
                "write_enabled": settings.write_enabled,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "counts": report.counts,
                "reasons": report.reasons,
                "source_types": report.source_types,
                "ledger": ledger.counts(),
            }
            private_json(settings.data_dir / "status.json", result)
            return result
        finally:
            ledger.close()

    try:
        return await connected(settings, work)
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, SyncError)
            else f"sync failed ({type(exc).__name__}); check account access and connectivity"
        )
        private_json(
            settings.data_dir / "status.json",
            {
                "started": started,
                "finished": datetime.now(ZoneInfo(settings.timezone)).isoformat(),
                "ok": False,
                "error": message,
            },
        )
        raise SyncError(message) from exc


def health(settings, *, now=None):
    status = read_json(settings.data_dir / "status.json")
    try:
        finished = datetime.fromisoformat(status["finished"])
        now = now or datetime.now(ZoneInfo(settings.timezone))
        return bool(
            status["ok"] and timedelta(0) <= now - finished <= timedelta(hours=settings.health_max_age_hours)
        )
    except (KeyError, ValueError, TypeError):
        return False
