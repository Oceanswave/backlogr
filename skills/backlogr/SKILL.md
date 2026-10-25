---
name: backlogr
description: Use whenever you start, change, block, finish or hand off any piece of work for the user, or are asked what is open, overdue, stale, blocked or waiting on the user. Backlogr is the system of record (no task, no work); this covers the backlogr CLI, the backlogr MCP tools (task_create, task_update, task_add_link, task_get, task_list, task_flags, task_stale, task_sean, task_import, task_act, task_link), the status flow and the logging rules.
---

# Backlogr

**No task, no work.** Before you start anything for the user, find or create its task, and record every status change, blocker and result on it as it happens, with your bot name as the actor.

## Connect

For a fresh install, follow [INSTALL_FOR_AGENTS.md](../../INSTALL_FOR_AGENTS.md); configuration details are in [docs/CONFIGURATION.md](../../docs/CONFIGURATION.md).

- MCP (preferred): server `backlogr`, streamable HTTP at `$BACKLOGR_URL/mcp`, header `Authorization: Bearer $BACKLOGR_TOKEN`.
- CLI: `backlogr` with `BACKLOGR_URL` and `BACKLOGR_TOKEN_FILE` (path to a mode-600 token file) set. Without `BACKLOGR_URL` it uses a local SQLite DB (`BACKLOGR_DB`).
- Never paste the token into chat, tickets, notes or logs.

## Fields
id (`T-xxxxxx`), title, done_means (one line: what done looks like), owner_bot, due (ISO 8601 date or datetime), status, source (`direct_to_bot` | `via_chukar` | `bot_initiated`), approval_needed, ask, waiting_since, approved_by, approved_at, approval_source, evidence, test_results, key, open_question, last_act, created_at, updated_at, updates[] (append-only log), links[] (`{ref, kind}`).

