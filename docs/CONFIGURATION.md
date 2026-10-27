# Configuration (2.2.0)

These are all `BACKLOGR_*` environment variables read by the Python application
and shipped shell scripts. Defaults below apply when unset; an explicitly empty
value can differ. Python reads process environment, not `.env` files. Source a
trusted shell-compatible file with `set -a; . ./.env; set +a`, or use the systemd
unit's EnvironmentFile. [deploy/env.example](../deploy/env.example) is a template:
replace its placeholders before using it.

Core DB/export paths and server settings are captured on module import; set them
before starting a process and restart after changes. Owner, human/bot/approver,
and link settings are read when used. `~` defaults are expanded by Python;
custom path values should be absolute (Python does not expand a literal `~` in
an environment value).

## Application and clients

| Variable | Default | Meaning / reader |
|---|---|---|
| `BACKLOGR_DB` | `~/backlogr/data/backlogr.db` | SQLite path: server, stdio MCP, local CLI. Connections create/migrate it. |
| `BACKLOGR_EXPORT` | `~/backlogr/exports/backlogr-export.json` | Nightly JSON export and default CLI export destination; `export --path` overrides it. Remote CLI export writes on the client. |
| `BACKLOGR_PORT` | `8796` | Integer HTTP listener port; no CLI port flag. |
| `BACKLOGR_HOSTS` | `127.0.0.1` | Comma-separated listener addresses, trimmed; must contain at least one address. |
| `BACKLOGR_TOKEN` | empty | Required for HTTP mode; exact bearer token for API/MCP. Not a remote CLI credential source. Local doctor reports presence if the token file is unavailable. |
| `BACKLOGR_ALLOWED_HOSTS` | empty | Comma-separated extra Host names accepted by the MCP transport's DNS rebinding protection. Bind names, localhost, and 127.0.0.1 are also included, with port variants. Does not check the separately handled API, health, or action routes. |
| `BACKLOGR_BACKUP_DIR` | empty (disabled) | HTTP nightly SQLite backup directory. Files are `backlogr-YYYY-MM-DD.db`, written through a temporary file. |
| `BACKLOGR_BACKUP_KEEP` | `14` | Integer number of daily backup files retained; values below 1 are treated as 1. Backups are written mode 600. |
| `BACKLOGR_URL` | unset (local mode) | Nonempty selects remote CLI API mode. Also required by `ensure.sh` for its health check. Include the proxy path, if any. |
| `BACKLOGR_TOKEN_FILE` | `~/backlogr/.token` | Remote CLI reads and strips this file. Remote doctor requires a nonempty token and mode 600; it does not fall back to the environment token. |
| `BACKLOGR_OWNER` | `owner` | Name in messages, dashboard headings, and default human actor for `act` and rule creation; configure server and CLI consistently. Always treated as a human (never a bot) for approvals. |
| `BACKLOGR_BOTS` | empty | Comma-separated bot names barred from approving. DB owners, actors of non-`act` updates, and the current actor/owner are also considered bots, except configured humans. |
| `BACKLOGR_HUMANS` | empty | Comma-separated human names excluded from bot detection (`BACKLOGR_OWNER` is always included). Their `act done` can attest completion, skip the test gate, and supply missing approval, subject to approver restrictions. |
| `BACKLOGR_APPROVERS` | empty (no explicit allowlist) | Comma-separated names allowed to approve; bot checks still apply. Human attestation is also restricted by this list when set. |
| `BACKLOGR_TZ` | server's local zone | IANA time zone (e.g. `Europe/Berlin`) for dashboard times and day grouping. |
| `BACKLOGR_DASHBOARD_CATEGORIES` | unset (no grouping) | Path to a JSON file of `[["Name", "regex"], ...]`. Dashboard cards are grouped by the first category whose case-insensitive regex matches the title, else notes and link refs; unmatched cards go under `Other`. Read once per process. |
| `BACKLOGR_TRUST_TAILSCALE_HEADERS` | unset (off) | `1`/`true`/`yes` lets a nonempty `Tailscale-User-Login` header authorize `/dashboard` and `/dashboard.json` without the bearer token. Enable only when every request reaches the listener through `tailscale serve`; anything that can connect directly could forge the header. Never applies to `/api` or MCP. |

Human/bot/approver names are trimmed and compared case-insensitively. Names are
workflow metadata, not authenticated individual identities. See
[security](../SECURITY.md) before changing attestation or exposure settings.

HTTP mode starts the nightly thread: approximately every five minutes it checks
for local hour 02:00, exporting and backing up once that day. No catch-up is
performed if the service misses that hour. Stdio mode does not start this thread.

## Deployment script

`scripts/deploy.sh` additionally reads these variables:

| Variable | Default | Meaning |
|---|---|---|
| `BACKLOGR_HOME` | `$HOME/backlogr` | Existing install directory; the script changes into it. |
| `BACKLOGR_SERVICE` | `backlogr` | systemd user unit to restart. |
| `BACKLOGR_HEALTH_TRIES` | `40` | Number of post-restart health attempts; each curl has a 2-second connect timeout and 5-second total timeout, with a 0.5-second sleep after a failed attempt. |

For deployment only, `BACKLOGR_DB` and `BACKLOGR_PORT` come from nonempty process
environment values, else the last matching assignment in `$BACKLOGR_HOME/.env`,
else `$BACKLOGR_HOME/data/backlogr.db` and `8796`. The script reads assignment
text rather than sourcing `.env`; use plain unquoted values for those two keys.
Pre-deploy backups always go in `$BACKLOGR_HOME/backups`, independently of
`BACKLOGR_BACKUP_DIR`. See [README deployment](../README.md#deploying-to-a-host-with-deploysh).

For a complete disposable setup, see [agent installation](../INSTALL_FOR_AGENTS.md).
