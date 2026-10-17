# Procareberry

Sync recorded Procare daycare activities to Huckleberry. Procareberry runs locally or in Docker, previews changes by default, and tracks imports for 90 days so ordinary reruns do not duplicate records.

**Status:** initial release. Offline tests exercise the pinned Huckleberry client against a simulated database. A live Procare preview must be reviewed before enabling writes. Procare and Huckleberry access is unofficial and can change.

## What gets synced

| Huckleberry record | Procare mapping | Requirements |
| --- | --- | --- |
| Sleep | Built-in `nap_activity` | Start and end times; uniquely pairable split records are supported |
| Bottle | Built-in `bottle_activity` | Recognized milk labels map automatically; custom descriptions need an override; amounts are ounces; blank volume stays blank |
| Diaper | Built-in `bathroom_activity` | Explicit diaper type and recognized wet/BM/both/dry subtype |
| Solids | Built-in `meal_activity` | Food description must exactly match one catalog item or an explicitly configured meal description |

Unknown types and missing/ambiguous values are reported without stopping other events. Free-text meals are not split or interpreted automatically. Potty, health, nursing, pumping, and general activity records are not supported. Attendance, media, messages, and administrative records are not imported.

## Docker quick start

1. Copy `.env.example` to `.env` and fill in both accounts' email/password credentials.
2. Mapping configuration is optional. For custom bottle descriptions or meal/food overrides, copy `mappings.example.json` to `config/mappings.json`, edit it, and set `MAPPING_FILE=/config/mappings.json` in `.env`.
3. List child IDs if either account has multiple children:

   ```sh
   docker compose run --rm procareberry children
   ```

   Set `CHILD_UID_MAP` to a JSON object pairing Procare IDs with Huckleberry IDs. Multiple pairs are supported. With exactly one child in each account, selection is automatic.

4. Review the last seven days without writing:

   ```sh
   docker compose run --rm procareberry once --dry-run
   ```

   The explicit CLI preview includes proposed times and record contents. Treat its terminal output as private. Routine service logs contain only counts and reasons.

5. After checking the preview against both apps, set `WRITE_ENABLED=true` in `.env` and start:

   ```sh
   docker compose up -d
   docker compose logs -f
   ```

Use `docker compose -f compose.yaml -f compose.local.yaml build` and the same two files for commands when building locally. Published releases use `ghcr.io/mjmeli/procareberry:latest`.

## Commands

```sh
procareberry children
procareberry once --dry-run
procareberry backfill --days 30 --dry-run
procareberry backfill --from 2026-10-01 --to 2026-10-10
procareberry serve
procareberry status
procareberry healthcheck
```

`once` and `backfill` default to seven calendar days including today. Date ranges are inclusive. `--dry-run` overrides `WRITE_ENABLED=true`. Future dates and dates before the current rolling 90-day window are rejected before network access.

`serve` checks immediately and then every 15 minutes. Each check revisits seven days. Longer outages need an explicit backfill within the retention window. Status includes skipped/unknown mappings and unresolved writes. A successful empty feed still counts as a healthy poll.

## Mapping configuration

`MAPPING_FILE` optionally points at a JSON file. Bottle descriptions matching Huckleberry labels (`Breast Milk`, `Formula`, `Tube Feeding`, `Cow Milk`, `Goat Milk`, `Soy Milk`, or `Other`) map automatically, ignoring capitalization and surrounding whitespace. Unknown or missing descriptions are skipped rather than assigned a milk type. Use `bottle_types` only to override an exact Procare description, such as a daycare-specific label. `meal_descriptions` can map an exact meal description to a list of actual foods; optional portions must come from known data or explicit configuration.

`foods` selects a specific existing food ID and catalog (`curated` or `custom`) when names are ambiguous. No food catalog items are created by the sync, including in write mode.

All events require a stable source `id` and `activity_time`. Source comments are preserved. Ambiguous local timestamps during a daylight-saving transition require a source UTC offset.

See `mappings.example.json` for the supported configuration. Do not post raw payloads containing child data in public issues.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROCARE_EMAIL`, `PROCARE_PASSWORD` | Required | Parent email/password authentication |
| `HUCKLEBERRY_EMAIL`, `HUCKLEBERRY_PASSWORD` | Required | Huckleberry authentication |
| `WRITE_ENABLED` | `false` | Permit destination writes |
| `TZ` | `America/New_York` | Date boundaries and historical offsets |
| `DATA_DIR` | `/data` | Tokens, SQLite ledger, current status and rotated logs |
| `MAPPING_FILE` | Unset | JSON mapping configuration |
| `CHILD_UID_MAP` | `{}` | Explicit source/destination child pairing |
| `SYNC_CATEGORIES` | All supported categories | Comma-separated allowlist |
| `INTERVAL_MINUTES` | `15` | Poll interval, minimum 5 minutes |
| `HEALTH_MAX_AGE_HOURS` | `36` | Maximum age of a successful poll |

Procare verification-code and SSO login flows are not implemented. A missing token or verification response produces a clear error. The account's default API site is discovered automatically; ambiguous site selection is reported.

## Corrections, duplicates and storage

Tracking uses stable Procare activity identities, not rounded timestamps. Distinct feeds/diapers remain distinct. Equivalent pre-existing entries are skipped without taking ownership. Existing sleep overlaps block imports rather than being trimmed or merged.

Daycare corrections update only untouched entries created by Procareberry. A manual edit or deletion releases ownership for the remainder of the retained window. Source disappearance never deletes destination history. Changes in category, child mapping, or an ambiguous latest-event correction are reported for review.

Import intent is saved before logging. When a request is interrupted, the next run checks for an unambiguous new matching destination entry. An unresolved result remains pending instead of being blindly retried. If a pending entry was deleted or edited before it could be identified, review it manually; repeated backfills intentionally do not bypass that protection.

**Storage is bounded to 90 days of event tracking.** Older rows are pruned and SQLite is compacted weekly. Huckleberry entries remain. Backfills outside the retained window are rejected. Logs rotate at about 1 MB each with two backups. No media or full source-feed archive is retained. Tracking includes the last destination contents, so `/data` still contains private child information.

Run one writer per data directory; a file lock rejects a second process. Independent installations targeting the same child are not coordinated. Back up the complete `/data` directory while the service is stopped, and restore it together with the matching account configuration. Do not delete/reset the ledger to resolve an uncertain import: doing so loses edit/deletion protection. Incompatible or unreadable state is reported rather than silently discarded.

## Development and releases

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked procareberry --help
```

Direct dependencies are pinned and `uv.lock` locks transitive versions. The Huckleberry wrapper is tested against `huckleberry-api==0.4.8`; dependency upgrades must run the compatibility tests. No library fork or upstream change is required.

GitHub Actions validates code and builds containers for pull requests without publishing. Pushes to `main` and `v*` tags publish GHCR images. Optional Docker Hub publishing requires `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` secrets. CI never needs real account credentials.
