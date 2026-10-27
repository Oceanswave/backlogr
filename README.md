# Backlogr

A small, self-hosted system of record for a team of AI agents' work (research, purchases, edits, outreach, code). Agents talk to it over MCP or a CLI. Tasks are the objects; the MCP tool verbs are `task_*`. Rule: **no task, no work.**

- Python, SQLite (WAL), append-only update log enforced by triggers.
- MCP over streamable HTTP (or stdio), plus a JSON API used by the CLI. Bearer-token auth on API/MCP; `/healthz` is unauthenticated.
- Nightly JSON export and SQLite backup inside the service.

## Documentation

- [Agent installation: disposable clone, venv, and smoke check](INSTALL_FOR_AGENTS.md)
- [Contributing and CI](CONTRIBUTING.md)
- [Security policy and threat model](SECURITY.md)
- [Architecture and schema](docs/ARCHITECTURE.md)
- [Complete configuration reference](docs/CONFIGURATION.md)

## Rule for every agent (paste verbatim)
> **No task, no work: before you start anything for the user, create or find its task in Backlogr (`task_create` / `backlogr new`), and record every status change, blocker and result on that task (`task_update` / `backlogr update`, `backlogr done`) as it happens, using your bot name as the actor.**

## Run the server
```
git clone '<repo-url>' ~/backlogr && cd ~/backlogr
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
mkdir -p data backups exports && chmod 700 data backups exports
cp deploy/env.example .env && chmod 600 .env    # fill in BACKLOGR_TOKEN etc.
set -a && . ./.env && set +a && .venv/bin/backlogr-server
curl -fsS http://127.0.0.1:8796/healthz         # ok
```
As a systemd user service: `install -D -m 644 deploy/backlogr.service ~/.config/systemd/user/backlogr.service && systemctl --user daemon-reload && systemctl --user enable --now backlogr` (and `loginctl enable-linger $USER` to start at boot).

Expose it only on a private network or behind an authenticating reverse proxy, for example Tailscale serve under a path: `tailscale serve --bg --https=443 --set-path /backlogr http://127.0.0.1:8796`, then `BACKLOGR_URL=https://backlogr.example.invalid/backlogr`. Add that host name to `BACKLOGR_ALLOWED_HOSTS` for MCP transport checks; enforce Host restrictions for all routes at the proxy. Use TLS and authentication before any public exposure. Routes: `$BACKLOGR_URL/mcp`, `$BACKLOGR_URL/healthz`, `$BACKLOGR_URL/api/<op>`.

## Configuration

See the [complete reference](docs/CONFIGURATION.md) for all environment variables, including deployment settings.

| Variable | Used by | Default |
|---|---|---|
| `BACKLOGR_TOKEN` | server (required for HTTP) | none |
| `BACKLOGR_DB` | server, local CLI | `~/backlogr/data/backlogr.db` |
| `BACKLOGR_PORT` | server | `8796` |
| `BACKLOGR_HOSTS` | server bind IPs (comma list) | `127.0.0.1` |
| `BACKLOGR_ALLOWED_HOSTS` | extra MCP Host headers (proxy DNS names) | none |
| `BACKLOGR_BACKUP_DIR` / `BACKLOGR_BACKUP_KEEP` | nightly backup (`backlogr-YYYY-MM-DD.db`) | off / `14` |
| `BACKLOGR_EXPORT` | nightly export, CLI `export` | `~/backlogr/exports/backlogr-export.json` |
| `BACKLOGR_URL` | CLI remote mode (and client configs below) | unset = local DB |
| `BACKLOGR_TOKEN_FILE` | CLI remote mode | `~/backlogr/.token` |
| `BACKLOGR_OWNER` | name in messages and default person for `act` (server and CLI) | `owner` |
| `BACKLOGR_HUMANS` | people whose `act done` attestation can satisfy completion gates | unset |
| `BACKLOGR_BOTS` | bot names that can never be `approved_by` (DB owners/actors are included, except configured humans) | none |
| `BACKLOGR_APPROVERS` | if set, the only names accepted as `approved_by` | unset |
| `BACKLOGR_TZ`, `BACKLOGR_DASHBOARD_CATEGORIES`, `BACKLOGR_TRUST_TAILSCALE_HEADERS` | dashboard time zone, card grouping, Tailscale header auth | local zone / none / off |

