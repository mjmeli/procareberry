import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from procareberry import service
from procareberry.config import Settings
from procareberry.models import Report, SyncError, fingerprint
from procareberry.storage import Ledger


async def test_service_preview_import_and_restart(tmp_path, destination, monkeypatch):
    records = [
        {
            "id": "one",
            "kid_id": "kid",
            "activity_type": "bottle_activity",
            "activity_time": "2026-10-16T09:00:00-04:00",
            "data": {"amount": "4", "desc": "Formula"},
        },
        {"activity_type": "photo_activity"},
        {"activity_type": "photo_activity"},
        {"activity_type": "sign_in_activity"},
    ]
    source = SimpleNamespace(
        base="https://api-school.procareconnect.com/api/web/",
        children=AsyncMock(return_value=[{"id": "kid"}]),
        activities=AsyncMock(return_value=records),
    )
    destination.api.user_uid = "synthetic-account"
    destination.children = AsyncMock(return_value=[{"id": "child"}])

    async def connect(settings, callback):
        return await callback(source, destination)

    monkeypatch.setattr(service, "connected", connect)
    now = datetime.fromisoformat("2026-10-16T15:00:00-04:00")
    settings = Settings(data_dir=tmp_path)
    result = await service.run_window(settings, now.date(), now.date(), now=now)
    assert result["ok"] and result["counts"]["preview_create"] == 1
    settings = replace(settings, write_enabled=True)
    result = await service.run_window(settings, now.date(), now.date(), now=now)
    assert result["counts"]["created"] == 1
    result = await service.run_window(settings, now.date(), now.date(), now=now)
    assert result["counts"]["unchanged"] == 1
    assert result["source_types"] == {"bottle_activity": 1, "photo_activity": 2, "sign_in_activity": 1}
    assert result["counts"]["ignored"] == 3
    assert result["ignored_types"] == {"photo_activity": 2, "sign_in_activity": 1}
    assert json.loads((tmp_path / "status.json").read_text())["ignored_types"] == result["ignored_types"]
    assert "Synthetic" not in (tmp_path / "status.json").read_text()


async def test_service_fetch_failure_sets_failed_status(tmp_path, destination, monkeypatch):
    source = SimpleNamespace(
        base="school",
        children=AsyncMock(return_value=[{"id": "kid"}]),
        activities=AsyncMock(side_effect=SyncError("incomplete feed")),
    )
    destination.children = AsyncMock(return_value=[{"id": "child"}])
    destination.api.user_uid = "synthetic"

    async def connect(settings, callback):
        return await callback(source, destination)

    monkeypatch.setattr(service, "connected", connect)
    now = datetime.fromisoformat("2026-10-16T15:00:00-04:00")
    with pytest.raises(SyncError, match="incomplete"):
        await service.run_window(Settings(data_dir=tmp_path), now.date(), now.date(), now=now)
    assert json.loads((tmp_path / "status.json").read_text())["ok"] is False


