# Contributing

Use an existing Backlogr task or create one before work; follow [AGENTS.md](AGENTS.md)
and the [agent skill](skills/backlogr/SKILL.md). Respect explicit restrictions on
accessing shared services. Never use a real database for development or tests.

## Development setup

Requires Python 3.11+ and Git. From a clone:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Run checks with disposable configuration in Bash:

```bash
DEV_CHECK_DIR="$(mktemp -d)"
env -i PATH="$PATH" HOME="$DEV_CHECK_DIR" TMPDIR="$DEV_CHECK_DIR" \
    BACKLOGR_DB="$DEV_CHECK_DIR/backlogr.db" \
    BACKLOGR_EXPORT="$DEV_CHECK_DIR/export.json" \
    .venv/bin/python -m pytest -q -p no:cacheprovider --basetemp "$DEV_CHECK_DIR/pytest" tests
```

Tests use temporary SQLite fixtures, mocked deployment commands, and loopback
HTTP/MCP servers. No deployed service is needed. See
[installation](INSTALL_FOR_AGENTS.md) for a disposable manual check.

## Style and compatibility

Follow the surrounding Python style; no formatter or Python linter is currently
configured. Keep changes focused and use clear names. Preserve the `task_*` MCP
verbs, CLI compatibility, persisted statuses and source values. New environment
variables use `BACKLOGR_` and must be documented in
[configuration](docs/CONFIGURATION.md). Schema changes must preserve existing
rows and append-only history; see [architecture](docs/ARCHITECTURE.md).

Use reserved example domains, dummy credentials, and generic paths in examples
and fixtures. Never add personal deployment details or real secrets. Update
relevant docs and [CHANGELOG.md](CHANGELOG.md) under Unreleased.

## Pull requests and CI

Create a topic branch and a focused PR describing the problem, resulting behavior,
and validation performed (including limitations). Link the task of record. Add
meaningful tests for behavior changes, and keep existing checks passing. Request
review and merge after review and required checks; do not publish a release as
part of an unrelated change. Report vulnerabilities privately using
[SECURITY.md](SECURITY.md), rather than a public PR or issue.

The [CI workflow](.github/workflows/ci.yml) runs on pushes to `main` and same-repository pull requests
on self-hosted Linux X64 runners. Fork PRs are skipped to protect those runners.
It uses the runner's `python3` (the package requires Python 3.11+). It installs the test extra and `shellcheck-py`, runs
ShellCheck on `scripts/*.sh` and `ensure.sh`, compiles `cli.py`, `core.py`, and
`server.py`, runs `python -m pytest -q tests`, and rejects legacy names and
private workspace paths (with the documented migration exception). CI does
not currently enforce a formatter or build a release package.
