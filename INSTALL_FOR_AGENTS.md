# Install Backlogr 1.8.0 for agents

Requires Git, Python 3.11+, venv/pip, curl, and Bash on a Unix-like system.
Replace `<repo-url>` with the public clone URL. Run the blocks in the same Bash
session. This disposable installation keeps the clone, fresh venv, token file,
SQLite database, exports, backups, and logs in a new temporary directory.
Do not use an existing service's URL, credentials, or database for this check.

## Clone and install

```bash
RELEASE_CHECK_DIR="$(mktemp -d)"
git clone '<repo-url>' "$RELEASE_CHECK_DIR/repo"
cd "$RELEASE_CHECK_DIR/repo"
python3 -m venv "$RELEASE_CHECK_DIR/venv"
. "$RELEASE_CHECK_DIR/venv/bin/activate"
python -m pip install -e .
backlogr --version  # expected: 1.8.0
```

## Configure

Choose an unused port (the example uses 18796). The server reads the port from
the environment; it has no `--port` flag. Do not enable shell tracing (`set -x`)
when handling secrets. Secrets below are generated locally and never printed.

```bash
umask 077
export BACKLOGR_DB="$RELEASE_CHECK_DIR/backlogr.db"
export BACKLOGR_EXPORT="$RELEASE_CHECK_DIR/export.json"
export BACKLOGR_BACKUP_DIR="$RELEASE_CHECK_DIR/backups"
export BACKLOGR_BACKUP_KEEP=14
export BACKLOGR_PORT=18796
export BACKLOGR_HOSTS=127.0.0.1
export BACKLOGR_ALLOWED_HOSTS=localhost
export BACKLOGR_OWNER=owner
export BACKLOGR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export BACKLOGR_TOKEN_FILE="$RELEASE_CHECK_DIR/token"
printf '%s\n' "$BACKLOGR_TOKEN" > "$BACKLOGR_TOKEN_FILE"
chmod 600 "$BACKLOGR_TOKEN_FILE"
export BACKLOGR_URL="http://127.0.0.1:$BACKLOGR_PORT"
unset BACKLOGR_HUMANS BACKLOGR_BOTS BACKLOGR_APPROVERS
```

The remote CLI reads `BACKLOGR_TOKEN_FILE`, **not** `BACKLOGR_TOKEN`. Without
`BACKLOGR_URL`, CLI commands open the local `BACKLOGR_DB` instead. Python does
not load `.env` automatically; deployment examples are in
[deploy/env.example](deploy/env.example) and [configuration](docs/CONFIGURATION.md).

Only configure `BACKLOGR_HUMANS`/`BACKLOGR_APPROVERS` deliberately; they affect
completion and approval gates. See [security](SECURITY.md).

## Start, verify, and create a task

```bash
backlogr-server > "$RELEASE_CHECK_DIR/server.log" 2>&1 &
BACKLOGR_SERVER_PID=$!
trap 'kill "$BACKLOGR_SERVER_PID" 2>/dev/null || true; wait "$BACKLOGR_SERVER_PID" 2>/dev/null || true' EXIT

# Wait up to roughly ten seconds for startup; a failed check stops this block.
for attempt in {1..50}; do
    kill -0 "$BACKLOGR_SERVER_PID" 2>/dev/null || break
    if curl --fail --silent --max-time 1 "$BACKLOGR_URL/healthz" > "$RELEASE_CHECK_DIR/healthz"; then
        break
    fi
    sleep 0.2
done
kill -0 "$BACKLOGR_SERVER_PID" && curl --fail --show-error --silent --max-time 2 "$BACKLOGR_URL/healthz" || exit 1
# expected response: ok

backlogr new "Verify disposable installation" --owner ReleaseAgent \
    --source bot_initiated --by ReleaseAgent \
    --done-means "Server health and diagnostics succeed" --key release:install-check
# expected: T-<six hex digits> todo
backlogr doctor
# expected: version 1.8.0, owner owner, mode remote, token file mode 600,
# healthz: OK, authenticated list: OK; exit status 0
```

An unauthenticated health response checks reachability only. `doctor` also
checks an authenticated API request. If startup fails, inspect the temporary
`server.log`; choose another unused port if necessary and rerun from Configure.
MCP clients can connect to `$BACKLOGR_URL/mcp` using
`Authorization: Bearer $BACKLOGR_TOKEN`; client setup is in the
[README](README.md#install-the-mcp).

## Stop

```bash
kill "$BACKLOGR_SERVER_PID"
wait "$BACKLOGR_SERVER_PID" || true
trap - EXIT
deactivate
unset BACKLOGR_TOKEN
```

The disposable files remain in `$RELEASE_CHECK_DIR` for inspection. Remove that
specific directory when finished. These settings are session-local; a restart
requires configuring the environment again. For persistent deployment, see
[README](README.md#run-the-server) and [MIGRATION.md](MIGRATION.md).
