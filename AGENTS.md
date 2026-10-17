# Repository working agreements

## Changes and reviews

- Work in a branch or worktree; no branch naming convention is required. Never commit or push directly to `main`.
- Submit every repository change through a pull request. Open or update the PR and attach it to the current chat when supported. Return the PR link with the final handoff.
- Leave merging to the maintainer unless explicitly instructed to merge.
- Keep PRs focused. Describe the resulting behavior, validation performed, and any material limitations.

## Validation

- Use the locked dependencies and the checks documented in README.md:
  - `uv sync --locked`
  - `uv run --locked pytest`
  - `uv run --locked ruff check .`
  - `uv run --locked ruff format --check .`
  - `uv run --locked procareberry --help`
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
- Keep credentials, saved sessions, and private child data out of committed files, fixtures, logs, and PR descriptions. Preserve the distinction between private preview output and routine logs containing only counts and reasons.
- Update user documentation when commands, configuration, or sync behavior change.

## GitHub connectivity

- If Git or the GitHub CLI reports an authentication or connection error inside the sandbox, retry a read-only diagnostic with network access before concluding that credentials are invalid. Do not recommend re-authentication based only on a sandboxed failure.
