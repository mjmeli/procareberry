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
        }
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
    assert result["source_types"] == {"bottle_activity": 1}
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
