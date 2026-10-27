# Architecture (2.0.0)

## Components and request flow

`core.py` owns SQLite access, validation, task transitions, approval/completion
gates, query views, import/export, human actions. `cli.py`
parses `backlogr` commands. Without `BACKLOGR_URL` it calls core directly; with
that URL it reads `BACKLOGR_TOKEN_FILE` and sends authenticated JSON POSTs to
`/api/<operation>`. `doctor` checks local schema/writability or remote health
and an authenticated list request. `--version` prints the code version.

`server.py` exposes the same core operations through FastMCP tools:
`task_create`, `task_update`, `task_add_link`, `task_get`, `task_list`,
`task_flags`, `task_act`, `task_waiting`, `task_stale`, and `task_import`.
Each tool opens and closes a SQLite connection. HTTP mode runs Uvicorn listeners
on `BACKLOGR_HOSTS`/`BACKLOGR_PORT`, serving stateless streamable HTTP at `/mcp`,
the CLI JSON API, `/healthz`. HTTP mode requires a bearer
token; health is unauthenticated. The API restricts
fields to the corresponding MCP signature and bodies to 1 MiB. `export_data`
is an API-only operation used by remote CLI export.

`backlogr-server --stdio` instead runs MCP over process input/output, without
HTTP authentication or the nightly thread. HTTP mode checks approximately every
five minutes for local hour 02:00, then exports JSON and, when configured, creates
a SQLite backup once that day. See [configuration](CONFIGURATION.md) and
[security](../SECURITY.md) for transport boundaries and retention.

## Storage and schema

`core.connect()` creates parent directories, opens SQLite with WAL, a 15-second
busy timeout, and foreign keys enabled, creates missing schema objects, and adds
missing task columns. `PRAGMA user_version` advances to schema version 3 after additive
setup; version 3 also rewrites the pre-2.0 stored values `waiting_on_sean` and `via_chukar`
to `waiting_on_owner` and `via_coordinator`. This is distinct from application version 2.0.0. `doctor` checks it.

| Table | Contents |
|---|---|
| `tasks` | `id` primary key (`T-` plus six hex digits), title, done_means, owner_bot, due, status, source, approval_needed, created_at, updated_at |
| `updates` | autoincrement seq, task_id foreign key, ts, actor, note; triggers reject UPDATE and DELETE |
| `links` | autoincrement seq, task_id foreign key, ts, ref, kind; attached evidence references |
| `rules` | user-supplied text ID, title, required rule, test, evidence, source, testable, category, applies_to, nullable playbook_id, created_at, updated_at, created_by |
| `playbooks` | user-supplied text ID, name, skill, test, description, created_at, updated_at |
| `playbook_rules` | playbook_id and rule_id foreign keys; composite primary key |

Additive task columns are `ask`, `waiting_since`, `approved_by`, `approved_at`,
`approval_source`, `evidence`, `test_results` (JSON), `key`, `open_question`, and
`last_act` (JSON undo snapshot). Indexes cover status and owner; a partial unique
index enforces non-null idempotency keys. Repeated creation with the same key
returns the existing task. Similar open titles produce warnings.

Statuses are `todo`, `in_progress`, `waiting_on_owner`, `blocked`, `done`, and
`declined`; sources are `direct_to_bot`, `via_coordinator`, and `bot_initiated`.
Legacy names remain part of the public contract regardless of the configured
owner name. Waiting requires an exact ask; approval-needed work needs a recorded
human approval before starting or finishing; done needs evidence and per-test
results when its completion criteria mention tests. Human `act done` attestation
has configured exceptions. Changes append history within transactional writes.
`act undo` restores the saved fields only if no intervening task change occurred.

Normal imports apply creation gates; trusted local CLI `import --restore`
preserves snapshots verbatim and bypasses them. Export writes JSON through a
temporary file and atomic replacement. SQLite backups are the primary recovery
mechanism; see [migration](../MIGRATION.md).

## Rules and playbooks

Core exposes `rule_list`, `rule_get`, `rule_add`, `rule_untested`, `rule_remove`,
`playbook_list`, `playbook_get`, `playbook_add`, `playbook_remove`, and `rules_import`. MCP tools and authenticated
HTTP API operations have the same names. Rule writes and imports take an immediate
write lock; nested operations use savepoints, so records and memberships commit
or roll back together. Imports resolve playbooks before rules, then link rules,
allowing references within one file. See [Rules and Playbooks](RULES_PLAYBOOKS.md)
for fields, derivation, link semantics, and import counts.
