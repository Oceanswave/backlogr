# Migrating an existing "task manager" deployment to Backlogr

Side-by-side install, data copy, cutover and rollback. Values in `<angle brackets>` are deployment-specific; set them as shell variables first. Nothing is secret here: tokens are read from files, never typed or echoed.

```
OLD=~/task-manager          # old install dir (unit task-manager.service, serve path /task-manager)
NEW=~/backlogr
OLD_PORT=<old port>         # e.g. 8795
NEW_PORT=8796
HOST=backlogr.example.invalid
REV=<release tag or merge sha>
```

| | Old | New |
|---|---|---|
| Dir / unit | `$OLD`, `task-manager.service` | `$NEW`, `backlogr.service` |
| Port | `$OLD_PORT` | `$NEW_PORT` |
| Path / MCP URL | `/task-manager`, `https://$HOST/task-manager/mcp` | `/backlogr`, `https://$HOST/backlogr/mcp` |
| DB | `$OLD/data/tasks.db` | `$NEW/data/backlogr.db` |
| MCP server name | `bot-team-tasks` | `backlogr` |
| CLI | `task` | `backlogr` |

Env renames (old names are ignored, no aliases): `TASKS_TOKEN`→`BACKLOGR_TOKEN`, `TASKS_DB`→`BACKLOGR_DB`, `TASKS_PORT`→`BACKLOGR_PORT`, `TASKS_HOSTS`→`BACKLOGR_HOSTS`, `TASKS_BACKUP_DIR`→`BACKLOGR_BACKUP_DIR`, `TASKS_BACKUP_KEEP`→`BACKLOGR_BACKUP_KEEP`, `TASKS_EXPORT`→`BACKLOGR_EXPORT`, CLI `TASK_URL`→`BACKLOGR_URL`, `TASK_TOKEN_FILE`→`BACKLOGR_TOKEN_FILE`. New: `BACKLOGR_ALLOWED_HOSTS` (the old code hard-coded its proxy host name; set it to `$HOST`).

## 1. Install side by side (old service keeps running)
```
git clone '<repo-url>' "$NEW" && cd "$NEW" && git checkout "$REV"
python3 -m venv .venv && .venv/bin/pip install -q -e '.[test]' && .venv/bin/python -m pytest -q tests
mkdir -p data backups exports && chmod 700 data backups exports
install -m 600 "$OLD/.token" "$NEW/.token"      # same token: clients only change URL
umask 077
sed -e 's/^TASKS_/BACKLOGR_/' -e "s/^BACKLOGR_PORT=.*/BACKLOGR_PORT=$NEW_PORT/" \
    -e 's#/data/tasks\.db#/data/backlogr.db#' -e "s#$OLD#$NEW#g" -e "s#task-manager/#backlogr/#g" \
    "$OLD/.env" > "$NEW/.env"
grep -q '^BACKLOGR_ALLOWED_HOSTS=' "$NEW/.env" || echo "BACKLOGR_ALLOWED_HOSTS=$HOST" >> "$NEW/.env"
cut -d= -f1 "$NEW/.env"                          # names only; check BACKLOGR_EXPORT/BACKUP_DIR paths
```

## 2. Copy the DB (consistent online copy)
```
sqlite3 "$OLD/data/tasks.db" ".backup '$NEW/data/backlogr.db'"
q='PRAGMA integrity_check; SELECT (SELECT count(*) FROM tasks),(SELECT count(*) FROM updates),(SELECT count(*) FROM links),(SELECT max(seq) FROM updates);'
sqlite3 "$OLD/data/tasks.db" "$q"; sqlite3 "$NEW/data/backlogr.db" "$q"    # must match
```

## 3. Start Backlogr and add its route
```
install -D -m 644 "$NEW/deploy/backlogr.service" ~/.config/systemd/user/backlogr.service
systemctl --user daemon-reload && systemctl --user enable --now backlogr
curl -fsS "http://127.0.0.1:$NEW_PORT/healthz"
tailscale serve --bg --https=443 --set-path /backlogr "http://127.0.0.1:$NEW_PORT"
tailscale serve status                                   # both /task-manager and /backlogr
curl -fsS "https://$HOST/backlogr/healthz"
curl -s -o /dev/null -w '%{http_code}\n' -X POST "https://$HOST/backlogr/api/list" -d '{}'     # 401
# authed check; header comes from a file, not argv:
curl -s -H @<(printf 'Authorization: Bearer %s\n' "$(cat "$NEW/.token")") -X POST "https://$HOST/backlogr/api/list" -d '{}' | head -c 200
```

