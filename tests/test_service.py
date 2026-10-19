import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from procareberry import service
from procareberry.config import Settings
from procareberry.models import SyncError


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
    from dataclasses import replace

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
    from dataclasses import replace

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
