# Bot Team Task Manager

System of record for **all** of Sean's bot work (research, purchases, edits, outreach, code). Not git-based: status describes the work.

## Rule for every bot (paste verbatim)
> **No task, no work: before you start anything for Sean, create or find its task in the Bot Team Task Manager (`task_create` / `task new`), and record every status change, blocker and result on that task (`task_update` / `task update`, `task done`) as it happens, using your bot name as the actor.**

## Where it lives (since 2026-10-24: server-host)
- Host: **server-host**, `~/task-manager/` (user `<user>`), systemd **user** unit `task-manager.service` (Restart=always, enabled, linger on, so it starts at boot without a login).
- Listens on `127.0.0.1:8795` and `100.64.0.10:8795` only (8787 on the tailnet IP is taken by x-outbox). Never 0.0.0.0, no Funnel, no public DNS.
- **MCP URL (use this):** `https://backlogr.example.invalid/task-manager/mcp` (tailscale serve, tailnet-only, proxies to 127.0.0.1:8795).
  Direct `http://100.64.0.10:8795/mcp` also works for tailnet nodes the ACL lets reach port 8795.
- Health (no auth): `.../task-manager/healthz` -> `ok`.
- Auth: every other request needs `Authorization: Bearer <token>`; otherwise 401.
- Token: server-host `~/task-manager/.token` (mode 600; also inside `~/task-manager/.env`). Box copy: `/workspace/coordinator/tasks/.token` (mode 600). Never paste the token into chat, tickets or logs.
- DB: server-host `~/task-manager/data/tasks.db` (SQLite WAL; updates log append-only via triggers).
- Nightly at ~2 AM ET (inside the service): JSON export to `~/task-manager/exports/tasks-export.json` and `sqlite3` backup API copy to `~/task-manager/backups/tasks-YYYY-MM-DD.db` (14 kept).
- Store export for Chukar: `/cursor/stores/store-002199cb-d2af-4901-a2b2-4845c7dd7e95/tasks-export.json` is written by `task export` on the box (pulls from the service); the box check-in routine runs it.
- The old box DB `/workspace/coordinator/tasks/tasks.db` is retired and read-only (migrated 45 tasks / 122 updates / 10 links, checksums matched).

## Operating (on server-host)
```
systemctl --user status|restart task-manager
journalctl --user -u task-manager -f
cd ~/task-manager && .venv/bin/python -m pytest -q tests
```
Deploy: copy `core.py server.py cli.py tests/` to `~/task-manager/`, then `systemctl --user restart task-manager`.
Remove only this serve route: `tailscale serve --https=443 --set-path /task-manager off` (do NOT run plain `--https=443 off`, it drops every route).

## Connecting clients (token read from the file, never typed into chat)
- Claude Code:
  `claude mcp add --transport http task-manager https://backlogr.example.invalid/task-manager/mcp --header "Authorization: Bearer $(cat ~/task-manager/.token)"`
- Codex (`~/.codex/config.toml`), with `TASKS_TOKEN` exported in the shell that launches Codex:
  ```toml
  [mcp_servers.task-manager]
  url = "https://backlogr.example.invalid/task-manager/mcp"
  bearer_token_env_var = "TASKS_TOKEN"
  ```
  (Codex on server-host itself can use `http://127.0.0.1:8795/mcp`.)
- hal9000 / any MCP client: streamable HTTP, URL above, header `Authorization: Bearer <token>`. Stdio-only clients: `ssh <user>@<server-host> 'cd task-manager && set -a && . ./.env && .venv/bin/python server.py --stdio'`.
- Grok Bot agents: register as a remote MCP connector with that URL and header (the box is `tag:agent` and reaches it via the serve route).

## MCP tools
task_create, task_update (status/due/owner/approval_needed/note), task_add_link, task_get, task_list (owner, status, overdue, stale_hours, include_closed), task_flags (overdue, stale, blocked, waiting_on_sean, created_recently), task_import.
Fields: id, title, done_means, owner_bot, due (ISO), status (todo|in_progress|waiting_on_sean|blocked|done|declined), source (direct_to_bot|via_chukar|bot_initiated), approval_needed, created_at, updated_at, updates[], links[].

## CLI (`/workspace/tools/bin/task` on the box)
Talks to the service over HTTPS (`TASK_URL`, default the serve URL; `TASK_TOKEN_FILE`, default `/workspace/coordinator/tasks/.token`). Without `TASK_URL` it uses a local DB (`TASKS_DB`).
```
task new "Order sample tee" --owner Mint --source via_chukar --by Chukar --done-means "tee arrives, photo posted" --due 2026-10-30 --approval
task update T-abc123 --by Mint --status blocked --note "needs vault card"
task update T-abc123 --by Mint --link https://printify.com/... --kind order
task done T-abc123 --by Mint --note "delivered"
task list [--owner Mint] [--status waiting_on_sean] [--overdue] [--stale 24] [--all]
task flags [--stale 24] [--recent 24]
task get T-abc123      task import file.json --by Chukar      task export [--path P]
```
Add `--json` before the subcommand for JSON output. The CLI uses authed `POST /api/<op>` endpoints (create, update, add_link, get, list, flags, import, export_data).

## Tests
`.venv/bin/python -m pytest -q tests` (unit, CLI local + remote, auth 401s, live MCP HTTP end-to-end). 14 pass.
`ensure.sh` now only health-checks the server-host service; `run.sh` is retired.
