# Backlogr

A small, self-hosted system of record for a team of AI agents' work (research, purchases, edits, outreach, code). Agents talk to it over MCP or a CLI. Tasks are the objects; the MCP tool verbs are `task_*`. Rule: **no task, no work.**

- Python, SQLite (WAL), append-only update log enforced by triggers.
- MCP over streamable HTTP (or stdio), plus a JSON API used by the CLI. Bearer-token auth on everything except `/healthz`.
- Nightly JSON export and SQLite backup inside the service.

## Rule for every agent (paste verbatim)
> **No task, no work: before you start anything for the user, create or find its task in Backlogr (`task_create` / `backlogr new`), and record every status change, blocker and result on that task (`task_update` / `backlogr update`, `backlogr done`) as it happens, using your bot name as the actor.**

## Run the server
```
git clone https://github.com/Oceanswave/backlogr.git ~/backlogr && cd ~/backlogr
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
mkdir -p data backups exports && chmod 700 data backups exports
cp deploy/env.example .env && chmod 600 .env    # fill in BACKLOGR_TOKEN etc.
set -a && . ./.env && set +a && .venv/bin/backlogr-server
curl -fsS http://127.0.0.1:8796/healthz         # ok
```
As a systemd user service: `install -D -m 644 deploy/backlogr.service ~/.config/systemd/user/backlogr.service && systemctl --user daemon-reload && systemctl --user enable --now backlogr` (and `loginctl enable-linger $USER` to start at boot).

Expose it only on a private network or behind an authenticating reverse proxy, for example Tailscale serve under a path: `tailscale serve --bg --https=443 --set-path /backlogr http://127.0.0.1:8796`, then `BACKLOGR_URL=https://<your-host>.<your-tailnet>.ts.net/backlogr`. Add that host name to `BACKLOGR_ALLOWED_HOSTS`. Routes: `$BACKLOGR_URL/mcp`, `$BACKLOGR_URL/healthz`, `$BACKLOGR_URL/api/<op>`.

## Configuration
| Variable | Used by | Default |
|---|---|---|
| `BACKLOGR_TOKEN` | server (required for HTTP) | none |
| `BACKLOGR_DB` | server, local CLI | `~/backlogr/data/backlogr.db` |
| `BACKLOGR_PORT` | server | `8796` |
| `BACKLOGR_HOSTS` | server bind IPs (comma list) | `127.0.0.1` |
| `BACKLOGR_ALLOWED_HOSTS` | extra accepted Host headers (proxy DNS names) | none |
| `BACKLOGR_BACKUP_DIR` / `BACKLOGR_BACKUP_KEEP` | nightly backup (`backlogr-YYYY-MM-DD.db`) | off / `14` |
| `BACKLOGR_EXPORT` | nightly export, CLI `export` | `~/backlogr/exports/backlogr-export.json` |
| `BACKLOGR_URL` | CLI remote mode (and client configs below) | unset = local DB |
| `BACKLOGR_TOKEN_FILE` | CLI remote mode | `~/backlogr/.token` |
| `BACKLOGR_BOTS` | bot names that can never be `approved_by` (owners and actors in the DB are always included) | none |
| `BACKLOGR_APPROVERS` | if set, the only names accepted as `approved_by` | unset |

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
git clone https://github.com/Oceanswave/backlogr.git ~/src/backlogr
ln -s ~/src/backlogr/skills/backlogr ~/.claude/skills/backlogr     # Claude Code
ln -s ~/src/backlogr/skills/backlogr ~/.codex/skills/backlogr      # Codex
```
Other agents: point them at `AGENTS.md`, or paste the rule above into their instructions.

## MCP tools
task_create, task_update (status/due/owner/approval_needed/note), task_add_link, task_get, task_list (owner, status or comma list, overdue, stale_hours, due_before, q, source, include_closed), task_flags (overdue, stale, blocked, waiting_on_sean, created_recently), task_stale (hours), task_sean (waiting_on_sean queue with asks), task_import.
Fields: id, title, done_means, owner_bot, due (ISO), status (todo|in_progress|waiting_on_sean|blocked|done|declined), source (direct_to_bot|via_chukar|bot_initiated), approval_needed, ask (required when moving to waiting_on_sean), waiting_since, created_at, updated_at, updates[], links[].

## CLI (`backlogr`)
Remote when `BACKLOGR_URL` is set (token from `BACKLOGR_TOKEN_FILE`), else the local DB (`BACKLOGR_DB`).
```
backlogr new "Order sample tee" --owner Mint --source via_chukar --by Chukar --done-means "tee arrives" --due 2026-10-30 --approval --key mint:sample-tee   # --key: retries return the same task
backlogr update T-abc123 --by Mint --status blocked --note "needs card"
backlogr update T-abc123 --by Mint --link https://example.com/order --kind order
backlogr done T-abc123 --by Mint --evidence "https://example.com/pr/12" --test unit=pass --test e2e=pass   # --evidence required; --test when done_means mentions tests
backlogr list [--owner Mint] [--status todo,in_progress] [--q PR] [--source via_chukar] [--due-before 2026-11-01] [--overdue] [--stale-hours 24] [--all]
backlogr flags [--stale 24] [--recent 24]
backlogr sean                       # waiting_on_sean queue: exact ask + age, oldest first
backlogr approve T-id --by Forge --approver Sean --source "Sean in chat 11:17"   # human approval record
backlogr stale [--hours 2]          # in_progress with no note in N hours, by owner
backlogr get T-abc123 [--short]      backlogr import file.json --by Chukar      backlogr export [--path P]
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
[MIT](LICENSE) © 2026 Sean McLellan / BaristaLabs

