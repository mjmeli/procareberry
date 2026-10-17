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
    events = Mapper("America/New_York").map(records, "school", "kid", "child", report)
    assert {e.kind for e in events} == {"sleep", "bottle", "diaper", "solids"}
    assert next(e for e in events if e.kind == "bottle").values["amount"] == 4
    assert next(e for e in events if e.kind == "diaper").values["mode"] == "both"
    assert report.counts["ignored"] == report.counts["skipped"] == 1


def test_missing_milk_type_not_guessed():
    report = Report()
    events = Mapper("UTC").map(
        [record(kind="bottle_activity", data={"amount": "5", "desc": "unknown"})], "s", "kid", "child", report
    )
    assert not events and "unrecognized bottle description" in next(iter(report.reasons))


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
    mapper, report = Mapper("UTC"), Report()
    bad = record(kind="bottle_activity", data={"amount": "NaN", "desc": "Formula"})
    no_id = record()
    del no_id["id"]
    assert not mapper.map([bad, no_id], "s", "kid", "child", report)
    assert report.counts["skipped"] == 2


def test_dst_ambiguous_local_times_rejected_and_offset_times_preserved():
    mapper = Mapper("America/New_York")
    for value in ("2026-11-01T01:30:00", "2026-03-08T02:30:00"):
        with pytest.raises(MappingError):
            timestamp(value, mapper.tz)
    raw = record(data={"start_time": "2026-11-01T01:30:00-04:00", "end_time": "2026-11-01T01:30:00-05:00"})
    event = mapper.map([raw], "s", "kid", "child", Report())[0]
    assert event.end.timestamp() - event.start.timestamp() == 3600


@pytest.mark.parametrize(
    "kind", ["potty", "temperature", "medicine", "growth", "nursing", "pump", "activity"]
)
def test_unsupported_types_do_not_block_diapers(kind):
    report = Report()
    events = Mapper("UTC").map(
        [
            record("unsupported", kind + "_activity", {"value": 37}),
            record("diaper", "bathroom_activity", {"type": "Diaper", "sub_type": "Wet"}),
        ],
        "s",
        "kid",
        "child",
        report,
    )
    assert [event.kind for event in events] == ["diaper"]
    assert report.reasons["unsupported activity type"] == 1


def test_potty_is_not_imported_as_diaper():
    report = Report()
    assert not Mapper("UTC").map(
        [record(kind="bathroom_activity", data={"type": "Potty", "sub_type": "Wet"})],
        "s",
        "kid",
        "child",
        report,
    )
    assert report.reasons["potty records are not supported"] == 1


@pytest.mark.parametrize(
    "description,expected",
    [
        ("Formula", "Formula"),
        (" formula ", "Formula"),
        ("BREAST MILK", "Breast Milk"),
        ("Cow Milk", "Cow Milk"),
        ("Goat Milk", "Goat Milk"),
        ("Soy Milk", "Soy Milk"),
        ("Tube Feeding", "Tube Feeding"),
        ("Other", "Other"),
    ],
)
def test_bottle_labels_map_without_configuration(description, expected):
    events = Mapper("UTC").map(
        [record(kind="bottle_activity", data={"amount": 4, "desc": description})],
        "s",
        "kid",
        "child",
        Report(),
    )
    assert events[0].values["bottle_type"] == expected


@pytest.mark.parametrize("description", [None, "", "Milk", "Formula and breast milk", {"invalid": True}])
def test_ambiguous_bottle_labels_are_not_guessed(description):
    report = Report()
    assert not Mapper("UTC").map(
        [record(kind="bottle_activity", data={"desc": description})], "s", "kid", "child", report
    )
    assert report.counts["skipped"] == 1


def test_meal_description_is_preserved_without_inference():
    events = Mapper("UTC").map(
        [record(kind="meal_activity", data={"desc": "Banana and oatmeal", "quantity": "Most"})],
        "s",
        "kid",
        "child",
        Report(),
    )
    assert events[0].values == {
        "foods": [{"name": "Banana and oatmeal"}],
        "notes": "Most",
    }


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"bottle_type": "Breast Milk", "desc": ""}, "Breast Milk"),
        ({"bottle_type": "Formula", "desc": "breast milk"}, "Formula"),
        ({"bottle_type": "  ", "desc": "Had 3 ounces of breast milk"}, "Breast Milk"),
        ({"desc": "Had 3 ounces of breast milk"}, "Breast Milk"),
        ({"desc": "4 oz of FORMULA consumed"}, "Formula"),
        ({"desc": "Expressed milk, 3 oz"}, "Breast Milk"),
        ({"desc": "EBM bottle"}, "Breast Milk"),
        ({"desc": "breast-milk"}, "Breast Milk"),
        ({"desc": "cow’s milk"}, "Cow Milk"),
        ({"desc": "whole milk bottle"}, "Cow Milk"),
        ({"desc": "goat's milk"}, "Goat Milk"),
        ({"desc": "soya milk"}, "Soy Milk"),
        ({"desc": "Tube feeding completed"}, "Tube Feeding"),
        ({"bottle_type": "EBM"}, "Breast Milk"),
    ],
)
def test_bottle_structured_types_and_text(data, expected):
    events = Mapper("UTC").map(
        [record(kind="bottle_activity", data={"amount": "3", **data})],
        "s",
        "kid",
        "child",
        Report(),
    )
    assert events[0].values["bottle_type"] == expected
    assert events[0].values["amount"] == 3