## 4. Cut over (old clients keep working)
1. Freeze **all** writers: stop every stdio MCP client (ones that ssh in and run `$OLD/server.py --stdio` or `backlogr-server --stdio`), then `systemctl --user stop task-manager backlogr`. Check `pgrep -af "server.py|backlogr-server"` shows nothing. Do not reconnect any client until step 2 finishes, or its writes are lost by the recopy.
2. Final copy with everything stopped: rerun step 2, compare counts, then `systemctl --user start backlogr`. Only now reconnect clients, to Backlogr (stdio: `backlogr-server --stdio`, or the HTTP URL).
3. Repoint the **old** path at Backlogr so the old CLI wrapper and old MCP URL keep working during the transition (same API, same token):
   `tailscale serve --bg --https=443 --set-path /task-manager "http://127.0.0.1:$NEW_PORT"`, then `curl -fsS "https://$HOST/task-manager/healthz"`.
4. Move clients to `https://$HOST/backlogr/mcp` (README → Install the MCP), add a `backlogr` CLI wrapper with `BACKLOGR_URL`, `BACKLOGR_TOKEN_FILE` and `BACKLOGR_EXPORT` set.
5. After every client has moved and the old path shows no traffic: `tailscale serve --https=443 --set-path /task-manager off` (never plain `--https=443 off`, which drops every route) and `systemctl --user disable task-manager`. Keep `$OLD` read-only as an archive.

## Rollback
Freeze all writers first, exactly as in step 4.1: stop every stdio MCP client and `systemctl --user stop backlogr task-manager`; `pgrep -af "server.py|backlogr-server"` must show nothing. Reconnect clients only after the old route is restored below.
```
# If Backlogr took writes, carry them back (schema is identical):
sqlite3 "$NEW/data/backlogr.db" ".backup '$OLD/data/tasks.db.from-backlogr'"
#   inspect, then with task-manager stopped: rm -f "$OLD/data/tasks.db-wal" "$OLD/data/tasks.db-shm" && mv "$OLD/data/tasks.db.from-backlogr" "$OLD/data/tasks.db"
systemctl --user start task-manager
tailscale serve --bg --https=443 --set-path /task-manager "http://127.0.0.1:$OLD_PORT"   # restore the old route
curl -fsS "https://$HOST/task-manager/healthz"
tailscale serve --https=443 --set-path /backlogr off
```
Then point clients back at `/task-manager/mcp`.

## Upgrading 1.0.x → 1.1.0 (schema)
1.1.0 adds columns to `tasks` (see `core.ADDED_COLUMNS`); the server adds them on start, so a restart migrates the DB in place. **Downgrading code below 1.1.0 after that needs the pre-deploy DB backup restored** (1.0.x inserts by column position and fails on the wider table). `scripts/deploy.sh` keeps that backup in `backups/pre-deploy-<ts>.db`.

Behaviour changes in 1.1.0 that existing callers may hit: moving to `waiting_on_sean` needs `ask`; an `approval_needed` task needs a human `approved_by` before `in_progress` or `done` (rows already past that point are left alone); moving to `done` needs `evidence` (and per-test results when `done_means` mentions tests). `key` is new and optional; a unique index `t_key` covers it.

## v1.2.0
Additive: the nullable columns `open_question` and `last_act` are added automatically at startup. New: `act` (CLI), `task_act` (MCP), API op `act`, and `flags.questions_for_owner`. New optional env var `BACKLOGR_HUMANS=Name[,Name]`: people who may use `act done` as an attestation and who never count as bots. Set it in the deploy env, for example to the dashboard owner's name. If it is unset, `act done` uses the normal gates.

## v1.4.0

Set `BACKLOGR_OWNER` to the person's name on the server and CLI clients; it defaults to `owner`. Set it consistently to the existing operator name when upgrading. This changes message text and the default actor for `act`, while keeping statuses, `sean`, `task_sean`, and source values. `BACKLOGR_HUMANS` and `BACKLOGR_APPROVERS` retain their existing meaning.

API callers must send only parameters exposed by the corresponding MCP tool. Unknown fields now return 400, including fields starting with `_`. Import items may still carry IDs and timestamps. API bodies larger than 1 MiB return 413; split larger imports into smaller batches or use the local CLI.

