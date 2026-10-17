# Repository working agreements

## Changes and reviews

- Work in a branch or worktree; no branch naming convention is required. Never commit or push directly to `main`.
- Submit every repository change through a pull request. Open or update the PR and attach it to the current chat when supported. Return the PR link with the final handoff.
- Leave merging to the maintainer unless explicitly instructed to merge.
- Keep PRs focused. Describe the resulting behavior, validation performed, and any material limitations.

## Validation

- Before pushing changes or opening/updating a PR, read `.github/workflows/build-and-publish.yml` and run its Python validation steps against the final changes using locked dependencies:
  - `uv sync --locked`
  - `uv run --locked pytest`
  - `uv run --locked ruff check .`
  - `uv run --locked ruff format --check .`
  - `uv run --locked procareberry --help`
- Run `git diff --check` as well. Lint and formatting are separate checks: passing `ruff check` does not verify formatting. Fix failures and rerun the checks before pushing. If a check cannot run, identify it and explain why rather than claiming it passed.
- Add regression coverage for changed sync behavior, particularly duplicate prevention, corrections, interrupted imports, and manual edits or deletions.
- Let CI build and smoke-test the Docker image for each PR. Agents do not need to wait for the Docker image build or publication before handing off. Report CI as pending, passed, failed, or unverified based on observed results; do not claim pending checks passed.

## Branch images for PR testing

- Image publishing follows Nanitberry's convention: pushes to every branch publish a sanitized branch-name tag and a commit-specific `sha-` tag to `ghcr.io/mjmeli/procareberry`. Docker Hub receives the same tags when configured.
- Pull request events build and smoke-test images without publishing. A PR's test image comes from its source branch push. Fork branches do not publish to this repository's registry.
- The default branch updates `latest`; version tags such as `v1.2.3` publish `1.2.3`, `1.2`, and `latest`.
- When sharing an image for testing, include the image reference and pull command. Prefer the commit-specific SHA tag or digest to identify the tested revision, and disclose when publication is still pending.
- Treat `.github/workflows/build-and-publish.yml` as the executable source of truth. If these rules and the workflow differ, report the discrepancy rather than claiming an unavailable image exists.

## Sync behavior and private data

- Preserve writes-disabled defaults and dry-run behavior, duplicate prevention, and respect for manually edited or deleted destination entries.
- Use simulated account data in automated tests. Do not access live accounts or enable destination writes for testing without explicit authorization.
- Keep credentials, saved sessions, and private child data out of committed files, fixtures, logs, and PR descriptions. Preserve the distinction between private preview output and routine logs. Routine logs may include counts, reasons, and confirmed write summaries with category, event times, and a short hashed source reference; omit notes, record contents, and child IDs.
- Update user documentation when commands, configuration, or sync behavior change.

## README writing

- Write for someone discovering and using the project. Use plain language, short paragraphs, and a practical tone; avoid dense technical prose and unexplained jargon.
- Describe the current behavior directly. The README is a standalone reference, not a conversation, changelog, or account of implementation decisions. Remove stale instructions when behavior changes; avoid phrases such as "no mapping file is needed", "now supports", or "no longer uses" that narrate past changes.
- Focus on what the project does, how to configure and run it, and what readers need for safe use and troubleshooting. Explain what imports, what gets skipped, and how corrections affect their entries.
- Include implementation details only when they help readers take an action or understand a meaningful limitation. Omit internal activity names, source field requirements, nap-pairing mechanics, database terminology, authentication internals, dependency-pinning rationale, and GitHub Actions setup unless necessary for the documented task. Keep useful development commands and published image/version information.
- Use descriptive subsections, bullets, numbered steps, and tables where they improve scanning. Use bold lead-ins for feature bullets. Keep related guidance together and avoid repeating it across sections.
- Preserve guidance that affects privacy, writes, duplicate prevention, manual edits, backups, and recovery. Simplify its wording without losing the practical consequences.
- Keep the feature overview and AI disclosure accurate. Distinguish simulated automated tests from live-account validation; do not imply testing that has not happened.
- Before finishing a README edit, read the affected section in context, verify commands and behavior against the code or workflow, and remove conversational wording, obsolete guidance, repetition, and unnecessary internal details.

## GitHub connectivity

- If Git or the GitHub CLI reports an authentication or connection error inside the sandbox, retry a read-only diagnostic with network access before concluding that credentials are invalid. Do not recommend re-authentication based only on a sandboxed failure.