@pytest.mark.parametrize("manual_change", ["edit", "delete"])
async def test_blank_bottle_default_reruns_corrections_and_manual_changes(
    tmp_path, destination, monkeypatch, db, manual_change
):
    records = [
        {
            "id": "blank-bottle",
            "kid_id": "kid",
            "activity_type": "bottle_activity",
            "activity_time": "2026-10-16T09:00:00-04:00",
            "data": {"amount": "3", "bottle_type": "", "desc": ""},
        }
    ]
    source = SimpleNamespace(
        base="school",
        children=AsyncMock(return_value=[{"id": "kid"}]),
        activities=AsyncMock(return_value=records),
    )
    destination.api.user_uid = "synthetic-account"
    destination.children = AsyncMock(return_value=[{"id": "child"}])

    async def connect(settings, callback):
        return await callback(source, destination)

    monkeypatch.setattr(service, "connected", connect)
    now = datetime.fromisoformat("2026-10-16T15:00:00-04:00")
    settings = Settings(data_dir=tmp_path, write_enabled=True)

    async def run():
        return await service.run_window(settings, now.date(), now.date(), now=now)

    result = await run()
    assert result["counts"] == {"skipped": 1}
    assert result["diagnostics"][0]["kind"] == "bottle"
    assert result["diagnostics"][0]["start"] == records[0]["activity_time"]
    saved = json.loads((tmp_path / "status.json").read_text())
    assert saved["diagnostics"] == result["diagnostics"]
    settings = replace(settings, default_bottle_type="Breast Milk", write_enabled=False)
    writes = db.write_count
    assert (await run())["counts"] == {"preview_create": 1}
    assert db.write_count == writes
    settings = replace(settings, write_enabled=True)
    assert (await run())["counts"]["created"] == 1
    assert (await run())["counts"] == {"unchanged": 1}
    paths = [p for p in db.data if "/intervals/" in p]
    assert len(paths) == 1
    path = paths[0]
    assert db.data[path]["bottleType"] == "Breast Milk"
    records[0]["data"]["bottle_type"] = "Breast Milk"
    assert (await run())["counts"] == {"unchanged": 1}
    records[0]["data"]["bottle_type"] = "Formula"
    assert (await run())["counts"] == {"updated": 1}
    assert db.data[path]["bottleType"] == "Formula"
    if manual_change == "edit":
        db.write(path, {**db.data[path], "amount": 7})
    else:
        del db.data[path]
    records[0]["data"]["bottle_type"] = ""
    assert (await run())["counts"] == {"skipped": 1}
    assert (await run())["counts"] == {"skipped": 1}
    assert db.data[path]["amount"] == 7 if manual_change == "edit" else path not in db.data


@pytest.mark.parametrize("changed", [None, "school", "source_child", "target_child", "malformed"])
def test_legacy_login_change_checks_child_identity(tmp_path, event, changed):
    settings = Settings(data_dir=tmp_path, procare_email="new@example.com")
    source = SimpleNamespace(base="school")
    destination = SimpleNamespace(api=SimpleNamespace(user_uid="target-account"))
    pairs = {"kid": "child"}
    legacy = fingerprint(["old@example.com", "target-account", "school", pairs])
    ledger = Ledger(tmp_path / "sync.sqlite3", legacy)
    states = ["owned", "pending", "released", "existing"]
    rows = {}
    for state in states:
        key = fingerprint(["school", "kid", state])
        body = {"event": service.serialize(event(key=key))}
        body["event"]["source_ids"] = [state]
        if state in {"released", "existing"}:
            body = {"reason": "manual change or equivalent entry"}
        if changed == "malformed" and state == "pending":
            body = {}
        ledger.put(key, "2026-10-16", state, "signature", body)
        rows[key] = ledger.get(key)
    ledger.close()
    if changed == "school":
        source.base = "other-school"
    elif changed == "source_child":
        pairs = {"other-kid": "child"}
    elif changed == "target_child":
        pairs = {"kid": "other-child"}
    if changed:
        with pytest.raises(SyncError, match="different accounts or children"):
            service.account_ledger(settings, source, destination, pairs)
        ledger = Ledger(tmp_path / "sync.sqlite3", legacy)
    else:
        # No previous email, saved Procare token, or historical feed is needed.
        ledger = service.account_ledger(settings, source, destination, pairs)
    try:
        for key, row in rows.items():
            assert ledger.get(key) == row
    finally:
        ledger.close()


@pytest.mark.parametrize("nonowned_only", [False, True])
def test_empty_or_nonowned_legacy_history_upgrades_automatically(tmp_path, nonowned_only):
    settings = Settings(data_dir=tmp_path, procare_email="new@example.com")
    source = SimpleNamespace(base="school")
    destination = SimpleNamespace(api=SimpleNamespace(user_uid="target-account"))
    pairs = {"kid": "child"}
    ledger = Ledger(tmp_path / "sync.sqlite3", "unknown-old-email-binding")
    if nonowned_only:
        ledger.put("deleted-source-key", "2026-10-16", "released", "sig", {"reason": "manual deletion"})
        ledger.put("existing-key", "2026-10-16", "existing", "sig", {"reason": "equivalent entry"})
    ledger.close()
    ledger = service.account_ledger(settings, source, destination, pairs)
    if nonowned_only:
        assert ledger.get("deleted-source-key")["state"] == "released"
        assert ledger.get("existing-key")["state"] == "existing"
    ledger.close()
    # A migrated scope remains strict, even when there are no owned events.
    for other_pairs in ({"other-kid": "child"}, {"kid": "other-child"}):
        with pytest.raises(SyncError, match="different accounts"):
            service.account_ledger(settings, source, destination, other_pairs)
    source.base = "other-school"
    with pytest.raises(SyncError, match="different accounts"):
        service.account_ledger(settings, source, destination, pairs)
    source.base = "school"
    destination.api.user_uid = "other-account"
    with pytest.raises(SyncError, match="different accounts"):
        service.account_ledger(settings, source, destination, pairs)