The existing additive column migration is unchanged. Databases with SQLite `user_version=0` are marked as schema version 1 on connection. Run `backlogr doctor` after upgrading to check configuration, schema, and connectivity. Use local `backlogr import export.json --by Bot --restore` (no `BACKLOGR_URL`) for trusted recovery with task fields, history, and links preserved verbatim, bypassing creation gates. Remote `--restore` is rejected. Normal CLI, API, and MCP imports enforce approval, evidence, and test gates, including for snapshots; existing IDs are skipped.

## v1.5.0

Back up the database before upgrading. On connection, the existing idempotent
schema setup creates `rules`, `playbooks`, and the many-to-many `playbook_rules`
table and advances `PRAGMA user_version` from 0 or 1 to 2. Existing tasks, links,
and append-only updates are unchanged. Restart and run `backlogr doctor` to
validate the tables and schema version. Repeated starts do not duplicate rows.
No new environment variables are required. SQLite backups include all tables;
task JSON exports do not include rules/playbooks. See
[Rules and Playbooks](docs/RULES_PLAYBOOKS.md) for bulk import and recovery.
Downgrading code leaves the additive tables in place; restoring the pre-upgrade
backup is the clean rollback, and loses writes made after that backup.

## v2.0.0

Back up the database first (`scripts/deploy.sh` does this for you). Then restart.

**Data (schema version 3).** On the first connection, stored values are rewritten
once: status `waiting_on_sean` becomes `waiting_on_owner`, and source `via_chukar`
becomes `via_coordinator`. The append-only update log is not rewritten, so older
notes still show the old words.

**Renamed interfaces.** The old names still work as *input* everywhere: core, CLI,
MCP and API all accept them. Results always use the new names, so update any
client code that compares status or source strings.

| Before | After |
|---|---|
| status `waiting_on_sean` | `waiting_on_owner` |
| source `via_chukar` | `via_coordinator` |
| `task_flags` key `waiting_on_sean` | `waiting_on_owner` |
| MCP `task_sean` | `task_waiting` (the old tool is removed; MCP clients rediscover tools) |
| API op `sean` | `waiting` (`sean` is kept as an alias for older CLIs) |
| CLI `backlogr sean` | `backlogr waiting` (`sean` is kept as an alias) |
| `core.sean_queue` | `core.waiting_queue` |

**Dashboard.** Bot names, colors and glyphs now come from your data instead of a
built-in roster. Card categories are off unless you set
`BACKLOGR_DASHBOARD_CATEGORIES`. Times use `BACKLOGR_TZ`, or the server's local zone.

**Tailscale header auth is now opt-in.** If you open the dashboard through
`tailscale serve` without a bearer token, add `BACKLOGR_TRUST_TAILSCALE_HEADERS=1`
to `.env`. Do this only if the listener binds loopback and is reached solely
through `tailscale serve`.

**Approvals.** `BACKLOGR_OWNER` is now always a human. People who use `act`
no longer become "bots" just by acting. This fixes a lockout where an owner who
had clicked `later` or `question` could no longer approve tasks.

**Rollback.** Older code still runs on a v3 database, but it does not recognise
the new status and source values. To roll back cleanly, restore the pre-deploy
backup.

Update installed agent skills from `skills/backlogr/SKILL.md`.

## v2.1.0

No configuration changes. On the first connection, schema version 4 turns any
`due` stored as an empty string (from an earlier `--due ""` update) into null.
JSON exports now also contain `rules` and `playbooks`; importing such an export
upserts them. Older code ignores those keys when importing. A container image is
published at `ghcr.io/oceanswave/backlogr:2.1.0`; the systemd install is unchanged.

## v2.2.0

The modules moved into the `backlogr` package. Deploy as usual (`pip install -e .`
regenerates the `backlogr` and `backlogr-server` commands). Existing systemd units
that run `.venv/bin/python server.py` keep working through a compatibility launcher;
switch them to the new unit when convenient:
```
install -m 644 deploy/backlogr.service ~/.config/systemd/user/backlogr.service
systemctl --user daemon-reload && systemctl --user restart backlogr
```
Code that imported `core`, `cli`, `server` or `dashboard` directly now imports
them from `backlogr` (for example `from backlogr import core`).
