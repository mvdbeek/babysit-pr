# Development checks

Use `uv run --locked tox` for full validation and `uv run --locked tox -e ENV` for targeted checks. Keep checks orchestrated through tox rather than invoking each tool manually.

Apply formatting and safe lint fixes with `uv run --locked tox -e fix`.

Tests must use isolated state directories and fake GitHub/agent executables. Browser tests run against temporary dashboard servers, never the user's live watcher.
