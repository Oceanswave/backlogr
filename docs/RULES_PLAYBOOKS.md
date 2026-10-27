# Rules and Playbooks

Rules capture requirements, tests, evidence and provenance independently of task
completion. Test commands are stored as text; Backlogr never executes them.
Playbooks group rules and can name an agent skill or describe a broader test.
All queries sort by ID. Get operations reject unknown IDs. Playbook results
include sorted `rule_ids` and full rule objects in `rules`.

## Untested rules

Core, CLI `rules untested` and `rules list --untested`, MCP `rule_untested`, and
HTTP API `POST /api/rule_untested` return every rule whose `testable` flag is
false, equivalent to listing rules with `testable=false`. This includes rules
with descriptive test text explicitly added or imported with `testable=false`.
Rules with `testable=true` are excluded.

Empty or whitespace-only tests and tests starting with `UNTESTED`
(case-insensitive, ignoring leading whitespace) always force `testable=false`.
Explicit true with an empty test is rejected. Other test text defaults to
`testable=true` unless false is supplied explicitly.

## Import

CLI: `backlogr rules import FILE --by Agent`. MCP/API:
`rules_import(items, actor)` where `items` is an object:

```json
{
  "rules": [
    {"id": "M1", "title": "Check examples", "rule": "Examples must run.",
     "test": "python -m pytest", "category": "quality", "playbook_id": "P-check"},
    {"id": "M2", "rule": "Review wording.", "test": "UNTESTED: manual review"}
  ],
  "playbooks": [
    {"id": "P-check", "name": "Example checks", "skill": "example-check",
     "test": "python -m pytest", "description": "Checks for examples.",
     "rule_ids": ["M1", "M2"]}
  ]
}
```

Both arrays are optional. Each ID must be unique within its array. All references
must resolve either in the same file or in the existing database. Imports are
atomic: an invalid record or unknown reference rolls back the entire batch.
They upsert full records and report integer `created`, `updated`, and `unchanged`
counts across both arrays; linked/embedded rules are not counted twice. Repeating
the same input preserves timestamps and reports only unchanged records. Rule
creation timestamps and `created_by` remain those of the original creation.

Rules accept `id`, `title`, `rule`, `test`, `evidence`, `source`, `testable`,
`category`, `applies_to`, and nullable `playbook_id`. Other than `testable` and
`playbook_id`, these are strings; omitted optional text defaults to empty.
Import also accepts `playbook` as an alias for `playbook_id` (conflicting values
are rejected), and serializes a list of strings in `applies_to` to JSON text.
Actor is supplied once for the import; record timestamps are managed by Backlogr.

A rule's nullable `playbook_id` is its primary playbook and implies a row in
`playbook_rules`. Additional links allow the same rule in multiple playbooks.
Changing a rule's primary playbook removes the old primary link and adds the new
one. Playbook add replaces additional links when `rule_ids` is supplied, while
always retaining primary memberships. Omitted `rule_ids` preserves existing links.
Clearing a primary membership requires replacing the rule with `playbook_id=null`.
Rule filters by playbook include both primary and additional links.

## Storage and recovery

Version 1.5.0 creates `rules`, `playbooks` and `playbook_rules` automatically on
connection and advances SQLite `user_version` from 0 or 1 to 2. Foreign keys
protect references; a composite primary key prevents duplicate memberships.
Boolean and test-content checks protect the testable invariant in SQLite as well
as in core validation. Existing task rows and append-only history are preserved.

SQLite backups contain rules and playbooks. Since 2.1.0 the JSON export (nightly
and `backlogr export`) also carries `rules` and `playbooks` in this import format,
and `backlogr import export.json` (with or without `--restore`) upserts them through
`rules_import`. Restored rules get the importing actor and new timestamps as
creation metadata; use a SQLite backup when those must be preserved.

`backlogr rules remove ID` (`rule_remove`) deletes a rule and its playbook links.
`backlogr playbooks remove ID` (`playbook_remove`) deletes a playbook but keeps its
rules; rules whose primary playbook it was get `playbook_id` null. No new
environment variables or permissions are needed. HTTP API and MCP use the same
authentication as tasks; the existing 1 MiB API body limit applies to imports.
