"""CLI for container operation and local review."""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from .config import Settings, check_window
from .models import SyncError
from .service import connected, health, run_window
from .storage import read_json, writer_lock


def parser():
    root = argparse.ArgumentParser(
        description="Sync recorded Procare activities to Huckleberry; writes disabled by default"
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("children", help="List child IDs for explicit account mapping")
    for command in ("once", "backfill"):
        sub = commands.add_parser(command)
        sub.add_argument("--days", type=int, default=7)
        sub.add_argument("--from", dest="start", type=date.fromisoformat)
        sub.add_argument("--to", dest="end", type=date.fromisoformat)
        sub.add_argument("--dry-run", action="store_true", help="Disable writes even when WRITE_ENABLED=true")
    commands.add_parser("serve", help="Poll every configured interval")
    commands.add_parser("status")
    commands.add_parser("healthcheck")
    return root


def configure_logging(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handlers = [logging.StreamHandler()]
    handler = RotatingFileHandler(directory / "service.log", maxBytes=1_000_000, backupCount=2)
    os.chmod(directory / "service.log", 0o600)
    handlers.append(handler)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)
    # Third-party logs include event amounts, child IDs and HTTP error responses.
    for name in ("huckleberry_api", "google", "grpc", "aiohttp"):
        logger = logging.getLogger(name)
        logger.disabled = True
        logger.propagate = False
        logger.addHandler(logging.NullHandler())


def print_preview(event, action, payload):
    # Explicit CLI preview is intended for private human review, not routine service logs.
    print(
        json.dumps(
            {
                "action": action,
                "kind": event.kind,
                "source": event.source_key[:12],
                "start": event.start.isoformat(),
                "end": event.end.isoformat() if event.end else None,
                "entry": payload,
            },
            sort_keys=True,
        )
    )


async def list_children(settings):
    async def inspect(source, destination):
        kids = await source.children()
        return {
            "procare": [
                {
                    "id": str(k["id"]),
                    "name": " ".join(str(k.get(f, "")) for f in ("first_name", "last_name")).strip(),
                }
                for k in kids
            ],
            "huckleberry": await destination.children(),
        }

    return await connected(settings, inspect)


async def serve(settings):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    while not stop.is_set():
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        try:
            result = await run_window(settings, today - timedelta(days=6), today)
            logging.info(
                "Sync %s: %s",
                "complete" if result["ok"] else "needs attention",
                json.dumps(result["counts"], sort_keys=True),
            )
            for reason, count in result["reasons"].items():
                logging.info("%s (%d)", reason, count)
        except SyncError as exc:
            logging.error("%s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.interval_minutes * 60)
        except TimeoutError:
            pass


def main():
    args = parser().parse_args()
    os.umask(0o077)
    try:
        settings = Settings.from_env()
        if args.command == "status":
            print(json.dumps(read_json(settings.data_dir / "status.json"), indent=2))
            return
        if args.command == "healthcheck":
            sys.exit(0 if health(settings) else 1)
        if getattr(args, "dry_run", False):
            from dataclasses import replace

            settings = replace(settings, write_enabled=False)
        # Validate ranges before making any network calls or creating local state.
        if args.command in {"once", "backfill"}:
            today = datetime.now(ZoneInfo(settings.timezone)).date()
            if not 1 <= args.days <= 90:
                raise SyncError("--days must be between 1 and 90")
            end = args.end or today
            start = args.start or end - timedelta(days=args.days - 1)
            check_window(start, end, today)
        configure_logging(settings.data_dir)
        with writer_lock(settings.data_dir):
            if args.command == "children":
                print(json.dumps(asyncio.run(list_children(settings)), indent=2))
            elif args.command == "serve":
                asyncio.run(serve(settings))
            else:
                result = asyncio.run(run_window(settings, start, end, preview=print_preview))
                print(json.dumps(result, indent=2))
                if not result["ok"]:
                    sys.exit(1)
    except (SyncError, OSError) as exc:
        # OSError may contain a pathname, but never dump upstream response bodies.
        print(
            str(exc)
            if isinstance(exc, SyncError)
            else "Cannot access Procareberry state; check directory permissions",
            file=sys.stderr,
        )
        sys.exit(1)
