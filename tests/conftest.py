from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from huckleberry_api.firebase_types import FirebaseMedicationTypeDocument

from procareberry.huckleberry import HistoricalAPI, Huckleberry
from procareberry.models import Event

from .fakes import DB


@pytest.fixture
def db():
    db = DB()
    for name in ("sleep", "feed", "diaper", "health", "pump", "activities"):
        db.write(name + "/child", {"prefs": {}})
    return db


@pytest.fixture
def destination(db):
    api = HistoricalAPI("fake@example.invalid", "fake-password", "America/New_York", None)
    api._get_firestore_client = AsyncMock(return_value=db)
    api.list_solids_curated_foods = AsyncMock(return_value=[SimpleNamespace(id="banana", name="Banana")])
    api.list_solids_custom_foods = AsyncMock(return_value=[])
    api.list_medicine_types = AsyncMock(
        return_value=[
            FirebaseMedicationTypeDocument(_id="vitamin", active=True, mode="medication", name="Vitamin")
        ]
    )
    return Huckleberry(api, "America/New_York")


@pytest.fixture
def event():
    def make(kind="bottle", values=None, *, start="2026-10-16T10:00:00-04:00", end=None, key="event-1"):
        return Event(
            key,
            kind,
            "child",
            datetime.fromisoformat(start).astimezone(ZoneInfo("America/New_York")),
            datetime.fromisoformat(end).astimezone(ZoneInfo("America/New_York")) if end else None,
            values if values is not None else {"amount": 4, "bottle_type": "Formula", "units": "oz"},
            (key,),
        )

    return make
