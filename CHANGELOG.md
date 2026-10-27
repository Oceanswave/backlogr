# Changelog

All notable changes. Versions follow semantic versioning.

## 2.2.0

- The code is now the `backlogr` package (`backlogr.core`, `backlogr.cli`, `backlogr.server`, `backlogr.dashboard`) instead of top-level `core`/`cli`/`server`/`dashboard` modules that could collide with other installed packages. The `backlogr` and `backlogr-server` commands are unchanged; `python -m backlogr` runs the CLI. The systemd unit now runs `backlogr-server`; a root `server.py` launcher keeps older units working until 3.0.

## 2.1.0

- Container image: `Dockerfile` (non-root, `/data` volume, health check), `compose.yaml` (loopback-only port), a CI job that builds and smoke-tests the image, and a release workflow that publishes `ghcr.io/oceanswave/backlogr` on `v*` tags.
- JSON exports (nightly and `backlogr export`) now include rules and playbooks in the `rules_import` format, so an export is a complete JSON backup. `backlogr import` (and `--restore`, `task_import`, API `import`) upserts them.
- Add `rule_remove` and `playbook_remove` across core, CLI (`rules remove`, `playbooks remove`), MCP and API. Removing a playbook keeps its rules; rules whose primary playbook it was are detached.
- Clear a due date with `backlogr update ID --due none` or `due=""` in `task_update`/API `update`; it is stored as null and logged as `due: <old> -> none`. Previously a blank due was stored as an empty string. Schema version 4 normalizes such rows on connect.

## 2.0.0

Prepares Backlogr for public release. See [MIGRATION.md](MIGRATION.md#v200) for upgrade steps.

**Breaking**

- Renamed deployment-specific identifiers. `waiting_on_sean` is now `waiting_on_owner`, `via_chukar` is now `via_coordinator`, `task_sean` is now `task_waiting`, and `backlogr sean` is now `backlogr waiting`.
  - The old names are still accepted as input.
  - Schema version 3 rewrites stored values once.
  - The API op `sean` and the CLI command `sean` remain as aliases.
- The dashboard is generic:
  - Owners come from the data. Headings use `BACKLOGR_OWNER`.
  - Categories come from the optional `BACKLOGR_DASHBOARD_CATEGORIES` JSON file.
  - The time zone comes from `BACKLOGR_TZ`.
- `Tailscale-User-Login` no longer authorizes the dashboard unless `BACKLOGR_TRUST_TAILSCALE_HEADERS` is set.

**Fixes**

- People who used `act` were counted as bots, so the owner could no longer approve tasks. `BACKLOGR_OWNER` is now always treated as a human.
- `created_at` is validated. Previously, one malformed import broke `flags` for every caller.
- A non-UTF-8 `Authorization` header now returns 401 instead of a 500.
- SQLite errors now return a JSON 503.
- Exports and backups are written mode 600 through unique temp files. Exports read a single consistent transaction.
- Setting `BACKLOGR_BACKUP_KEEP=0` no longer keeps every backup.
- `backlogr update --link` applies field changes first, so a rejected update no longer leaves a stray link.
- `scripts/deploy.sh` strips quotes from `.env` values and refuses to run when the DB file is missing. Previously it reported an empty backup as a success.
- Dashboard: `trunc` no longer drops a character from text that has no spaces.

**Security**

- The dashboard now sends Content-Security-Policy, `nosniff` and `no-referrer` headers.
- The remote CLI refuses HTTP redirects. Without this, urllib would forward the bearer token to the redirect target.
- The remote CLI warns when it sends the token over plain HTTP to a non-loopback host.

**Internals**

- The version is defined once, in `core.__version__`.
- The HTTP API dispatches directly to the MCP tool functions, replacing a duplicate lambda table and reflection over `globals()`.
- Removed the `sys.path` hacks.
- CI runs on GitHub-hosted runners.

## 1.9.0

- Add a live, read-only `/dashboard` and `/dashboard.json`, reading tasks, rules, playbooks, and the last 48 hours of task history directly from SQLite on each request.
- Replicate the Bot Team Dashboard layout with inline dark-theme styling, bot glyphs, category cards, and a 60-second refresh.
- Accept the API bearer token or a nonempty Tailscale identity header for dashboard requests; enforce the Host allow-list and GET/HEAD only.

## 1.8.0

- Remove signed action links: the public confirmation/direct-submit endpoint, CLI `link`, MCP `task_link`, signing and nonce handling, and link-only configuration.
- Preserve task actions, evidence attachments, authenticated API/MCP, and health checks.
- No destructive schema migration: existing `used_links` tables and their data remain untouched and unused; new databases no longer create the table. Schema version remains 2.

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
