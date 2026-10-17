# Procareberry

Parents who use Procare for daycare updates and Huckleberry for daily tracking otherwise have to enter the same activities in both apps. Procareberry automates that handoff by checking Procare's recorded activities and preparing matching entries in Huckleberry.

Procareberry can:

- **Automatic daycare sync:** Check every 15 minutes for recorded sleep, bottles, diapers, and solids.
- **Safe preview:** Review proposed entries before writing. Procareberry starts with writes disabled and checks for existing Huckleberry entries.
- **Daycare corrections:** Update entries when daycare changes an activity, while respecting your manual edits and deletions.
- **Recent history:** Import missed activities with a backfill covering up to the past 90 days.
- **Multiple children:** Pair children across your Procare and Huckleberry accounts.
- **Local or Docker setup:** Run on your own machine, with sync history kept locally for 90 days to prevent duplicate imports on ordinary reruns.

Review a preview against both apps before enabling writes. Procareberry never writes to Huckleberry until you opt in. Procare and Huckleberry access is unofficial and can change.

## AI disclosure

Procareberry was developed primarily with assistance from OpenAI Codex. I review and maintain the project. Automated tests use simulated account data; review a live preview with your own accounts before enabling writes.

## What gets synced

| Huckleberry record | Procare mapping | Requirements |
| --- | --- | --- |
| Sleep | Built-in `nap_activity` | Start and end times; uniquely pairable split records are supported |
| Bottle | Built-in `bottle_activity` | Recognized milk labels map automatically; amounts are ounces; blank volume stays blank |
| Diaper | Built-in `bathroom_activity` | Explicit diaper type and recognized wet/BM/both/dry subtype |
| Solids | Built-in `meal_activity` | Food description must match exactly one existing catalog item |

Unknown types and missing/ambiguous values are reported without stopping other events. Free-text meals are not split or interpreted automatically. Potty, health, nursing, pumping, and general activity records are not supported. Attendance, media, messages, and administrative records are not imported.

## Docker quick start

1. Copy `.env.example` to `.env` and fill in both accounts' email/password credentials.
2. List child IDs if either account has multiple children:

   ```sh
   docker compose run --rm procareberry children
   ```

   Set `CHILD_UID_MAP` to a JSON object pairing Procare IDs with Huckleberry IDs. Multiple pairs are supported. With exactly one child in each account, selection is automatic.

3. Review the last seven days without writing:

   ```sh
   docker compose run --rm procareberry once --dry-run
   ```

   The explicit CLI preview includes proposed times and record contents. Treat its terminal output as private. Routine service logs contain only counts and reasons.

4. After checking the preview against both apps, set `WRITE_ENABLED=true` in `.env` and start:

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

## Automatic activity matching

Bottle descriptions matching Huckleberry labels (`Breast Milk`, `Formula`, `Tube Feeding`, `Cow Milk`, `Goat Milk`, `Soy Milk`, or `Other`) map automatically, ignoring capitalization and surrounding whitespace. Missing or unrecognized descriptions are skipped.

Each meal description is matched as a single food name against Huckleberry's existing curated and custom catalogs, ignoring capitalization and surrounding whitespace. A meal is imported when exactly one catalog item matches. Unknown or ambiguous names are skipped, and the recorded quantity is preserved in notes.

All events require a stable source `id` and `activity_time`. Source comments are preserved. Ambiguous local timestamps during a daylight-saving transition require a source UTC offset.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROCARE_EMAIL`, `PROCARE_PASSWORD` | Required | Parent email/password authentication |
| `HUCKLEBERRY_EMAIL`, `HUCKLEBERRY_PASSWORD` | Required | Huckleberry authentication |
| `WRITE_ENABLED` | `false` | Permit destination writes |
| `TZ` | `America/New_York` | Date boundaries and historical offsets |
| `DATA_DIR` | `/data` | Tokens, SQLite ledger, current status and rotated logs |
| `CHILD_UID_MAP` | `{}` | Explicit source/destination child pairing |
| `SYNC_CATEGORIES` | All supported categories | Comma-separated allowlist |
| `INTERVAL_MINUTES` | `15` | Poll interval, minimum 5 minutes |
| `HEALTH_MAX_AGE_HOURS` | `36` | Maximum age of a successful poll |

Procare verification-code and SSO login flows are not implemented. A missing token or verification response produces a clear error. The account's default API site is discovered automatically; ambiguous site selection is reported.

## Corrections, duplicates and storage

### Duplicates and corrections

- Procareberry tracks each Procare activity separately, so two feeds or diapers close together stay separate.
- Matching entries already in Huckleberry are skipped and left alone. Sleep records that overlap existing sleep are skipped for review.
- Daycare corrections carry over only to entries Procareberry created that you have not edited. If you edit or delete an entry in Huckleberry, Procareberry leaves it alone for the rest of the 90-day tracking period.
- Removing an activity from Procare never deletes it from Huckleberry. Changes to the activity category or child pairing, and corrections that cannot be matched confidently, are flagged for review.

### Interrupted imports

If a connection drops during an import, Procareberry checks Huckleberry on the next run to see whether the entry was saved. If it cannot tell for sure, it marks the import as pending to avoid creating a duplicate.

Review pending imports manually, especially if you edited or deleted the entry before Procareberry could confirm it. Running another backfill will not force a pending import through.

### Storage and backups

Procareberry keeps **90 days of sync history** and automatically removes older tracking data. Your Huckleberry entries stay in place. Backfills are limited to the same 90-day window.

- `/data` contains private child information used to track changes, along with account tokens and logs. Media and a full copy of the Procare feed are not stored.
- Logs are limited to about 1 MB each, with two older copies kept.
- Stop the service before backing up the entire `/data` directory. Restore it with the matching account configuration.
- Keep the tracking data when troubleshooting. Deleting it removes the protection for entries you have edited or deleted. If saved data cannot be read, Procareberry reports the problem instead of resetting it.

Run only one syncing instance per data directory; Procareberry blocks a second one. Separate installations do not coordinate, so avoid running them against the same child.

## Development and releases

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked procareberry --help
```

Container images are published to `ghcr.io/mjmeli/procareberry`: pushes to `main` update `latest`, and version tags such as `v1.2.3` publish `1.2.3`. Images also receive a `sha-` tag identifying the source commit.