@pytest.mark.parametrize(
    "data",
    [
        {"bottle_type": "unknown", "desc": "Formula"},
        {"bottle_type": {"invalid": True}, "desc": "Formula"},
        {"desc": "Formula mixed with breast milk"},
        {"desc": "No formula consumed"},
        {"desc": "Breast milk or formula"},
        {"desc": "Refused breast milk"},
        {"desc": "Other child had a bottle"},
        {"desc": "Milk bottle"},
        {"desc": "Reformulated drink"},
    ],
)
def test_bottle_detection_does_not_guess(data):
    report = Report()
    assert not Mapper("UTC").map([record(kind="bottle_activity", data=data)], "s", "kid", "child", report)
    assert report.counts == {"skipped": 1}


def test_learning_activities_are_ignored_without_skip_reason():
    report = Report()
    assert not Mapper("UTC").map([record(kind="learning_activity")], "s", "kid", "child", report)
    assert report.counts == {"ignored": 1}
    assert report.reasons == {}


@pytest.mark.parametrize(
    "comment,expected",
    [
        ("\n\n\ufffc", ""),
        ("Mrs Janay ", "Mrs Janay"),
        (" \r\n First line \r\n\r\n\r\n Second line \r\n", "First line\n\nSecond line"),
        ("Finished \ufffc bottle 🍼 — café", "Finished bottle 🍼 — café"),
    ],
)
@pytest.mark.parametrize(
    "kind,data",
    [
        ("bathroom_activity", {"type": "Diaper", "sub_type": "Wet"}),
        ("bottle_activity", {"desc": "Formula", "amount": "3"}),
        (
            "nap_activity",
            {"start_time": "2026-10-16T10:00:00-04:00", "end_time": "2026-10-16T11:00:00-04:00"},
        ),
        ("meal_activity", {"desc": "Banana"}),
    ],
)
def test_activity_notes_are_sanitized(comment, expected, kind, data):
    raw = record(kind=kind, data=data)
    raw["comment"] = comment
    events = Mapper("UTC").map([raw], "s", "kid", "child", Report())
    assert events[0].values["notes"] == expected


def test_split_nap_uses_end_comment_when_start_only_has_placeholders():
    start = record("start", data={"start_time": "2026-10-16T10:00:00-04:00"})
    start["comment"] = "\n\n\ufffc"
    end = record("end", data={"end_time": "2026-10-16T11:00:00-04:00"})
    end["comment"] = " Slept well \ufffc\n"
    events = Mapper("UTC").map([start, end], "s", "kid", "child", Report())
    assert events[0].values["notes"] == "Slept well"


def test_meal_note_parts_are_cleaned_before_joining():
    raw = record(kind="meal_activity", data={"desc": "Banana", "type": " Snack \n", "quantity": "\ufffc\n"})
    raw["comment"] = " Ate well \n"
    events = Mapper("UTC").map([raw], "s", "kid", "child", Report())
    assert events[0].values["notes"] == "Snack; Ate well"


@pytest.mark.parametrize(
    "comment,expected",
    [
        ("\ufeffAte\u00a0well\u202f today\t!", "Ate well today !"),
        ("A\x00te\x07 well\x1b", "Ate well"),
        ("First\u2028Second\u2029Third", "First\nSecond\nThird"),
        ("Family 👨\u200d👩\u200d👧 ❤️ café", "Family 👨\u200d👩\u200d👧 ❤️ café"),
        ("Words\u200bhere می\u200cروم", "Words\u200bhere می\u200cروم"),
        (r"Literal \n and \ufffc", r"Literal \n and \ufffc"),
    ],
)
def test_note_unicode_cleanup_preserves_meaning(comment, expected):
    from procareberry.mapping import sanitize_notes

    assert sanitize_notes(comment) == expected
    assert sanitize_notes(sanitize_notes(comment)) == expected
