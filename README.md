# Procareberry

Procareberry imports recorded daycare activities from Procare into Huckleberry, keeping sleep, bottles, diapers, and solids together with your daily tracking.

Procareberry can:

- **Automatic daycare sync:** Check every 15 minutes for recorded sleep, bottles, diapers, and solids.
- **Safe preview:** Review proposed entries before writing. Procareberry starts with writes disabled and checks for existing Huckleberry entries.
- **Daycare corrections:** Update entries when daycare changes an activity, while respecting your manual edits and deletions.
- **Recent history:** Import missed activities with a backfill covering up to the past 90 days.
- **Multiple children:** Pair children across your Procare and Huckleberry accounts.
- **Local or Docker setup:** Run on your own machine, with sync history kept locally for 90 days to prevent duplicate imports on ordinary reruns.

Writes are disabled by default. Review a preview against both apps before enabling them. Procare and Huckleberry access is unofficial and can change.

## AI disclosure

Procareberry was developed primarily with assistance from OpenAI Codex. I review and maintain the project. Automated tests use simulated account data; review a live preview with your own accounts before enabling writes.

## What gets synced

| Activity | What gets imported |
| --- | --- |
| Sleep | Completed naps with recorded start and end times |
| Bottle | Feeds with a recognized milk type; amounts are ounces, and unspecified amounts stay blank |
| Diaper | Wet, dirty, both, or dry diaper changes |
| Solids | Meals whose food description matches exactly one existing Huckleberry food |

Unsupported or unclear activities are skipped and reported while other imports continue. Potty, health, nursing, pumping, and general activity records are not supported. Attendance, media, messages, and administrative records are not imported.

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

   The preview includes proposed times and record contents. Treat its terminal output as private. Routine logs show created, updated, and recovered entries with their category, event times, and a short source reference, plus sync totals, ignored counts by activity type, and reasons for skipped entries. Notes, record contents, and child IDs are omitted.

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

`once` and `backfill` default to seven calendar days including today. Date ranges are inclusive. `--dry-run` previews changes even when `WRITE_ENABLED=true`. Dates must be within the past 90 calendar days, including today.

`serve` checks immediately and then every 15 minutes. Each check revisits seven days. Use `backfill` to import missed activities from earlier dates within the retention window. `status` shows the last sync result, skipped activities, and imports needing review.

## Automatic activity matching

Bottles use Procare's structured `bottle_type` first. When it is blank, descriptions are checked for Huckleberry labels (`Breast Milk`, `Formula`, `Tube Feeding`, `Cow Milk`, `Goat Milk`, `Soy Milk`, or `Other`) and clear milk names within text. Recognition ignores capitalization and supports aliases such as EBM, expressed milk, cow's milk, and soya milk. `Other` requires an exact label. Unknown structured types, multiple milk types, and negated or ambiguous descriptions are skipped for review. Recorded ounce amounts are preserved; amounts are never inferred from prose.

Each meal description is matched as a single food name against Huckleberry's existing foods, ignoring capitalization and surrounding whitespace. A meal is imported when exactly one food matches. Unknown or ambiguous names are skipped, and the recorded quantity is preserved in notes.

Activity comments are copied into Huckleberry notes after removing attachment placeholders and nonprinting control characters. Spaces and line endings are normalized, and surrounding whitespace is trimmed. Meaningful line breaks are preserved, with repeated blank lines collapsed to one.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROCARE_EMAIL`, `PROCARE_PASSWORD` | Required | Parent email/password authentication |
| `HUCKLEBERRY_EMAIL`, `HUCKLEBERRY_PASSWORD` | Required | Huckleberry authentication |
| `WRITE_ENABLED` | `false` | Permit destination writes |
| `TZ` | `America/New_York` | Time zone for activity dates and times |
| `DATA_DIR` | `/data` | Account sessions, sync history, status, and logs |
| `CHILD_UID_MAP` | `{}` | Explicit source/destination child pairing |
| `SYNC_CATEGORIES` | All supported categories | Comma-separated allowlist |
| `INTERVAL_MINUTES` | `15` | Poll interval, minimum 5 minutes |
| `HEALTH_MAX_AGE_HOURS` | `36` | Maximum age of a successful poll |

Procareberry signs in with your Procare parent email and password. Accounts requiring a verification code or single sign-on are not supported.

## Corrections, duplicates and storage

### Duplicates and corrections

- Procareberry tracks each Procare activity separately, so two feeds or diapers close together stay separate.
- Matching entries already in Huckleberry are skipped and left alone. Sleep records that overlap existing sleep are skipped for review.
- Daycare corrections carry over only to entries Procareberry created that you have not edited. If you edit or delete an entry in Huckleberry, Procareberry leaves it alone for the rest of the 90-day tracking period.
- Removing an activity from Procare never deletes it from Huckleberry. Changes to the activity category or child pairing, and corrections that cannot be matched confidently, are flagged for review.

### Interrupted imports

If a connection drops during an import, Procareberry checks Huckleberry on the next run to see whether the entry was saved. If it cannot tell for sure, it marks the import as pending to avoid creating a duplicate.

Polling and backfills keep unresolved imports pending for manual review. Review these entries in Huckleberry, especially if you edited or deleted one before Procareberry could confirm it.

### Storage and backups

Procareberry keeps **90 days of sync history** and automatically removes older tracking data. Your Huckleberry entries stay in place. Backfills are limited to the same 90-day window.

- `/data` contains private child information used to track changes, along with saved account sessions and logs. Media and a full copy of the Procare feed are not stored.
- Older logs are automatically rotated to limit disk usage.
- Stop the service before backing up the entire `/data` directory. Restore it with the matching account configuration.
- Keep the tracking data when troubleshooting. Deleting it removes the protection for entries you have edited or deleted.

Run only one syncing instance per data directory; Procareberry blocks a second one. Separate installations do not coordinate, so avoid running them against the same child.

## Development and releases

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked procareberry --help
```

Container images are published to `ghcr.io/mjmeli/procareberry` on pushes to every branch. Each image receives a sanitized branch-name tag and a `sha-` tag identifying the source commit. Pushes to the default branch update `latest`. Version tags such as `v1.2.3` publish `1.2.3`, `1.2`, and `latest`. Docker Hub receives the same tags when its credentials are configured.

Pull request workflows build and smoke-test an image without publishing it. For testing a PR locally, pull the image published by the source branch push; fork branches do not publish to this repository's registry. Use the commit-specific `sha-` tag or image digest when testing a particular revision.
