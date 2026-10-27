from dataclasses import replace
from datetime import datetime

import pytest

from procareberry.mapping import Mapper
from procareberry.models import Report
from procareberry.service import Synchronizer
from procareberry.storage import Ledger


@pytest.fixture
def ledger(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3", "account-binding")
    yield ledger
    ledger.close()


async def test_rerun_backfill_and_correction(destination, event, ledger, db, caplog):
    caplog.set_level("INFO", logger="procareberry")
    syncer = Synchronizer(destination, ledger, write=True)
    source = event()
    report = Report()
    await syncer.sync(source, report)
    assert report.counts.get("created") == 1
    path = ledger.get(source.source_key)["body"]["path"]
    await syncer.sync(source, report)
    assert report.counts["unchanged"] == 1
    corrected = replace(source, values={**source.values, "amount": 5})
    await syncer.sync(corrected, report)
    assert report.counts.get("updated") == 1
    assert db.data[path]["amount"] == 5
    assert len([p for p in db.data if p.startswith("feed/child/intervals/")]) == 1
    assert caplog.messages == [
        "Created bottle entry: source=event-1 start=2026-10-16T10:00:00-04:00",
        "Updated bottle entry: source=event-1 start=2026-10-16T10:00:00-04:00",
    ]


@pytest.mark.parametrize("deleted", [False, True])
async def test_manual_changes_are_preserved(destination, event, ledger, db, deleted):
    source, report = event(), Report()
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(source, report)
    path = ledger.get(source.source_key)["body"]["path"]
    if deleted:
        del db.data[path]
    else:
        # Unknown fields must also count as a manual edit.
        db.write(path, {**db.data[path], "unknown_user_field": "keep"})
    await syncer.sync(replace(source, values={**source.values, "amount": 6}), report)
    assert ledger.get(source.source_key)["state"] == "released"
    await syncer.sync(source, report)
    assert (path not in db.data) if deleted else db.data[path]["amount"] == 4
    assert len(report.diagnostics) == 2
    assert all(d["reason"] == "imported entry was manually edited or deleted" for d in report.diagnostics)
    assert all(d["start"] == source.start.isoformat() for d in report.diagnostics)


async def test_dry_run_never_writes(destination, event, ledger, db, caplog):
    caplog.set_level("INFO", logger="procareberry")
    writes = db.write_count
    report = Report()
    await Synchronizer(destination, ledger, write=False).sync(event(), report)
    assert report.counts["preview_create"] == 1
    assert db.write_count == writes
    assert ledger.get("event-1") is None
    assert not caplog.messages


async def test_failed_ack_recovers_without_duplicate(destination, event, ledger, db, caplog):
    caplog.set_level("INFO", logger="procareberry")
    original = destination.log

    async def log_then_fail(prepared):
        await original(prepared)
        raise TimeoutError("synthetic acknowledgement loss")

    destination.log = log_then_fail
    report = Report()
    await Synchronizer(destination, ledger, write=True).sync(event(), report)
    assert ledger.get("event-1")["state"] == "pending"
    assert not caplog.messages
    destination.log = original
    await Synchronizer(destination, ledger, write=True).sync(event(), report)
    assert ledger.get("event-1")["state"] == "owned"
    assert len([p for p in db.data if "/intervals/" in p]) == 1
    assert caplog.messages == ["Recovered bottle entry: source=event-1 start=2026-10-16T10:00:00-04:00"]


async def test_created_log_has_sleep_times_without_private_contents(destination, event, ledger, caplog):
    caplog.set_level("INFO", logger="procareberry")
    source = event("sleep", {"notes": "private comment"}, end="2026-10-16T11:00:00-04:00", key="a" * 64)
    await Synchronizer(destination, ledger, write=True).sync(source, Report())
    assert caplog.messages == [
        "Created sleep entry: source=aaaaaaaaaaaa "
        "start=2026-10-16T10:00:00-04:00 end=2026-10-16T11:00:00-04:00"
    ]


async def test_uncertain_missing_record_not_recreated(destination, event, ledger, db):
    original = destination.log

    async def never_ack(prepared):
        raise TimeoutError("synthetic")

    destination.log = never_ack
    report = Report()
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(event(), report)
    destination.log = original
    await syncer.sync(event(), report)
    assert report.counts["uncertain"] == 1
    assert not any("/intervals/" in p for p in db.data)


async def test_source_edit_during_pending_recovers_old_intent(destination, event, ledger, db):
    original = destination.log

    async def fail(prepared):
        await original(prepared)
        raise TimeoutError

    destination.log = fail
    report = Report()
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(event(), report)
    destination.log = original
    await syncer.sync(replace(event(), values={"amount": 9, "bottle_type": "Formula", "units": "oz"}), report)
    row = ledger.get("event-1")
    assert row["state"] == "owned"
    assert db.data[row["body"]["path"]]["amount"] == 9


async def test_sleep_overlap_blocks_import(destination, event, ledger, db):
    source = event("sleep", {}, end="2026-10-16T11:00:00-04:00")
    db.write(
        "sleep/child/intervals/manual",
        {"start": source.start.timestamp() - 300, "duration": 1800, "offset": 240},
    )
    report = Report()
    await Synchronizer(destination, ledger, write=True).sync(source, report)
    assert report.counts["conflict"] == 1
    assert report.diagnostics == [
        {
            "result": "conflict",
            "reason": "sleep overlaps existing Huckleberry history",
            "kind": "sleep",
            "source": source.source_key[:12],
            "start": source.start.isoformat(),
            "end": source.end.isoformat(),
        }
    ]
    assert ledger.get(source.source_key) is None


@pytest.mark.parametrize("manual", [False, True])
async def test_split_nap_with_stale_start_preserves_manual_sleep_and_reruns(destination, ledger, db, manual):
    rows = [
        {
            "id": identifier,
            "kid_id": "kid",
            "activity_type": "nap_activity",
            "activity_time": when,
            "data": {field: when},
        }
        for identifier, field, when in [
            ("stale", "start_time", "2026-10-15T14:00:00-04:00"),
            ("start", "start_time", "2026-10-16T10:00:00-04:00"),
            ("end", "end_time", "2026-10-16T11:00:00-04:00"),
        ]
    ]
    mapper = Mapper("America/New_York")
    source = mapper.map(rows, "school", "kid", "child", Report())[0]
    manual_path = "sleep/child/intervals/manual"
    manual_payload = {"start": source.start.timestamp(), "duration": 3600, "offset": 240}
    if manual:
        db.write(manual_path, manual_payload)
    initial_writes = db.write_count
    syncer, report = Synchronizer(destination, ledger, write=True), Report()
    await syncer.sync(source, report)
    await syncer.sync(source, report)
    rows[-1]["data"]["end_time"] = "2026-10-16T11:15:00-04:00"
    corrected = mapper.map(rows, "school", "kid", "child", Report())[0]
    assert corrected.source_key == source.source_key
    await syncer.sync(corrected, report)
    assert len([p for p in db.data if p.startswith("sleep/child/intervals/")]) == 1
    if manual:
        assert db.data[manual_path] == manual_payload
        assert db.write_count == initial_writes
        assert ledger.get(source.source_key) is None
        assert report.counts["conflict"] == 3
    else:
        assert report.counts["created"] == report.counts["unchanged"] == report.counts["updated"] == 1
        path = ledger.get(source.source_key)["body"]["path"]
        assert db.data[path]["duration"] == 4500


async def test_failed_history_read_blocks_import(destination, event, ledger, db):
    db.fail_reads = True
    report = Report()
    await Synchronizer(destination, ledger, write=True).sync(event(), report)
    assert report.counts["failed"] == 1
    assert ledger.get("event-1") is None


async def test_distinct_same_time_events_not_fuzzily_merged(destination, event, ledger, db):
    syncer, report = Synchronizer(destination, ledger, write=True), Report()
    await syncer.sync(event(), report)
    await syncer.sync(
        event(values={"amount": 1, "bottle_type": "Formula", "units": "oz"}, key="event-2"), report
    )
    assert report.counts["created"] == 2


async def test_conditional_correction_rejects_concurrent_edit(destination, event, ledger, db):
    syncer, report = Synchronizer(destination, ledger, write=True), Report()
    await syncer.sync(event(), report)
    original = destination.correct

    async def racing_edit(record, prepared):
        db.write(record.path, {**db.data[record.path], "notes": "concurrent manual edit"})
        return await original(record, prepared)

    destination.correct = racing_edit
    await syncer.sync(replace(event(), values={"amount": 8, "bottle_type": "Formula", "units": "oz"}), report)
    path = ledger.get("event-1")["body"]["path"]
    assert db.data[path]["amount"] == 4
    assert db.data[path]["notes"] == "concurrent manual edit"
    assert report.counts["failed"] == 1


async def test_ambiguous_new_records_stay_pending(destination, event, ledger, db):
    original = destination.log

    async def duplicated(prepared):
        await original(prepared)
        await original(prepared)

    destination.log = duplicated
    report = Report()
    await Synchronizer(destination, ledger, write=True).sync(event(), report)
    assert report.counts["uncertain"] == 1
    assert ledger.get("event-1")["state"] == "pending"


@pytest.mark.parametrize("kind", ["diaper", "bottle", "solids", "sleep"])
@pytest.mark.parametrize("time", ["09:53:00", "10:07:00"])
async def test_latest_time_correction_updates_summary_and_reruns(destination, event, ledger, db, kind, time):
    from procareberry.huckleberry import LAST_FIELDS, LOCATIONS

    values = {
        "diaper": {"mode": "pee"},
        "bottle": {"amount": 4, "bottle_type": "Formula", "units": "oz"},
        "solids": {"foods": [{"name": "Banana"}]},
        "sleep": {},
    }[kind]
    source = event(kind, values, end="2026-10-16T11:00:00-04:00" if kind == "sleep" else None)
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(source, Report())
    path = ledger.get(source.source_key)["body"]["path"]
    corrected = replace(source, start=datetime.fromisoformat(f"2026-10-16T{time}-04:00"))
    writes = db.write_count
    preview = Report()
    await Synchronizer(destination, ledger, write=False).sync(corrected, preview)
    assert preview.counts == {"preview_update": 1}
    assert db.write_count == writes
    report = Report()
    await syncer.sync(corrected, report)
    await syncer.sync(corrected, report)
    assert report.counts == {"updated": 1, "unchanged": 1}
    assert db.data[path]["start"] == corrected.start.timestamp()
    summary = db.data[f"{LOCATIONS[kind][0]}/child"]["prefs"][LAST_FIELDS[kind]]
    assert summary["start"] == corrected.start.timestamp()
    if kind == "sleep":
        assert summary["duration"] == corrected.end.timestamp() - corrected.start.timestamp()
    assert len([p for p in db.data if "/intervals/" in p]) == 1


@pytest.mark.parametrize("batched", [False, True])
async def test_time_correction_crossing_other_history_is_conflict(destination, event, ledger, db, batched):
    source = event("diaper", {"mode": "pee"})
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(source, Report())
    other = {"start": source.start.timestamp() - 180, "mode": "poo", "offset": 240}
    db.write("diaper/child/intervals/other", {"multi": True, "data": {"one": other}} if batched else other)
    writes = db.write_count
    original = ledger.get(source.source_key)
    corrected = replace(source, start=datetime.fromisoformat("2026-10-16T09:53:00-04:00"))
    report = Report()
    await syncer.sync(corrected, report)
    await syncer.sync(corrected, report)
    assert report.counts == {"conflict": 2}
    assert db.write_count == writes
    assert ledger.get(source.source_key) == original


async def test_time_correction_preserves_newer_summary_and_blocks_overtaking(destination, event, ledger, db):
    source = event("diaper", {"mode": "pee"})
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(source, Report())
    await destination.log(
        await destination.prepare(event("diaper", {"mode": "poo"}, start="2026-10-16T11:00:00-04:00"))
    )
    summary = dict(db.data["diaper/child"]["prefs"]["lastDiaper"])
    report = Report()
    await syncer.sync(replace(source, start=datetime.fromisoformat("2026-10-16T10:30:00-04:00")), report)
    assert report.counts == {"updated": 1}
    assert db.data["diaper/child"]["prefs"]["lastDiaper"] == summary
    writes = db.write_count
    await syncer.sync(replace(source, start=datetime.fromisoformat("2026-10-16T11:30:00-04:00")), report)
    assert report.counts["conflict"] == 1
    assert db.write_count == writes


async def test_manual_summary_edit_blocks_time_correction_without_failure(destination, event, ledger, db):
    source = event("diaper", {"mode": "pee"})
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(source, Report())
    db.write("diaper/child", {"prefs.lastDiaper.mode": "poo"}, "update")
    writes = db.write_count
    report = Report()
    await syncer.sync(replace(source, start=datetime.fromisoformat("2026-10-16T09:53:00-04:00")), report)
    assert report.counts == {"conflict": 1}
    assert db.write_count == writes


async def test_time_correction_batch_rejects_concurrent_summary_edit(destination, event, ledger, db):
    source = event("diaper", {"mode": "pee"})
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(source, Report())
    path = ledger.get(source.source_key)["body"]["path"]
    original_batch = db.batch

    def racing_batch():
        batch = original_batch()
        commit = batch.commit

        async def racing_commit():
            db.write("diaper/child", {"prefs.lastDiaper.mode": "poo"}, "update")
            return await commit()

        batch.commit = racing_commit
        return batch

    db.batch = racing_batch
    report = Report()
    await syncer.sync(replace(source, start=datetime.fromisoformat("2026-10-16T09:53:00-04:00")), report)
    assert report.counts == {"failed": 1}
    assert db.data[path]["start"] == source.start.timestamp()
    assert db.data["diaper/child"]["prefs"]["lastDiaper"]["mode"] == "poo"


async def test_time_correction_ignores_other_category_history(destination, event, ledger, db):
    source = event()
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(source, Report())
    db.write(
        "feed/child/intervals/other",
        {"multi": True, "data": {"solid": {"start": source.start.timestamp(), "mode": "solids"}}},
    )
    report = Report()
    await syncer.sync(replace(source, start=datetime.fromisoformat("2026-10-16T09:53:00-04:00")), report)
    assert report.counts == {"updated": 1}


async def test_same_time_history_blocks_ambiguous_summary_correction(destination, event, ledger, db):
    source = event("diaper", {"mode": "pee"})
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(source, Report())
    db.write(
        "diaper/child/intervals/other", {"start": source.start.timestamp(), "mode": "pee", "offset": 240}
    )
    writes = db.write_count
    report = Report()
    await syncer.sync(replace(source, start=datetime.fromisoformat("2026-10-16T10:07:00-04:00")), report)
    assert report.counts == {"conflict": 1}
    assert db.write_count == writes


async def test_lost_time_correction_ack_does_not_duplicate_or_overwrite(destination, event, ledger, db):
    source = event("diaper", {"mode": "pee"})
    syncer = Synchronizer(destination, ledger, write=True)
    await syncer.sync(source, Report())
    correct = destination.correct

    async def lose_ack(record, prepared):
        await correct(record, prepared)
        raise TimeoutError("synthetic acknowledgement loss")

    destination.correct = lose_ack
    corrected = replace(source, start=datetime.fromisoformat("2026-10-16T09:53:00-04:00"))
    report = Report()
    await syncer.sync(corrected, report)
    assert report.counts == {"failed": 1}
    writes = db.write_count
    await syncer.sync(corrected, report)
    assert report.counts["skipped"] == 1
    assert db.write_count == writes
    assert len([p for p in db.data if "/intervals/" in p]) == 1
    assert db.data["diaper/child"]["prefs"]["lastDiaper"]["start"] == corrected.start.timestamp()
