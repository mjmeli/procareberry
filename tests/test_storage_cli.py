import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from procareberry.config import Settings, check_window
from procareberry.models import SyncError
from procareberry.service import child_pairs, health
from procareberry.storage import Ledger, private_json, writer_lock


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


def test_example_mapping_is_valid_json_and_does_not_enable_unverified_rules():
    root = Path(__file__).parents[1]
    options = json.loads((root / "mappings.example.json").read_text())
    assert options["source_rules"] == {}
