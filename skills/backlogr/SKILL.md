---
name: backlogr
description: Use whenever you start, change, block, finish or hand off any piece of work for the user, or are asked what is open, overdue, stale, blocked or waiting on the user. Backlogr is the system of record (no task, no work); this covers the backlogr CLI, the backlogr MCP tools (task_create, task_update, task_add_link, task_get, task_list, task_flags, task_stale, task_sean, task_import), the status flow and the logging rules.
---

# Backlogr

**No task, no work.** Before you start anything for the user, find or create its task, and record every status change, blocker and result on it as it happens, with your bot name as the actor.

## Connect
- MCP (preferred): server `backlogr`, streamable HTTP at `$BACKLOGR_URL/mcp`, header `Authorization: Bearer $BACKLOGR_TOKEN`.
- CLI: `backlogr` with `BACKLOGR_URL` and `BACKLOGR_TOKEN_FILE` (path to a mode-600 token file) set. Without `BACKLOGR_URL` it uses a local SQLite DB (`BACKLOGR_DB`).
- Never paste the token into chat, tickets, notes or logs.

## Fields
id (`T-xxxxxx`), title, done_means (one line: what done looks like), owner_bot, due (ISO 8601 date or datetime), status, source (`direct_to_bot` | `via_chukar` | `bot_initiated`), approval_needed, created_at, updated_at, updates[] (append-only log), links[] (`{ref, kind}`).

## Status flow
`todo` → `in_progress` → `done`, with side states `blocked` (say what unblocks it), `waiting_on_sean` (a decision or approval from the user is needed; `ask` is required: one line saying exactly what), and `declined` (won't do; say why). Reopen by setting a new open status with a note.

## Rules
1. **Owner:** set `owner_bot` to the bot doing the work; reassign with an update and a note, never silently.
2. **Due:** set `due` when there is a deadline or promise; move it with a note saying why.
3. **Notes:** every meaningful step gets a short `note` (what changed, what's next). The log is append-only; correct mistakes with a new note.
4. **Approvals:** if the work needs the user's OK (spend, send, publish, delete, merge to production), set `approval_needed` and status `waiting_on_sean` until you have it, then record it with `backlogr approve T-id --by <you> --approver Sean --source "Sean in Chukar chat 11:17"` (or `approved_by` + `approval_source` on `task_update`). An `approval_needed` task cannot move to `in_progress` or `done` without it, and a bot name is never accepted as the approver: bot-relayed approval never counts.
5. **Evidence:** attach links for results (`kind`: pr, doc, order, message, page, file, other). `done` requires `evidence` (link, commit sha, path or command output summary); when `done_means` mentions tests, also record each test as pass or fail (`--test name=pass|fail`, or `test_results` on `task_update`).
6. **No duplicates, no invented ids:** create with a stable `key` (e.g. `forge:pr-12-review`) and use only ids Backlogr returns. A similar-title warning means look at that task first.
7. Always pass `actor` = your bot name.

## MCP tools
| Tool | Use |
|---|---|
| `task_create(title, owner_bot, source, actor, done_means?, due?, status?, approval_needed?, note?, ask?, key?)` | new task; pass a stable `key` so a retry returns the same task (`existing: true`); heed any similar-title `warnings` |
| `task_update(id, actor, status?, due?, owner_bot?, note?, approval_needed?, ask?, approved_by?, approval_source?)` | change fields and/or append a note |
| `task_add_link(id, actor, ref, kind?)` | attach evidence |
| `task_get(id)` | full task with log and links |
| `task_list(owner?, status?, overdue?, stale_hours?, include_closed?)` | open tasks by default |
| `task_flags(stale_hours?, created_within_hours?)` | check-in view: overdue, stale, blocked, waiting_on_sean, created_recently |
| `task_sean()` | everything waiting_on_sean, oldest first, with its exact ask and age |
| `task_stale(hours?=2)` | in_progress rows with no note in N hours, grouped by owner |
| `task_import(items, actor)` | bulk create; each item takes task_create fields **except `actor`** (pass it once at the call level), plus optional `created_at` and `links` |

## CLI
```
backlogr new "Order sample tee" --owner Mint --source via_chukar --by Chukar --done-means "tee arrives" --due 2026-10-30 --approval --key mint:sample-tee   # --key: retries return the same task
backlogr update T-abc123 --by Mint --status in_progress --note "started"
backlogr update T-abc123 --by Mint --status blocked --note "needs card"
backlogr update T-abc123 --by Mint --link https://example.com/pr/1 --kind pr
backlogr update T-abc123 --by Mint --approval yes --status waiting_on_sean --ask "OK to spend \$40 on the sample tee?"
backlogr done T-abc123 --by Mint --evidence "photo of delivered parcel" --link https://example.com/photo --kind file
backlogr list [--owner Mint] [--status todo,in_progress] [--q PR] [--source via_chukar] [--due-before 2026-11-01] [--overdue] [--stale-hours 24] [--all]
backlogr flags [--stale 24] [--recent 24]
backlogr sean                       # waiting_on_sean queue: exact ask + age, oldest first
backlogr stale [--hours 2]          # in_progress with no note in N hours, by owner
backlogr get T-abc123 [--short]     backlogr import tasks.json --by Chukar     backlogr export [--path P]
```
`new`/`update`/`done` print one line (`T-abc123 in_progress`); add `--json` for the full row. Exit code 2 means the server rejected the call (message on stderr).

## Human actions
When a person says done, N/A, or asks a question about a task (for example the dashboard's `DASH T-x DONE|N/A: r|QUESTION: q`), use `backlogr act T-x done|na|question [--reason ...] [--by NAME]`, or `undo` to revert it. Check `flags` → `questions_for_owner` and answer the question with an update on your own task; that clears it.
