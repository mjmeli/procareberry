"""Credential-free runtime check, executable inside the production image."""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

# The workflow mounts tests read-only; no test dependencies are installed in the image.
from fakes import DB

from procareberry.huckleberry import HistoricalAPI, Huckleberry
from procareberry.models import Event, Report
from procareberry.service import Synchronizer
from procareberry.storage import Ledger, writer_lock


async def check(directory, mode):
    with writer_lock(directory):
        ledger = Ledger(directory / "smoke.sqlite3", "container-test")
        try:
            if mode == "read":
                assert ledger.get("smoke")["state"] == "owned"
                print("Container restart retained tracking state")
                return
            db = DB()
            timer = {"tummyTime": {"active": True, "paused": False, "uuid": "0123456789abcdef"}}
            db.write("activities/child", {"prefs": {}, "timer": timer})
            api = HistoricalAPI("synthetic", "synthetic", "UTC", None)

            async def client():
                return db

            api._get_firestore_client = client
            destination = Huckleberry(api, "UTC")
            event = Event(
                "smoke",
                "activity",
                "child",
                datetime.fromisoformat("2026-10-16T10:00:00+00:00"),
                values={"mode": "tummyTime", "duration": 60},
            )
            writes = db.write_count
            preview = Report()
            await Synchronizer(destination, ledger, write=False).sync(event, preview)
            assert preview.counts["preview_create"] == 1 and db.write_count == writes
            report = Report()
            await Synchronizer(destination, ledger, write=True).sync(event, report)
            assert report.counts["created"] == 1
            assert db.data["activities/child"]["timer"] == timer
            print("Container preview, import and timer checks passed")
        finally:
            ledger.close()


if __name__ == "__main__":
    asyncio.run(check(Path(sys.argv[1]), sys.argv[2]))