## Status flow
`todo` → `in_progress` → `done`, with side states `blocked` (say what unblocks it), `waiting_on_sean` (a decision or approval from the user is needed; `ask` is required: one line saying exactly what), and `declined` (won't do; say why). Reopen by setting a new open status with a note.

## Rules
1. **Owner:** set `owner_bot` to the bot doing the work; reassign with an update and a note, never silently.
2. **Due:** set `due` when there is a deadline or promise; move it with a note saying why.
3. **Notes:** every meaningful step gets a short `note` (what changed, what's next). The log is append-only; correct mistakes with a new note.
4. **Approvals:** if the work needs the user's OK (spend, send, publish, delete, merge to production), set `approval_needed` and status `waiting_on_sean` until you have it, then record it with `backlogr approve T-id --by <you> --approver Operator --source "Operator in chat"` (or `approved_by` + `approval_source` on `task_update`). An `approval_needed` task cannot move to `in_progress` or `done` without it, and a bot name is never accepted as the approver: bot-relayed approval never counts.
5. **Evidence:** attach links for results (`kind`: pr, doc, order, message, page, file, other). `done` requires `evidence` (link, commit sha, path or command output summary); when `done_means` mentions tests, also record each test as pass or fail (`--test name=pass|fail`, or `test_results` on `task_update`).
6. **No duplicates, no invented ids:** create with a stable `key` (e.g. `forge:pr-12-review`) and use only ids Backlogr returns. A similar-title warning means look at that task first.
7. Always pass `actor` = your bot name.

## MCP tools
| Tool | Use |
|---|---|
| `task_create(title, owner_bot, source, actor, done_means?, due?, status?, approval_needed?, note?, ask?, approved_by?, approval_source?, evidence?, test_results?, key?)` | new task; pass a stable `key` so a retry returns the same task (`existing: true`); heed any similar-title `warnings` |
| `task_update(id, actor, status?, due?, owner_bot?, note?, approval_needed?, ask?, approved_by?, approval_source?, evidence?, test_results?)` | change fields and/or append a note |
| `task_add_link(id, actor, ref, kind?)` | attach evidence |
| `task_get(id)` | full task with log and links |
| `task_list(owner?, status?, overdue?, stale_hours?, include_closed?, due_before?, q?, source?, limit?, offset?)` | open tasks by default |
| `task_flags(stale_hours?, created_within_hours?)` | check-in view: overdue, stale, blocked, waiting_on_sean, created_recently |
| `task_sean()` | everything waiting_on_sean, oldest first, with its exact ask and age |
| `task_stale(hours?=2)` | in_progress rows with no note in N hours, grouped by owner |
| `task_act(id, action, by?, reason?)` | human action: done, na, question, later, undo; `by` defaults to BACKLOGR_OWNER |
| `task_link(id, action, ttl_seconds?)` | mint signed single-use done/na/question/later URL (requires link configuration) |
| `task_import(items, actor)` | bulk create; each item takes task_create fields **except `actor`** (pass it once at the call level), plus optional `created_at` and `links` |

## CLI
```
backlogr new "Order sample tee" --owner Mint --source via_chukar --by Coordinator --done-means "tee arrives" --due 2026-10-30 --approval --key mint:sample-tee   # --key: retries return the same task
backlogr approve T-abc123 --by Mint --approver Operator --source "Operator in chat"
backlogr update T-abc123 --by Mint --status in_progress --note "started"
backlogr update T-abc123 --by Mint --status blocked --note "needs card"
backlogr update T-abc123 --by Mint --link https://example.com/pr/1 --kind pr
backlogr update T-abc123 --by Mint --approval yes --status waiting_on_sean --ask "OK to spend \$40 on the sample tee?"
backlogr done T-abc123 --by Mint --evidence "photo of delivered parcel" --link https://example.com/photo --kind file
backlogr list [--owner Mint] [--status todo,in_progress] [--q PR] [--source via_chukar] [--due-before 2026-11-01] [--overdue] [--stale-hours 24] [--all] [--limit 50] [--offset 0]
backlogr flags [--stale 24] [--recent 24]
backlogr sean                       # waiting_on_sean queue: exact ask + age, oldest first
backlogr stale [--hours 2]          # in_progress with no note in N hours, by owner
backlogr get T-abc123 [--short]
backlogr import tasks.json --by Coordinator
backlogr export [--path P]
backlogr approve T-abc123 --by Mint --approver Operator --source "Operator in chat"
backlogr link T-abc123 --action done --ttl 3600
backlogr link T-abc123 --all
backlogr --version
backlogr doctor
```
`new`/`update`/`done` print one line (`T-abc123 in_progress`); add `--json` for the full row. Ordinary CLI failures (validation, files, JSON, network, or server rejection) exit 2 with `error: ...` on stderr. `doctor` exits 0 on success or 1 on failed checks. Remote doctor requires a nonempty mode-600 token file and checks health plus authenticated list; local doctor opens/migrates the configured DB and checks writability/schema. `--json` works before or after subcommands for structured task/query output; doctor remains plain text.

## Human actions
When a person says done, N/A, or asks a question about a task (for example the dashboard's `DASH T-x DONE|N/A: r|QUESTION: q`), use `backlogr act T-x done|na|question|later [--reason ...] [--by NAME]`, or `undo` to revert it. Check `backlogr flags --json` → `questions_for_owner` and answer the question with an update on your own task; that clears it.

`question` and `later` require `--reason`; later only appends a note. `--by`
defaults to `BACKLOGR_OWNER` (default `owner`). Human names in `BACKLOGR_HUMANS`
can attest completion with `act done`, subject to `BACKLOGR_APPROVERS`; this can
supply missing approval and skip the test gate. These are workflow rules, not
independent identity verification. `undo` requires no intervening task change.

Action links require a separate `BACKLOGR_LINK_SECRET` and `BACKLOGR_PUBLIC_URL`.
They authorize whoever holds the URL; GET shows confirmation and POST consumes it once.
Keep links out of logs. Default TTL is 604800 seconds and link actor is literal
`owner` unless `BACKLOGR_LINK_ACTOR` is set (independent of BACKLOGR_OWNER).

Normal import accepts a task list or exported `{"tasks": [...]}` object and
enforces creation gates. Only trusted local recovery may use
`backlogr import snapshot.json --by Agent --restore`; this preserves snapshot
fields/history and bypasses gates. Remote `--restore` is rejected. Export and
restore can write files/DBs: use only the explicitly authorized destination.

For machine output, use returned IDs; bracketed optional flags above are usage
notation, not literal shell arguments. Legacy `via_chukar`, `waiting_on_sean`,
`sean`, and `task_sean` remain stable regardless of configured owner names.

## Rules and Playbooks (1.5.0)

Store reusable requirements and group them into playbooks. Record the work on a
task first. Rules need user-supplied IDs and nonempty rule text. Duplicate IDs
require explicit replacement. Playbook IDs and linked rule IDs must exist;
imports can create both together. Tests are stored, never executed by Backlogr.

| MCP tool (also HTTP API operation) | Use |
|---|---|
| `rule_list(playbook?, testable?, category?)` | list rules; filters combine with AND |
| `rule_get(id)` | full rule including creation metadata |
| `rule_add(id, rule, title?, test?, evidence?, source?, testable?, category?, applies_to?, playbook_id?, actor?="owner", replace?=false)` | create or explicitly replace a full rule; always pass your bot name as actor |
| `rule_untested()` | every rule with testable=false, including descriptive test text |
| `playbook_list()` | all playbooks with linked rules |
| `playbook_get(id)` | one playbook with `rule_ids` and embedded `rules` |
| `playbook_add(id, name, skill?, test?, description?, rule_ids?)` | upsert playbook; unknown rule IDs fail |
| `rules_import(items, actor)` | atomic `{rules: [...], playbooks: [...]}` upsert; created/updated/unchanged counts |

```bash
backlogr rules add M1 --rule "Examples must run." --title "Check examples" --test "python -m pytest" --evidence "test output" --source "project guide" --category quality --by Agent
backlogr playbooks add P-check --name "Example checks" --rules M1 --skill example-check --test "python -m pytest"
backlogr rules add M2 --rule "Review wording." --test "UNTESTED: manual review" --playbook P-check --by Agent
backlogr rules list --playbook P-check --testable yes --json
backlogr rules list --untested
backlogr rules list --category quality --testable no
backlogr rules get M1
backlogr rules untested
backlogr rules add M1 --rule "Examples must run." --test "python -m pytest" --replace --by Agent
backlogr rules import rules.json --by Agent --json
backlogr playbooks list
backlogr playbooks get P-check --json
```

`--json` works globally or after any subcommand. Rule add also accepts
`--applies-to` and `--testable yes|no`. Replacement preserves `created_at` and
`created_by`, but resets omitted editable fields to defaults. Empty tests or
case-insensitive `UNTESTED` prefixes always derive testable=false; true with an
empty test is an error. CLI `rules untested` and `rules list --untested`, MCP
`rule_untested`, and API `POST /api/rule_untested` return every rule with
testable=false, including rules explicitly added or imported with false despite
descriptive test text. Rules with testable=true are excluded.

A primary `playbook_id` implies a membership; additional many-to-many links are
allowed. Playbook add preserves links if `rule_ids` is omitted, otherwise replaces
additional links while retaining primary memberships. Imports take an `items`
object, not a file path; the CLI reads the file. Re-importing identical records
returns only unchanged counts and preserves timestamps. See
[import and storage details](../../docs/RULES_PLAYBOOKS.md).
