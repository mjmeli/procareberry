# Working agreements

- If a GitHub CLI or Git command reports an authentication or connection error inside the sandbox, retry a read-only diagnostic with network access before concluding that credentials are invalid. Do not recommend re-authentication based only on a sandboxed failure.

# Validation before pushing

- Read `.github/workflows/build-and-publish.yml` and match its Python validation steps before pushing changes or opening a pull request. The workflow is the source of truth if these commands change.
- Run all of the following against the final changes:

  ```sh
  uv sync --locked
  uv run --locked ruff check .
  uv run --locked ruff format --check .
  uv run --locked pytest
  uv run --locked procareberry --help
  git diff --check
  ```

- Lint and formatting are separate checks; passing `ruff check` does not verify formatting. If formatting fails, apply `ruff format` to the affected files and rerun the checks.
- For changes to Docker packaging or container behavior, also run the applicable image build and runtime/state-persistence smoke checks from the workflow when Docker is available.
- If a required check cannot run, state which check was blocked and why. Do not claim that all checks passed.
