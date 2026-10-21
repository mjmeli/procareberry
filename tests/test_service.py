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


@pytest.mark.parametrize("changed", [None, "school", "destination", "source_child", "target_child", "email"])
async def test_switch_legacy_procare_account_preserves_history(tmp_path, monkeypatch, changed):
    settings = Settings(data_dir=tmp_path, procare_email="new@example.com", write_enabled=True)
    source = SimpleNamespace(base="school", children=AsyncMock(return_value=[{"id": "kid"}]))
    destination = SimpleNamespace(
        api=SimpleNamespace(user_uid="target-account"),
        children=AsyncMock(return_value=[{"id": "child"}]),
    )
    binding = fingerprint(["old@example.com", "target-account", "school", {"kid": "child"}])
    ledger = Ledger(tmp_path / "sync.sqlite3", binding)
    states = ["owned", "pending", "released", "existing"]
    for state in states:
        ledger.put(state, "2026-10-16", state, "signature", {"preserved": state})
    ledger.close()

    async def connect(settings, callback):
        return await callback(source, destination)

    monkeypatch.setattr(service, "connected", connect)
    previous_email = "OLD@example.com"
    if changed == "school":
        source.base = "other-school"
    elif changed == "destination":
        destination.api.user_uid = "other-account"
    elif changed == "source_child":
        source.children.return_value = [{"id": "other-kid"}]
    elif changed == "target_child":
        destination.children.return_value = [{"id": "other-child"}]
    elif changed == "email":
        previous_email = "wrong@example.com"
    if changed:
        with pytest.raises(SyncError, match="different accounts"):
            await service.switch_procare_account(settings, previous_email)
        ledger = Ledger(tmp_path / "sync.sqlite3", binding)
    else:
        # Ordinary sync cannot silently claim a legacy history from another email.
        with pytest.raises(SyncError, match="switch-procare-account"):
            service.account_ledger(settings, source, destination, {"kid": "child"})
        await service.switch_procare_account(settings, previous_email)
        # Restarting with either login needs no further migration.
        for email in ("new@example.com", "old@example.com"):
            ledger = service.account_ledger(
                replace(settings, procare_email=email), source, destination, {"kid": "child"}
            )
            ledger.close()
        ledger = service.account_ledger(settings, source, destination, {"kid": "child"})
    try:
        for state in states:
            row = ledger.get(state)
            assert row["state"] == state
            assert row["signature"] == "signature"
            assert row["body"] == {"preserved": state}
    finally:
        ledger.close()


async def test_switch_requires_existing_history_before_connecting(tmp_path, monkeypatch):
    connect = AsyncMock()
    monkeypatch.setattr(service, "connected", connect)
    with pytest.raises(SyncError, match="No sync history"):
        await service.switch_procare_account(Settings(data_dir=tmp_path), "old@example.com")
    with pytest.raises(SyncError, match="previous Procare email"):
        await service.switch_procare_account(Settings(data_dir=tmp_path), " ")
    connect.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_matching_legacy_login_upgrades_and_keeps_scope_checks(tmp_path):
    settings = Settings(data_dir=tmp_path, procare_email="old@example.com")
    source = SimpleNamespace(base="school")
    destination = SimpleNamespace(api=SimpleNamespace(user_uid="target-account"))
    pairs = {"kid": "child"}
    legacy = fingerprint(["old@example.com", "target-account", "school", pairs])
    Ledger(tmp_path / "sync.sqlite3", legacy).close()
    service.account_ledger(settings, source, destination, pairs).close()
    # The original account can upgrade automatically, then either parent can sync.
    settings = replace(settings, procare_email="new@example.com")
    service.account_ledger(settings, source, destination, pairs).close()
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


@pytest.mark.parametrize("state", ["owned", "pending", "edited", "deleted"])
async def test_sync_after_account_switch(tmp_path, monkeypatch, destination, event, db, state):
    settings = Settings(data_dir=tmp_path, procare_email="new@example.com", write_enabled=True)
    source = SimpleNamespace(base="school", children=AsyncMock(return_value=[{"id": "kid"}]))
    destination.api.user_uid = "target-account"
    destination.children = AsyncMock(return_value=[{"id": "child"}])

    async def connect(settings, callback):
        return await callback(source, destination)

    monkeypatch.setattr(service, "connected", connect)
    legacy = fingerprint(["old@example.com", "target-account", "school", {"kid": "child"}])
    ledger = Ledger(tmp_path / "sync.sqlite3", legacy)
    original_log = destination.log

    async def lose_ack(prepared):
        await original_log(prepared)
        raise TimeoutError("simulated acknowledgement loss")

    if state == "pending":
        destination.log = lose_ack
    await service.Synchronizer(destination, ledger, write=True).sync(event(), Report())
    destination.log = original_log
    path = next(p for p in db.data if "/intervals/" in p)
    if state == "edited":
        db.write(path, {**db.data[path], "amount": 9})
    elif state == "deleted":
        del db.data[path]
    ledger.close()
    writes = db.write_count
    await service.switch_procare_account(settings, "old@example.com")
    assert db.write_count == writes
    ledger = service.account_ledger(settings, source, destination, {"kid": "child"})
    try:
        syncer = service.Synchronizer(destination, ledger, write=True)
        report = Report()
        await syncer.sync(event(), report)
        assert (
            report.counts
            == {
                "owned": {"unchanged": 1},
                "pending": {"recovered": 1, "unchanged": 1},
                "edited": {"skipped": 1},
                "deleted": {"skipped": 1},
            }[state]
        )
        corrected = event(values={"amount": 5, "bottle_type": "Formula", "units": "oz"})
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
