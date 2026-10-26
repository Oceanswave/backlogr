# Changelog

All notable changes. Versions follow semantic versioning.

## Unreleased

## 1.7.0

- Support signed GET action links with `go=1`, minimal escaped result pages, and idempotent repeat taps for completed done/N/A links; preserve confirmation forms without `go=1`.

## 1.6.0

- Add reusable signed reply links and reply actions with owner notifications, unchanged status, and undo support.

- Fix core, CLI, MCP, and API untested queries to return every rule with `testable=false`, including descriptive tests explicitly marked false on add or import; exclude `testable=true` rules.
- Document disposable agent installation, contribution/CI workflow, private security reporting, architecture/schema, and all environment variables; link guides from README and AGENTS.
- Align the agent skill with CLI/MCP behavior and replace personal deployment examples with generic placeholders while retaining compatibility identifiers and license attribution.

## 1.5.0

- Add Rules and Playbooks across core, CLI, MCP, and the authenticated HTTP API, with rule filters and embedded playbook rules.
- Add idempotent SQLite schema version 2 migration and many-to-many rule memberships, preserving existing tasks and history.
- Derive testability from test content, reject duplicate rule IDs without explicit replacement, and validate linked IDs.
- Add atomic bulk rules/playbooks import with created/updated/unchanged counts and generic example fixtures.
- Document CLI/MCP usage, import format, migration, and recovery; expand tests across all layers.

## 1.4.0

- Configure the owner name and default `act` actor with `BACKLOGR_OWNER` (default `owner`).
- Close SQLite connections after API requests and MCP calls, including failures.
- Restrict API arguments to MCP parameters and cap request bodies at 1 MiB.
- Roll back failed link additions and check task existence inside the transaction.
- Add optional list limits and offsets across core, CLI, MCP, and API.
- Restrict verbatim snapshot restoration to trusted local CLI `import --restore` (core `restore=True`); reject remote `--restore`. Normal imports, including API/MCP snapshots, enforce approval, evidence, and test gates; skip existing IDs.
- Report CLI file, JSON, and network failures without tracebacks.
- Add `backlogr doctor`, `backlogr --version`, and schema version checks.
- Keep `act`, existing statuses, `sean`, `task_sean`, and source values.

## 1.3.1

- `deploy/deploy.sh` calls pip through the venv Python, so a moved venv no longer breaks deploys.

## 1.3.0

- Signed single-use action links (`done`, `na`, `question`, `later`): `backlogr link`, MCP `task_link`, and a confirm page at `/act`. Links expire (default seven days), are rejected when tampered with (403), expired (410), or reused (409).
- `later` action: records a note without changing status.

## 1.2.0

- `act` (CLI), `task_act` (MCP), and API op `act` for one-call human actions; `undo` reverts the last act.
- `BACKLOGR_HUMANS` names people whose `act done` is an attestation.

## 1.1.0 and earlier

See `MIGRATION.md`.
