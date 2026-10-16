import json
from pathlib import Path

import pytest

from procareberry.mapping import Mapper, timestamp
from procareberry.models import MappingError, Report


def record(identifier="nap", kind="nap_activity", data=None, child="kid"):
    return {
        "id": identifier,
        "kid_id": child,
        "activity_type": kind,
        "activity_time": "2026-10-16T10:00:00-04:00",
        "data": data or {},
    }


def test_default_payload_shapes():
    records = json.loads((Path(__file__).parent / "fixtures/activities.json").read_text())
    report = Report()
    events = Mapper("America/New_York", {"bottle_types": {"Synthetic formula": "Formula"}}).map(
        records, "school", "kid", "child", report
    )
    assert {e.kind for e in events} == {"sleep", "bottle", "diaper", "solids"}
    assert next(e for e in events if e.kind == "bottle").values["amount"] == 4
    assert next(e for e in events if e.kind == "diaper").values["mode"] == "both"
    assert report.counts["ignored"] == report.counts["skipped"] == 1


def test_missing_milk_type_not_guessed():
    report = Report()
    events = Mapper("UTC").map(
        [record(kind="bottle_activity", data={"amount": "5", "desc": "unknown"})], "s", "kid", "child", report
    )
    assert not events and "explicit bottle_types" in next(iter(report.reasons))


def test_split_nap_identity_stays_anchored_to_start():
    mapper, report = Mapper("America/New_York"), Report()
    start = record("start", data={"start_time": "2026-10-16T10:00:00-04:00"})
    end = record("end", data={"end_time": "2026-10-16T11:00:00-04:00"})
    combined = record(
        "start", data={"start_time": "2026-10-16T10:00:00-04:00", "end_time": "2026-10-16T11:00:00-04:00"}
    )
    split_event = mapper.map([end, start], "s", "kid", "child", report)[0]
    full_event = mapper.map([combined], "s", "kid", "child", Report())[0]
    assert split_event.source_key == full_event.source_key
    assert split_event.source_ids == ("start", "end")


def test_ambiguous_and_other_child_naps_not_paired():
    mapper, report = Mapper("America/New_York"), Report()
    rows = [
        record("start-1", data={"start_time": "2026-10-16T09:00:00-04:00"}),
        record("start-2", data={"start_time": "2026-10-16T10:00:00-04:00"}),
        record("end", data={"end_time": "2026-10-16T11:00:00-04:00"}),
        record("other", data={"end_time": "2026-10-16T11:00:00-04:00"}, child="other"),
    ]
    assert mapper.map(rows, "s", "kid", "child", report) == []
    assert "ambiguous or missing nap start" in report.reasons
    assert "activity belongs to another child" in report.reasons


def test_missing_id_and_invalid_volume_skipped():
    mapper, report = Mapper("UTC", {"bottle_types": {"f": "Formula"}}), Report()
    bad = record(kind="bottle_activity", data={"amount": "NaN", "desc": "f"})
    no_id = record()
    del no_id["id"]
    assert not mapper.map([bad, no_id], "s", "kid", "child", report)
    assert report.counts["skipped"] == 2


def test_explicit_rule_does_not_infer_unknown_value():
    options = {
        "source_rules": {
            "school_temp": {
                "kind": "temperature",
                "fields": {"amount": "data.value", "units": "data.unit"},
                "values": {"units": {"Celsius": "C"}},
            }
        }
    }
    mapper, report = Mapper("UTC", options), Report()
    rows = [
        record("a", "school_temp", {"value": 37, "unit": "Celsius"}),
        record("b", "school_temp", {"value": 99, "unit": "unknown"}),
    ]
    events = mapper.map(rows, "s", "kid", "child", report)
    assert len(events) == 1 and events[0].values == {"amount": 37, "units": "C"}
    assert report.counts["skipped"] == 1


def test_dst_ambiguous_local_times_rejected_and_offset_times_preserved():
    mapper = Mapper("America/New_York")
    for value in ("2026-11-01T01:30:00", "2026-03-08T02:30:00"):
        with pytest.raises(MappingError):
            timestamp(value, mapper.tz)
    raw = record(data={"start_time": "2026-11-01T01:30:00-04:00", "end_time": "2026-11-01T01:30:00-05:00"})
    event = mapper.map([raw], "s", "kid", "child", Report())[0]
    assert event.end.timestamp() - event.start.timestamp() == 3600


def test_explicit_meal_description_preserves_known_foods_only():
    mapper = Mapper(
        "UTC",
        {
            "meal_descriptions": {
                "Breakfast combo": [{"name": "Banana"}, {"name": "Oatmeal", "amount": "some"}]
            }
        },
    )
    event = mapper.map(
        [record(kind="meal_activity", data={"desc": "Breakfast combo"})], "s", "kid", "child", Report()
    )[0]
    assert len(event.values["foods"]) == 2


def test_shared_source_type_selects_explicit_subtype_without_changing_identity():
    options = {
        "source_rules": {
            "school_health": [
                {
                    "kind": "temperature",
                    "when": {"data.type": "temperature"},
                    "fields": {"amount": "data.value", "units": "data.units"},
                },
                {"kind": "medicine", "when": {"data.type": "medicine"}, "fields": {"medicine": "data.name"}},
            ]
        }
    }
    mapper, report = Mapper("UTC", options), Report()
    rows = [record("same", "school_health", {"type": "temperature", "value": 37, "units": "C"})]
    temp = mapper.map(rows, "s", "kid", "child", report)[0]
    med = mapper.map(
        [record("same", "school_health", {"type": "medicine", "name": "Vitamin"})],
        "s",
        "kid",
        "child",
        Report(),
    )[0]
    assert temp.kind == "temperature" and med.kind == "medicine"
    assert temp.source_key == med.source_key
    assert not mapper.map(
        [record("unknown", "school_health", {"type": "other"})], "s", "kid", "child", report
    )
    assert report.counts["skipped"] == 1
