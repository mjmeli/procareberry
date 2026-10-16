# Procareberry

Sync recorded Procare daycare activities to Huckleberry. Procareberry runs locally or in Docker, previews changes by default, and tracks imports for 90 days so ordinary reruns do not duplicate records.

**Status:** initial release. Offline tests exercise the pinned Huckleberry client against a simulated database. A live Procare preview must be reviewed before enabling writes. Procare and Huckleberry access is unofficial and can change.

## What gets synced

| Huckleberry record | Procare mapping | Requirements |
| --- | --- | --- |
| Sleep | Built-in `nap_activity` | Start and end times; uniquely pairable split records are supported |
| Bottle | Built-in `bottle_activity` | Explicit description-to-milk-type configuration; feed amounts are ounces; blank volume stays blank |
| Diaper | Built-in `bathroom_activity` | Explicit diaper type and recognized wet/BM/both/dry subtype |
| Solids | Built-in `meal_activity` | Food description must exactly match one catalog item or an explicitly configured meal description |
| Potty | Explicit source rule | Mode and recorded potty outcome |
| Temperature | Explicit source rule | Value and C/F units |
| Medicine | Explicit source rule | Exact or configured existing medicine reference; units if an amount exists |
| Growth | Explicit source rule | Actual measurements and metric/imperial units |
| Nursing | Explicit source rule | Start, end and side; both side durations when supplied |
| Pumping | Explicit source rule | Total volume or both side volumes, with units |
| Activities | Explicit source rule | One supported activity mode; duration is optional |

The less common Procare payload formats have not been verified against a live account. Their destination support is implemented and tested, but **no guessed source mappings are enabled**. `source_rules` lets you explicitly describe the fields your school returns. Unknown types and missing/ambiguous values are reported without stopping other events. Free-text meals are not split or interpreted automatically. Attendance, media, messages and administrative records are not imported.

No live timer is started or stopped. Completed activity imports specifically preserve active/paused Huckleberry activity timers.

## Docker quick start

1. Copy `.env.example` to `.env` and fill in both accounts' email/password credentials.
2. Create `config/`, copy `mappings.example.json` to `config/mappings.json`, and edit it for your daycare. The sample milk descriptions and custom rules are examples, not assertions about your school's data.
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

`MAPPING_FILE` points at a JSON file. Built-in bottle mappings use `bottle_types` to map the exact Procare description to a Huckleberry milk type. `meal_descriptions` can map an exact meal description to a list of actual foods; optional portions must come from known data or explicit configuration.

`foods` selects a specific existing food ID and catalog (`curated` or `custom`) when names are ambiguous. `medicines` selects an existing child-specific medicine ID. No food/medicine catalog items are created by the sync, including in write mode.

For an additional source type, define a `source_rules` entry with:

- `kind`: one of `sleep`, `bottle`, `solids`, `diaper`, `potty`, `temperature`, `medicine`, `growth`, `nursing`, `pump`, `activity`.
- `fields`: destination input names mapped to dot-separated paths in the original Procare record.
- `values`: optional explicit value translations per field. Unknown values are skipped.
- `constants`: explicit configuration for information you have verified, never guessed defaults.
- `when`: optional exact field-value conditions. A source type can contain a list of rules for different subtypes; exactly one must match, so a shared health activity type can map temperature and medicine records separately.

All events require a stable source `id` and `activity_time`. Use `start_time`/`end_time` field mappings when the source has different interval boundaries. Source comments are included by built-in mappings; explicit rules should map `notes` where supported. Ambiguous local timestamps during a daylight-saving transition require a source UTC offset.

See `mappings.example.json` for parameter names and synthetic examples of every additional category. Keep `source_rules` empty until you have checked those field paths against your school's actual payload. Do not post raw payloads containing child data in public issues.

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

Direct dependencies are pinned and `uv.lock` locks transitive versions. The activity wrapper deliberately uses the tested internal helper in `huckleberry-api==0.4.8`; dependency upgrades must run the compatibility tests. No library fork or upstream change is required.

GitHub Actions validates code and builds containers for pull requests without publishing. Pushes to `main` and `v*` tags publish GHCR images. Optional Docker Hub publishing requires `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` secrets. CI never needs real account credentials.