def test_legacy_migration_checks_all_children_and_paired_naps(tmp_path, event):
    settings = Settings(data_dir=tmp_path, procare_email="new@example.com")
    source = SimpleNamespace(base="school")
    destination = SimpleNamespace(api=SimpleNamespace(user_uid="target-account"))
    pairs = {"kid": "child", "sibling": "other-child"}
    legacy = fingerprint(["old@example.com", "target-account", "school", pairs])
    ledger = Ledger(tmp_path / "sync.sqlite3", legacy)
    for child, target in pairs.items():
        key = fingerprint(["school", child, "nap-start"])
        nap = event("sleep", end="2026-10-16T11:00:00-04:00", key=key)
        nap = replace(nap, child_uid=target, source_ids=("nap-start", "nap-end"))
        ledger.put(key, "2026-10-16", "owned", nap.signature, {"event": service.serialize(nap)})
    ledger.close()
    with pytest.raises(SyncError, match="different accounts"):
        service.account_ledger(settings, source, destination, {"kid": "child"})
    ledger = service.account_ledger(settings, source, destination, pairs)
    assert ledger.counts() == {"owned": 2}
    ledger.close()


@pytest.mark.parametrize("state", ["owned", "pending", "edited", "deleted"])
async def test_sync_after_automatic_account_switch(tmp_path, destination, event, db, state):
    settings = Settings(data_dir=tmp_path, procare_email="new@example.com", write_enabled=True)
    source = SimpleNamespace(base="school", children=AsyncMock(return_value=[{"id": "kid"}]))
    destination.api.user_uid = "target-account"
    destination.children = AsyncMock(return_value=[{"id": "child"}])

    legacy = fingerprint(["old@example.com", "target-account", "school", {"kid": "child"}])
    ledger = Ledger(tmp_path / "sync.sqlite3", legacy)
    key = fingerprint(["school", "kid", "event-1"])
    current = event(key=key)
    current = replace(current, source_ids=("event-1",))
    original_log = destination.log

    async def lose_ack(prepared):
        await original_log(prepared)
        raise TimeoutError("simulated acknowledgement loss")

    if state == "pending":
        destination.log = lose_ack
    await service.Synchronizer(destination, ledger, write=True).sync(current, Report())
    destination.log = original_log
    path = next(p for p in db.data if "/intervals/" in p)
    if state == "edited":
        db.write(path, {**db.data[path], "amount": 9})
    elif state == "deleted":
        del db.data[path]
    ledger.close()
    writes = db.write_count
    ledger = service.account_ledger(settings, source, destination, {"kid": "child"})
    assert db.write_count == writes
    try:
        syncer = service.Synchronizer(destination, ledger, write=True)
        report = Report()
        await syncer.sync(current, report)
        assert (
            report.counts
            == {
                "owned": {"unchanged": 1},
                "pending": {"recovered": 1, "unchanged": 1},
                "edited": {"skipped": 1},
                "deleted": {"skipped": 1},
            }[state]
        )
        corrected = replace(current, values={"amount": 5, "bottle_type": "Formula", "units": "oz"})
        report = Report()
        await syncer.sync(corrected, report)
        if state in {"edited", "deleted"}:
            assert report.counts == {"skipped": 1}
            assert db.data[path]["amount"] == 9 if state == "edited" else path not in db.data
        else:
            assert report.counts == {"updated": 1}
            assert db.data[path]["amount"] == 5
            assert len([p for p in db.data if "/intervals/" in p]) == 1
    finally:
        ledger.close()
    for email in ("old@example.com", "third-parent@example.com"):
        ledger = service.account_ledger(
            replace(settings, procare_email=email), source, destination, {"kid": "child"}
        )
        assert ledger.get(key)["state"] == ("released" if state in {"edited", "deleted"} else "owned")
        ledger.close()