## Install the MCP
Server name `backlogr`, streamable HTTP at `$BACKLOGR_URL/mcp`, header `Authorization: Bearer <token>`. Keep the token in an env var or a mode-600 file; never commit it or paste it into chat.

- **Claude Code** (project `.mcp.json` or `~/.claude.json`; Claude Code expands `${VAR}`):
  ```json
  {"mcpServers": {"backlogr": {"type": "http", "url": "${BACKLOGR_URL}/mcp",
    "headers": {"Authorization": "Bearer ${BACKLOGR_TOKEN}"}}}}
  ```
- **Codex** (`~/.codex/config.toml`, with `BACKLOGR_TOKEN` exported in the launching shell):
  ```toml
  [mcp_servers.backlogr]
  url = "https://<your-host>/backlogr/mcp"
  bearer_token_env_var = "BACKLOGR_TOKEN"
  ```
- **Cursor** (`~/.cursor/mcp.json`):
  ```json
  {"mcpServers": {"backlogr": {"url": "https://<your-host>/backlogr/mcp",
    "headers": {"Authorization": "Bearer ${env:BACKLOGR_TOKEN}"}}}}
  ```
- **Stdio-only clients** (runs on the server host, reads `.env` there):
  ```json
  {"mcpServers": {"backlogr": {"command": "ssh", "args": ["<user>@<server-host>",
    "cd backlogr && set -a && . ./.env && .venv/bin/python server.py --stdio"]}}}
  ```
- **Any other client:** streamable HTTP, the URL above, the bearer header.

## Install the skill
The agent skill is [`skills/backlogr/SKILL.md`](skills/backlogr/SKILL.md) (CLI, MCP tools, status flow, rules). Copy or symlink the folder into your agent's skills directory:
```
git clone '<repo-url>' ~/src/backlogr
ln -s ~/src/backlogr/skills/backlogr ~/.claude/skills/backlogr     # Claude Code
ln -s ~/src/backlogr/skills/backlogr ~/.codex/skills/backlogr      # Codex
```
Other agents: point them at `AGENTS.md`, or paste the rule above into their instructions.

## MCP tools
task_create, task_update (status/due/owner/approval_needed/note), task_add_link, task_get, task_list (owner, status or comma list, overdue, stale_hours, due_before, q, source, include_closed, limit, offset), task_flags (overdue, stale, blocked, waiting_on_owner, created_recently), task_stale (hours), task_waiting (waiting_on_owner queue with asks), task_import, task_act.
Fields: id, title, done_means, owner_bot, due (ISO), status (todo|in_progress|waiting_on_owner|blocked|done|declined), source (direct_to_bot|via_coordinator|bot_initiated), approval_needed, ask (required when moving to waiting_on_owner), waiting_since, created_at, updated_at, updates[], links[].

## CLI (`backlogr`)
Remote when `BACKLOGR_URL` is set (token from `BACKLOGR_TOKEN_FILE`), else the local DB (`BACKLOGR_DB`).
```
backlogr new "Order sample tee" --owner Mint --source via_coordinator --by Coordinator --done-means "tee arrives" --due 2026-10-30 --approval --key mint:sample-tee   # --key: retries return the same task
backlogr update T-abc123 --by Mint --status blocked --note "needs card"
backlogr update T-abc123 --by Mint --link https://example.com/order --kind order
backlogr done T-abc123 --by Mint --evidence "https://example.com/pr/12" --test unit=pass --test e2e=pass   # --evidence required; --test when done_means mentions tests
backlogr list [--owner Mint] [--status todo,in_progress] [--q PR] [--source via_coordinator] [--due-before 2026-11-01] [--overdue] [--stale-hours 24] [--all] [--limit 50] [--offset 0]
backlogr flags [--stale 24] [--recent 24]
backlogr waiting                    # waiting_on_owner queue: exact ask + age, oldest first
backlogr approve T-id --by Forge --approver Operator --source "Operator in chat 11:17"   # human approval record
backlogr stale [--hours 2]          # in_progress with no note in N hours, by owner
backlogr get T-abc123 [--short]      backlogr import file.json --by Coordinator      backlogr export [--path P]
```
`new`, `update` and `done` print one line (`T-abc123 in_progress`); add `--json` (before or after the subcommand) for the full row. Other commands print a readable summary or JSON with `--json`.

