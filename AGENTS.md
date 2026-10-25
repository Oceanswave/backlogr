# AGENTS.md

Backlogr is a task system of record for AI agents. Rule: **no task, no work.**

- **Use it:** load the skill at [`skills/backlogr/SKILL.md`](skills/backlogr/SKILL.md) (install steps in [README → Install the skill](README.md#install-the-skill)).
- **Connect:** add the MCP server `backlogr` at `$BACKLOGR_URL/mcp` with header `Authorization: Bearer $BACKLOGR_TOKEN` ([README → Install the MCP](README.md#install-the-mcp)). Never commit or print the token.
- **Develop:** `python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/python -m pytest -q tests`. Keep the MCP tool verbs (`task_*`) stable. Env vars use the `BACKLOGR_` prefix. No hostnames, IPs, tokens or personal paths in the repo.
- **Operate/migrate:** see [`MIGRATION.md`](MIGRATION.md) and `deploy/`.
