import asyncio
import signal
import subprocess
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from procareberry.config import Settings, check_window
from procareberry.models import SyncError
from procareberry.service import child_pairs, health
from procareberry.storage import Ledger, private_json, writer_lock


async def test_serve_logs_ignored_type_totals_once(monkeypatch, caplog, tmp_path):
    from procareberry import cli

    caplog.set_level("INFO")
    handlers = {}
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", handlers.__setitem__)

    async def run_window(*args):
        handlers[signal.SIGTERM]()
        return {
            "ok": True,
            "counts": {"created": 6, "ignored": 11},
            "ignored_types": {"sign_in_activity": 1, "photo_activity": 10},
            "reasons": {},
            "from": "2026-10-10",
            "to": "2026-10-16",
            "diagnostics": [],
        }

    monkeypatch.setattr(cli, "run_window", run_window)
    await cli.serve(Settings(data_dir=tmp_path))
    assert caplog.messages == [
        'Sync complete [2026-10-10 to 2026-10-16]: {"created": 6, "ignored": 11}',
        'Ignored by type: {"photo_activity": 10, "sign_in_activity": 1}',
    ]


def test_retention_and_binding_survive_restart(tmp_path):
    path, today = tmp_path / "state.sqlite3", date(2026, 10, 16)
    ledger = Ledger(path, "account-a")
    ledger.put("old", (today - timedelta(days=90)).isoformat(), "released", "sig", {})
    ledger.put("edge", (today - timedelta(days=89)).isoformat(), "released", "sig", {})
    ledger.prune(today)
    ledger.close()
    restored = Ledger(path, "account-a")
    assert restored.get("old") is None and restored.get("edge")["state"] == "released"
    restored.close()
    with pytest.raises(SyncError, match="different accounts"):
        Ledger(path, "account-b")
    assert path.stat().st_mode & 0o777 == 0o600


def test_window_boundary_and_writer_lock(tmp_path):
    today = date(2026, 10, 16)
    check_window(today - timedelta(days=89), today, today)
    with pytest.raises(SyncError, match="90-day"):
        check_window(today - timedelta(days=90), today, today)
    with writer_lock(tmp_path):
        with pytest.raises(SyncError, match="Another"):
            with writer_lock(tmp_path):
                pass


def test_atomic_status_and_health(tmp_path):
    settings = Settings(data_dir=tmp_path)
    now = datetime.now(ZoneInfo(settings.timezone))
    private_json(tmp_path / "status.json", {"ok": True, "finished": now.isoformat()})
    assert health(settings, now=now)
    assert not health(settings, now=now + timedelta(hours=37))
    assert (tmp_path / "status.json").stat().st_mode & 0o777 == 0o600


def test_child_mapping_must_be_explicit_when_ambiguous():
    with pytest.raises(SyncError, match="multiple"):
        child_pairs([{"id": "a"}, {"id": "b"}], [{"id": "x"}], {})
    assert child_pairs([{"id": 1}], [{"id": "x"}], {}) == {"1": "x"}


def test_cli_help_and_old_backfill_need_no_credentials(tmp_path):
    import os

    env = {**os.environ, "DATA_DIR": str(tmp_path), "WRITE_ENABLED": "false"}
    help_result = subprocess.run(
        [sys.executable, "-m", "procareberry", "--help"], env=env, capture_output=True, text=True
    )
    assert help_result.returncode == 0 and "healthcheck" in help_result.stdout
    old = subprocess.run(
        [sys.executable, "-m", "procareberry", "backfill", "--days", "91"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert old.returncode == 1 and "90" in old.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "value,expected",
    [("", None), ("  ", None), (" breast MILK ", "Breast Milk"), ("Formula", "Formula"), ("Other", "Other")],
)
def test_optional_bottle_default_configuration(monkeypatch, value, expected):
    monkeypatch.setenv("DEFAULT_BOTTLE_TYPE", value)
    settings = Settings.from_env()
    assert settings.default_bottle_type == expected


def test_invalid_bottle_default_configuration(monkeypatch):
    monkeypatch.setenv("DEFAULT_BOTTLE_TYPE", "Milk")
    with pytest.raises(SyncError, match="Invalid DEFAULT_BOTTLE_TYPE"):
        Settings.from_env()


def test_each_poll_repeats_full_decision_details(caplog):
    from procareberry.cli import log_poll

    caplog.set_level("INFO")
    detail = {
        "result": "conflict",
        "kind": "sleep",
        "source": "a" * 12,
        "start": "2026-10-16T10:00:00-04:00",
        "end": "2026-10-16T11:00:00-04:00",
        "reason": "sleep overlaps existing Huckleberry history",
    }
    result = {
        "ok": True,
        "from": "2026-10-10",
        "to": "2026-10-16",
        "counts": {"conflict": 1},
        "ignored_types": {"photo_activity": 3},
        "diagnostics": [detail],
    }
    log_poll(result)
    first_poll = list(caplog.messages)
    assert first_poll[-1] == (
        "Conflict sleep entry: source=aaaaaaaaaaaa start=2026-10-16T10:00:00-04:00 "
        "end=2026-10-16T11:00:00-04:00; sleep overlaps existing Huckleberry history"
    )
    caplog.clear()
    log_poll(result)
    assert caplog.messages == first_poll


def test_malformed_activity_log_uses_unavailable_metadata(caplog):
    from procareberry.cli import log_poll
    from procareberry.mapping import Mapper
    from procareberry.models import Report

    caplog.set_level("INFO")
    report = Report()
    Mapper("UTC").map(
        [
            {
                "id": "private-id",
                "activity_type": "bottle_activity",
                "activity_time": "private invalid date",
                "comment": "private note",
            }
        ],
        "school",
        "private-child",
        "private-target",
        report,
    )
    log_poll(
        {
            "ok": True,
            "from": "2026-10-16",
            "to": "2026-10-16",
            "counts": report.counts,
            "diagnostics": report.diagnostics,
            "ignored_types": {},
        },
    )
    assert "source=unavailable start=unavailable" in caplog.text
    assert "private" not in caplog.text