## Deploying to a host with deploy.sh
`scripts/deploy.sh` updates an existing install (a git clone with a `.venv` and a systemd user unit, as in Run the server) on a host that has no GitHub access. Ship the release as a git bundle:
```
git bundle create /tmp/backlogr.bundle --all --tags
scp /tmp/backlogr.bundle "$DEPLOY_HOST:/tmp/backlogr.bundle"
# on the host, from the install dir:
scripts/deploy.sh --bundle /tmp/backlogr.bundle --ref vX.Y.Z --dry-run   # preview: fetches refs, changes no files, DB or service
scripts/deploy.sh --bundle /tmp/backlogr.bundle --ref vX.Y.Z
```
It refuses to run (exit 4) if tracked files in the install are modified. Otherwise it backs up the DB to `backups/pre-deploy-<ts>.db`, checks out the ref, runs `pip install -e .`, restarts the service and waits for `/healthz` (5 s per try, 40 tries). If pip install, the restart or healthz fails, it rolls the code back to the previous commit and exits 1; schema changes are not reverted, so the DB backup is kept. Without `--ref` it deploys the highest `v*` tag; without `--bundle` it fetches from `origin`.

| Env | Default |
|---|---|
| `BACKLOGR_HOME` | `~/backlogr` (install dir) |
| `BACKLOGR_SERVICE` | `backlogr` (systemd `--user` unit) |
| `BACKLOGR_DB`, `BACKLOGR_PORT` | environment, else `$BACKLOGR_HOME/.env`, else `data/backlogr.db` and `8796` |

## Tests
`.venv/bin/python -m pytest -q tests` (unit, CLI local + remote, auth 401s, live MCP HTTP end-to-end). `ensure.sh` health-checks `$BACKLOGR_URL`. Migrating from the earlier "task manager" deployment: [MIGRATION.md](MIGRATION.md).

## License
[MIT](LICENSE) (see license for copyright attribution)

## Diagnostics and restore

`backlogr --version` prints the installed code version. `backlogr doctor` prints configuration without the token value, including token presence and token-file permissions. Locally it checks DB writability and schema version/columns; remotely it checks `/healthz` and an authenticated `list` with `limit=1`. Exit status is 0 when all checks pass, 1 otherwise. Remote token files should have mode 600. Other CLI failures, including unreadable files, invalid import JSON, and network errors, print `error: ...` on stderr and exit 2.

List pagination is optional: `--limit N --offset N` (MCP/API: `limit`, `offset`), with no limit by default. Both must be non-negative integers; pagination applies after filtering.

`backlogr import export.json --by Bot` accepts either the exported `{"tasks": [...]}` object or a task list. Normal imports, including API/MCP snapshots, enforce approval, evidence, and test gates; snapshot history and derived metadata are not restored. For trusted local recovery, `backlogr import export.json --by Bot --restore` preserves IDs, timestamps, task fields, update history, and links verbatim, bypassing creation gates. Remote `--restore` is rejected; unset `BACKLOGR_URL` for local use. Existing IDs are left as they are and reported in `skipped`; the result also lists `created` IDs and per-item `errors`. SQLite backups remain the primary restore method.

## One-call human actions (`act`)

`backlogr act T-id done|na|question|later|undo [--reason TEXT] [--by NAME]` (MCP: `task_act`, API op `act`). `--by` defaults to `BACKLOGR_OWNER` (`owner` when unset). Set `BACKLOGR_OWNER=Operator` on the server and CLI clients to retain that name.

| action | effect |
|---|---|
| `done` | status `done`, with evidence `confirmed done by <by>[: reason]`. If `<by>` is listed in `BACKLOGR_HUMANS` (and in `BACKLOGR_APPROVERS`, when that is set), their word counts: the tests gate is skipped, and a missing approval is recorded as `<by> via act done`. Anyone else goes through the normal gates. |
| `na` | status `declined`, with the reason in the note |
| `question` | status unchanged. `--reason` is the question; it is stored in `open_question` and listed under `flags.questions_for_owner`, and it clears on the owner bot's next update |
| `later` | adds a note without changing status; `--reason` is required |
| `undo` | restores status, evidence, approval, tests and question to what they were before the last act. This works only if nothing else changed the task since. |

