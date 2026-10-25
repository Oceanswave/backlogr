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
task_create, task_update (status/due/owner/approval_needed/note), task_add_link, task_get, task_list (owner, status, overdue, stale_hours, include_closed), task_flags (overdue, stale, blocked, waiting_on_sean, created_recently), task_import.
Fields: id, title, done_means, owner_bot, due (ISO), status (todo|in_progress|waiting_on_sean|blocked|done|declined), source (direct_to_bot|via_chukar|bot_initiated), approval_needed, created_at, updated_at, updates[], links[].

## CLI (`backlogr`)
Remote when `BACKLOGR_URL` is set (token from `BACKLOGR_TOKEN_FILE`), else the local DB (`BACKLOGR_DB`).
```
backlogr new "Order sample tee" --owner Mint --source via_chukar --by Chukar --done-means "tee arrives" --due 2026-10-30 --approval
backlogr update T-abc123 --by Mint --status blocked --note "needs card"
backlogr update T-abc123 --by Mint --link https://example.com/order --kind order
backlogr done T-abc123 --by Mint --note "delivered"
backlogr list [--owner Mint] [--status waiting_on_sean] [--overdue] [--stale 24] [--all]
backlogr flags [--stale 24] [--recent 24]
backlogr get T-abc123      backlogr import file.json --by Chukar      backlogr export [--path P]
```
Add `--json` before the subcommand for JSON output.

## Tests
`.venv/bin/python -m pytest -q tests` (unit, CLI local + remote, auth 401s, live MCP HTTP end-to-end). `ensure.sh` health-checks `$BACKLOGR_URL`. Migrating from the earlier "task manager" deployment: [MIGRATION.md](MIGRATION.md).

## License
[MIT](LICENSE) © 2026 Sean McLellan / BaristaLabs
