import copy
from datetime import datetime
from importlib.metadata import version
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from procareberry.huckleberry import HistoricalAPI, Huckleberry
from procareberry.models import MappingError


@pytest.mark.parametrize(
    "kind,values,end",
    [
        ("sleep", {"notes": "synthetic nap"}, "2026-10-16T11:00:00-04:00"),
        ("bottle", {"amount": 4, "bottle_type": "Formula", "units": "oz", "notes": "synthetic note"}, None),
        ("bottle", {"bottle_type": "Breast Milk", "units": "oz"}, None),
        ("solids", {"foods": [{"name": "Banana"}], "notes": "Some"}, None),
        (
            "diaper",
            {"mode": "both", "pee_amount": "medium", "poo_amount": "little", "notes": "synthetic"},
            None,
        ),
        ("potty", {"mode": "pee", "how_it_happened": "wentPotty"}, None),
        ("temperature", {"amount": 37.2, "units": "C"}, None),
        ("medicine", {"medicine": "Vitamin", "amount": 1, "units": "ml"}, None),
        ("growth", {"weight": 7, "height": 65, "units": "metric"}, None),
        ("nursing", {"side": "left", "notes": "synthetic"}, "2026-10-16T10:10:00-04:00"),
        ("pump", {"total_amount": 120, "units": "ml", "duration": 900}, None),
        ("activity", {"mode": "tummyTime", "duration": 300, "notes": "synthetic"}, None),
    ],
)
async def test_real_pinned_logging_contract(destination, event, kind, values, end):
    assert version("huckleberry-api") == "0.4.8"
    planned = await destination.prepare(event(kind, values, end=end))
    before = await destination.scan(planned)
    assert before == []
    await destination.log(planned)
    after = await destination.scan(planned)
    matches = [r for r in after if destination.matches(r, planned.payload, ignore_offsets=True)]
    assert len(matches) == 1
    record = await destination.finish_logged_record(matches[0], planned)
    assert destination.matches(record, planned.payload)


@pytest.mark.parametrize("paused", [False, True])
async def test_historical_activity_never_changes_timer(db, destination, event, paused):
    timer = {
        "tummyTime": {
            "active": True,
            "paused": paused,
            "startTime": 1234567890000,
            "duration": 42,
            "notes": "keep",
            "uuid": "0123456789abcdef",
        }
    }
    db.write("activities/child", {"prefs": {}, "timer": timer})
    original = copy.deepcopy(timer)
    planned = await destination.prepare(event("activity", {"mode": "tummyTime", "duration": 60}))
    await destination.log(planned)
    assert db.data["activities/child"]["timer"] == original


async def test_backfill_preserves_newer_summary(db, destination, event):
    db.write(
        "feed/child",
        {
            "prefs": {
                "lastBottle": {
                    "start": 9999999999,
                    "bottleType": "Breast Milk",
                    "bottleUnits": "ml",
                    "bottleAmount": 50,
                }
            }
        },
    )
    previous = copy.deepcopy(db.data["feed/child"])
    await destination.log(await destination.prepare(event()))
    assert db.data["feed/child"] == previous


async def test_historical_offsets_cross_dst(destination, event):
    planned = await destination.prepare(
        event("sleep", {}, start="2026-11-01T01:30:00-04:00", end="2026-11-01T01:30:00-05:00")
    )
    assert planned.payload["duration"] == 3600
    assert planned.payload["offset"] == 240
    assert planned.payload["end_offset"] == 300
    await destination.log(planned)
    record = (await destination.scan(planned))[0]
    corrected = await destination.finish_logged_record(record, planned)
    assert corrected.payload["end_offset"] == 300


async def test_nested_history_and_failed_reads(db, destination, event):
    planned = await destination.prepare(event("sleep", {}, end="2026-10-16T11:00:00-04:00"))
    nested = {**planned.payload, "start": planned.payload["start"] - 1000, "duration": 8000}
    db.write("sleep/child/intervals/batch", {"multi": True, "data": {"entry": nested}})
    records = await destination.scan(planned)
    assert len(records) == 1 and records[0].nested_key == "entry"
    db.fail_reads = True
    with pytest.raises(RuntimeError):
        await destination.scan(planned)


async def test_no_catalog_writes_and_ambiguous_food(destination, event):
    destination.api.create_solids_custom_food = AsyncMock()
    with pytest.raises(MappingError):
        await destination.prepare(event("solids", {"foods": [{"name": "Unknown"}]}))
    destination.api.create_solids_custom_food.assert_not_called()


async def test_offset_override_does_not_leak(db):
    api = HistoricalAPI("fake", "fake", "America/New_York", None)
    api._get_firestore_client = AsyncMock(return_value=db)
    assert (
        await api._get_timezone_offset_minutes()
        == -datetime.now(ZoneInfo("America/New_York")).utcoffset().total_seconds() / 60
    )
    assert Huckleberry(api, "America/New_York")


@pytest.mark.parametrize(
    "kind,values",
    [
        ("growth", {"weight": 15.5, "height": 25, "head": 15, "units": "imperial"}),
        ("medicine", {"medicine": "Vitamin"}),
        ("diaper", {"mode": "dry"}),
        ("solids", {"foods": [{"name": "Banana", "amount": "some"}], "reaction": "LOVED"}),
        ("pump", {"left_amount": 2, "right_amount": 1, "units": "oz"}),
        ("activity", {"mode": "storyTime"}),
        ("activity", {"mode": "storyTime", "duration": 0}),
    ],
)
async def test_additional_valid_destination_shapes(destination, event, kind, values):
    prepared = await destination.prepare(event(kind, values))
    await destination.log(prepared)
    records = await destination.scan(prepared)
    assert len(records) == 1
    actual = await destination.finish_logged_record(records[0], prepared)
    assert destination.matches(actual, prepared.payload)