Every act is logged in the history as `act <action> by <by>...`. Each act is checked inside the write lock. If the task changed after it was read, the act is refused with "retry". Names in `BACKLOGR_HUMANS` (comma-separated) never count as bots, even after they act on tasks. The HTTP API accepts only the parameters exposed by the corresponding MCP tool; unknown fields return HTTP 400 (`unknown field: x`). Request bodies over 1 MiB return HTTP 413.

Dashboard mapping: `DASH T-x DONE` → `act T-x done`; `DASH T-x N/A[: r]` → `act T-x na --reason r`; `DASH T-x QUESTION: q` → `act T-x question --reason q`.

## Rules and Playbooks (1.5.0)

Store reusable rules alongside tasks, and group them into playbooks with optional
skill names and test commands. IDs are supplied by you. A rule needs nonempty
`rule` text; its title and other descriptive fields are optional.

```bash
backlogr rules add M1 --title "Check examples" --rule "Examples must run." --test "python -m pytest" --category quality --by Agent
backlogr playbooks add P-check --name "Example checks" --rules M1 --skill example-check --test "python -m pytest"
backlogr rules list --playbook P-check --testable yes --json
backlogr rules get M1
backlogr rules untested
backlogr playbooks list
backlogr playbooks get P-check --json
backlogr rules import tests/fixtures/rules-playbooks.json --by Agent --json
```

`rules add` accepts `--evidence`, `--source`, `--applies-to`, `--playbook`,
`--testable yes|no`, and `--replace`. Duplicate rule IDs fail unless `--replace`
is given; replacement supplies the full editable record, preserving creation
metadata. `--by` defaults to `BACKLOGR_OWNER` for rule creation. Playbook add
upserts by ID; `--description` is optional. An omitted `--rules` preserves links;
an explicit list replaces additional links. Unknown rule/playbook references fail.

A rule's `testable` defaults to true when it has a test. Empty or whitespace-only
tests and tests starting with `UNTESTED` (case-insensitive, ignoring leading
whitespace) always derive false. Explicit true with an empty test is rejected.
`rules untested` and `rules list --untested` return every rule with
`testable=false`, just like `rules list --testable no`. This includes empty tests,
`UNTESTED` placeholders, and rules explicitly added or imported with
`testable=false` despite descriptive test text. Rules with `testable=true` are
excluded. MCP/API `rule_untested` uses the same flag-based filter.
`rules list` also accepts `--category`. `--json` works at every command level.

The same authenticated HTTP API and MCP expose `rule_list`, `rule_get`,
`rule_add`, `rule_untested`, `playbook_list`, `playbook_get`, `playbook_add`, and
`rules_import`. API operations use those exact names: POST to
`$BACKLOGR_URL/api/rule_get` with `{"id":"M1"}`, for example. See the
[agent skill](skills/backlogr/SKILL.md) for signatures and
[import format](docs/RULES_PLAYBOOKS.md) for relationships and bulk import.

## Live dashboard

Open `/dashboard` on your Backlogr server (or `/backlogr/dashboard` under a
`/backlogr` reverse-proxy prefix) for the live dashboard. The page reads
SQLite on every request and refreshes every 60 seconds. It shows task counts,
assignees, decisions waiting on the owner (`BACKLOGR_OWNER`), task cards (grouped by
optional `BACKLOGR_DASHBOARD_CATEGORIES`), the latest 40 finished
tasks, up to 80 timeline events from the last 48 hours, and rules/playbooks.
`/dashboard.json` serves the same computed model for other read-only clients.

Both routes accept GET and HEAD only and require the `Authorization: Bearer …`
API credential. Behind `tailscale serve` you can set
`BACKLOGR_TRUST_TAILSCALE_HEADERS=1` to also accept the `Tailscale-User-Login`
identity header it adds; enable that only when the listener is reachable solely
through `tailscale serve`, because a direct client could forge the header. The
existing `BACKLOGR_ALLOWED_HOSTS` configuration also applies. The dashboard sends
a strict Content-Security-Policy and has no scripts, task action controls, or
external assets. Times use `BACKLOGR_TZ` (default: the server's local zone).