## One-call human actions (`act`)

`backlogr act T-id done|na|question|undo [--reason TEXT] [--by NAME]` (MCP: `task_act`, API op `act`). `--by` defaults to `Sean`.

| action | effect |
|---|---|
| `done` | status `done`, with evidence `confirmed done by <by>[: reason]`. If `<by>` is listed in `BACKLOGR_HUMANS` (and in `BACKLOGR_APPROVERS`, when that is set), their word counts: the tests gate is skipped, and a missing approval is recorded as `<by> via act done`. Anyone else goes through the normal gates. |
| `na` | status `declined`, with the reason in the note |
| `question` | status unchanged. `--reason` is the question; it is stored in `open_question` and listed under `flags.questions_for_owner`, and it clears on the owner bot's next update |
| `undo` | restores status, evidence, approval, tests and question to what they were before the last act. This works only if nothing else changed the task since. |

Every act is logged in the history as `act <action> by <by>...`. Each act is checked inside the write lock. If the task changed after it was read, the act is refused with "retry". Names in `BACKLOGR_HUMANS` (comma-separated) never count as bots, even after they act on tasks. The HTTP API drops arguments that start with `_`.

Dashboard mapping: `DASH T-x DONE` → `act T-x done`; `DASH T-x N/A[: r]` → `act T-x na --reason r`; `DASH T-x QUESTION: q` → `act T-x question --reason q`.

## One-tap links

Set `BACKLOGR_LINK_SECRET` to a separate random secret; it is never derived from
`BACKLOGR_TOKEN`. Set `BACKLOGR_PUBLIC_URL` to the public base URL, including
any proxy path. Without the link secret, minting and `/act` are disabled.
`BACKLOGR_LINK_TTL` sets the default lifetime in seconds (604800, or seven days).
`BACKLOGR_LINK_ACTOR` sets the recorded actor (default `owner`). The live
deployment sets `BACKLOGR_LINK_ACTOR=Sean` and `BACKLOGR_HUMANS=Sean` so the
existing human action rules apply.

Mint locally with the secret and public URL set, or remotely with the usual
CLI connection settings:

```
backlogr link T-xxxxxx --action done --ttl 3600
backlogr link T-xxxxxx --all
```

MCP clients can use `task_link(id, action, ttl_seconds?)`. Actions are `done`,
`na`, `question`, and `later`. Opening a link only shows a confirmation form.
Submitting it applies the action once. Question and later require text; later
adds a note without changing status. Actions can be undone through `backlogr act T-xxxxxx undo`.

Links are bearer-like until expiry: anyone holding one can submit it. They
are single use, but opening one does not consume it. Keep TTLs short, protect
the secret, and serve only on a private network. Rotating the secret invalidates
outstanding links.
