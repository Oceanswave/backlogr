"""MCP server (streamable HTTP on 127.0.0.1, or stdio with --stdio) for Backlogr."""
import sys, os, threading, time, datetime as dt, logging, hmac, json, glob, sqlite3
from typing import Optional
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import core
import dashboard
from contextlib import closing
import inspect

__version__ = core.__version__

PORT = int(os.environ.get("BACKLOGR_PORT", "8796"))
HOSTS = [h.strip() for h in os.environ.get("BACKLOGR_HOSTS", "127.0.0.1").split(",") if h.strip()]
TOKEN = os.environ.get("BACKLOGR_TOKEN", "")
BACKUP_DIR = os.environ.get("BACKLOGR_BACKUP_DIR", "")
BACKUP_KEEP = max(1, int(os.environ.get("BACKLOGR_BACKUP_KEEP", "14")))
# Accept a nonempty Tailscale-User-Login header as dashboard auth. Only safe when every request arrives through
# `tailscale serve`, which sets that header; anything that can reach the listener directly could forge it.
TRUST_TAILSCALE = os.environ.get("BACKLOGR_TRUST_TAILSCALE_HEADERS", "").strip().lower() in ("1", "true", "yes")
# Extra Host header names to accept (e.g. your reverse-proxy / tailnet DNS name), comma separated.
EXTRA_HOSTS = [h.strip() for h in os.environ.get("BACKLOGR_ALLOWED_HOSTS", "").split(",") if h.strip()]
_names = set(HOSTS) | {"127.0.0.1", "localhost"} | set(EXTRA_HOSTS)
_allowed = sorted(_names | {f"{h}:*" for h in _names})
mcp = FastMCP("backlogr", host=HOSTS[0],
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=_allowed, allowed_origins=[]), port=PORT, stateless_http=True,
    instructions="Backlogr, the system of record for all of the team's bot work. "
    "Rule: no task, no work. Before starting any work for the user, call task_create (or find the existing task), "
    "and record every status change with task_update. Always pass actor = your bot name.")

def _c(): return core.connect()

def _export():
    with closing(_c()) as c:
        return core.export(c)

@mcp.tool()
def task_create(title: str, owner_bot: str, source: str, actor: str, done_means: str = "",
                due: Optional[str] = None, status: str = "todo", approval_needed: bool = False,
                note: Optional[str] = None, ask: Optional[str] = None,
                approved_by: Optional[str] = None, approval_source: Optional[str] = None,
                evidence: Optional[str] = None, test_results: Optional[dict[str, str]] = None,
                key: Optional[str] = None) -> dict:
    """Create a task. key: a stable idempotency key (e.g. "forge:pr-12-review"); if a task already has it, that task is returned unchanged with existing=true, so retries never duplicate. The result has warnings when an open task has a very similar title: check it before carrying on. source: direct_to_bot | via_coordinator | bot_initiated. status: todo|in_progress|waiting_on_owner|blocked|done|declined. due: ISO 8601. done_means: one line saying what done looks like. ask: required with status waiting_on_owner, one line saying exactly what the owner must decide or do. approved_by/approval_source: the human approval (never a bot name); required before an approval_needed task can be in_progress or done. evidence/test_results: see task_update. actor: your bot name."""
    with closing(_c()) as c:
        return core.create(c, title=title, owner_bot=owner_bot, source=source, actor=actor,
                           done_means=done_means, due=due, status=status, approval_needed=approval_needed, note=note, ask=ask,
                           approved_by=approved_by, approval_source=approval_source, evidence=evidence, test_results=test_results, key=key)

@mcp.tool()
def task_update(id: str, actor: str, status: Optional[str] = None, due: Optional[str] = None,
                owner_bot: Optional[str] = None, note: Optional[str] = None,
                approval_needed: Optional[bool] = None, ask: Optional[str] = None,
                approved_by: Optional[str] = None, approval_source: Optional[str] = None,
                evidence: Optional[str] = None, test_results: Optional[dict[str, str]] = None) -> dict:
    """Update a task's status/due/owner/approval_needed and/or append a note to its log. Moving to waiting_on_owner requires ask (one line: exactly what the owner must decide or do). Record a human approval with approved_by (the human, never a bot) and approval_source (e.g. "owner in chat"); an approval_needed task cannot move to in_progress or done without one. Moving to done requires evidence (link, commit sha, path or command output summary), and test_results {test name: "pass"|"fail"} when done_means mentions tests. actor: your bot name."""
    with closing(_c()) as c:
        return core.update(c, id, actor, status=status, due=due, owner_bot=owner_bot, note=note, approval_needed=approval_needed, ask=ask,
                           approved_by=approved_by, approval_source=approval_source, evidence=evidence, test_results=test_results)

@mcp.tool()
def task_add_link(id: str, actor: str, ref: str, kind: str = "other") -> dict:
    """Attach a link (URL or plain text) to a task. kind e.g. pr, doc, order, message, page, file, other."""
    with closing(_c()) as c:
        return core.add_link(c, id, actor, ref, kind)

@mcp.tool()
def task_get(id: str) -> dict:
    """Get one task with its full updates log and links."""
    with closing(_c()) as c:
        return core.get(c, id)

@mcp.tool()
def task_list(owner: Optional[str] = None, status: Optional[str] = None, overdue: bool = False,
              stale_hours: Optional[float] = None, include_closed: bool = False, due_before: Optional[str] = None,
              q: Optional[str] = None, source: Optional[str] = None, limit: Optional[int] = None, offset: int = 0) -> list:
    """List tasks (open only unless status or include_closed given). Filters (AND): owner, status (one or comma list e.g. "todo,in_progress"), overdue, stale_hours (no update in N hours), due_before (ISO date), q (text in title/done_means/notes/links), source."""
    with closing(_c()) as c:
        return core.list_tasks(c, owner=owner, status=status, overdue=overdue, stale_hours=stale_hours, include_closed=include_closed,
                               due_before=due_before, q=q, source=source, limit=limit, offset=offset)

@mcp.tool()
def task_flags(stale_hours: float = 24, created_within_hours: float = 24) -> dict:
    """Check-in view: overdue, stale, blocked, waiting_on_owner, open questions for owners, and tasks created in the last N hours."""
    with closing(_c()) as c:
        return core.flags(c, stale_hours=stale_hours, created_within_hours=created_within_hours)

@mcp.tool()
def task_act(id: str, action: str, reason: Optional[str] = None, by: Optional[str] = None) -> dict:
    """One-call action by a person (default BACKLOGR_OWNER), e.g. from dashboard buttons. action: later (note only; reason required) | done (status done; the person's word is the evidence) | na (status declined, reason in the note) | question (status unchanged; reason is the question, marked as open_question for the owner bot until the owner updates) | undo (reverts the last act if nothing changed since). Logged in the task history as "act <action> by <by>"."""
    with closing(_c()) as c:
        return core.act(c, id, action, by=by, reason=reason)

@mcp.tool()
def task_waiting() -> list:
    """Owner queue: every waiting_on_owner task, oldest first, with its exact ask and age in hours."""
    with closing(_c()) as c:
        return core.waiting_queue(c)

@mcp.tool()
def task_stale(hours: float = 2) -> dict:
    """in_progress tasks with no note or change in the last N hours, grouped by owner (oldest first)."""
    with closing(_c()) as c:
        return core.stale(c, hours=hours)

@mcp.tool()
def task_import(items: list[dict] | dict, actor: str) -> dict:
    """Bulk create tasks. Each item takes task_create fields except actor (title, owner_bot, source, done_means, due, status, approval_needed, note, created_at) plus optional links [{ref, kind}]."""
    with closing(_c()) as c:
        return core.bulk_import(c, items, actor)


@mcp.tool()
def rule_list(playbook: Optional[str] = None, testable: Optional[bool] = None, category: Optional[str] = None) -> list:
    """List rules with optional playbook, testability and category filters."""
    with closing(_c()) as c:
        return core.rule_list(c, playbook=playbook, testable=testable, category=category)

@mcp.tool()
def rule_get(id: str) -> dict:
    """Get one rule."""
    with closing(_c()) as c:
        return core.rule_get(c, id=id)

@mcp.tool()
def rule_add(id: str, rule: str, title: str = '', test: str = '', evidence: str = '',
             source: str = '', testable: Optional[bool] = None, category: str = '',
             applies_to: str = '', playbook_id: Optional[str] = None,
             actor: Optional[str] = None, replace: bool = False) -> dict:
    """Add a rule; replace=true explicitly updates an existing ID. UNTESTED tests derive testable=false. actor defaults to BACKLOGR_OWNER."""
    with closing(_c()) as c:
        return core.rule_add(c, id=id, rule=rule, title=title, test=test, evidence=evidence,
                             source=source, testable=testable, category=category, applies_to=applies_to,
                             playbook_id=playbook_id, actor=actor, replace=replace)

@mcp.tool()
def rule_untested() -> list:
    """List every rule whose testable flag is false, including rules with test text."""
    with closing(_c()) as c:
        return core.rule_untested(c)

@mcp.tool()
def playbook_list() -> list:
    """List playbooks with linked rules."""
    with closing(_c()) as c:
        return core.playbook_list(c)

@mcp.tool()
def playbook_get(id: str) -> dict:
    """Get a playbook with linked rules embedded."""
    with closing(_c()) as c:
        return core.playbook_get(c, id=id)

@mcp.tool()
def playbook_add(id: str, name: str, skill: str = '', test: str = '', description: str = '', rule_ids: Optional[list[str]] = None) -> dict:
    """Upsert a playbook and its rule links; unknown rule IDs are errors."""
    with closing(_c()) as c:
        return core.playbook_add(c, id=id, name=name, skill=skill, test=test, description=description, rule_ids=rule_ids)

@mcp.tool()
def rules_import(items: dict, actor: str) -> dict:
    """Atomically import a rules/playbooks object; return created/updated/unchanged counts."""
    with closing(_c()) as c:
        return core.rules_import(c, items=items, actor=actor)

def export_data() -> dict:
    """Every task with its log and links (the CLI's remote export)."""
    with closing(_c()) as c:
        return core.snapshot(c)

# ---- plain JSON API used by the `backlogr` CLI (same auth): POST /api/<op> with the tool's arguments ----
API = {"create": task_create, "update": task_update, "add_link": task_add_link, "get": task_get, "list": task_list,
       "flags": task_flags, "stale": task_stale, "waiting": task_waiting, "act": task_act, "import": task_import,
       "export_data": export_data, "rule_list": rule_list, "rule_get": rule_get, "rule_add": rule_add,
       "rule_untested": rule_untested, "playbook_list": playbook_list, "playbook_get": playbook_get,
       "playbook_add": playbook_add, "rules_import": rules_import,
       "sean": task_waiting}  # pre-2.0 name of "waiting", kept for older CLI clients
API_FIELDS = {op: set(inspect.signature(fn).parameters) for op, fn in API.items()}
MAX_BODY = 1024 * 1024

DASHBOARD_HEADERS = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"}

def _bearer_ok(header):
    return bool(TOKEN) and hmac.compare_digest(header.encode("latin-1", "replace"), f"Bearer {TOKEN}".encode())

def build_app():
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
    from starlette.requests import HTTPConnection
    from mcp.server.transport_security import TransportSecurityMiddleware
    inner = mcp.streamable_http_app()
    security = TransportSecurityMiddleware(mcp.settings.transport_security)
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        path = scope["path"]
        if path == "/healthz":
            return await PlainTextResponse("ok")(scope, receive, send)
        if path in ("/dashboard", "/dashboard.json"):
            invalid = await security.validate_request(HTTPConnection(scope))
            if invalid is not None:
                return await invalid(scope, receive, send)
            headers = HTTPConnection(scope).headers
            authorized = _bearer_ok(headers.get("authorization", ""))
            if not authorized and not (TRUST_TAILSCALE and headers.get("tailscale-user-login", "").strip()):
                return await JSONResponse({"error": "unauthorized"}, status_code=401,
                                          headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            if scope["method"] not in ("GET", "HEAD"):
                return await PlainTextResponse("Method Not Allowed", status_code=405,
                                               headers={"Allow": "GET, HEAD"})(scope, receive, send)
            with closing(core.connect()) as c:
                data = dashboard.load_data(c)
            response = JSONResponse(data) if path.endswith(".json") else HTMLResponse(dashboard.render(data))
            if scope["method"] == "HEAD":
                response.body = b""
            response.headers.update(DASHBOARD_HEADERS)
            return await response(scope, receive, send)
        if not _bearer_ok(HTTPConnection(scope).headers.get("authorization", "")):
            return await JSONResponse({"error": "unauthorized"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        if path.startswith("/api/"):
            op = path[5:]
            if op not in API or scope["method"] != "POST":
                return await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
            body = b""
            while True:
                m = await receive()
                if m["type"] == "http.disconnect": return
                body += m.get("body", b"")
                if len(body) > MAX_BODY:
                    return await JSONResponse({"error": "request body too large"}, status_code=413)(scope, receive, send)
                if not m.get("more_body"): break
            try:
                args = json.loads(body or b"{}")
                if not isinstance(args, dict): raise core.TaskError("request must be an object")
                for field in args:
                    if field not in API_FIELDS[op]: raise core.TaskError(f"unknown field: {field}")
                r, code = API[op](**args), 200
            except core.TaskError as e:
                r, code = {"error": str(e)}, 400
            except (TypeError, ValueError) as e:
                r, code = {"error": f"bad request: {e}"}, 400
            except sqlite3.Error as e:
                logging.exception("api %s failed", op)
                r, code = {"error": f"database error: {e}"}, 503
            return await JSONResponse(r, status_code=code)(scope, receive, send)
        return await inner(scope, receive, send)
    return app, inner

def backup():
    if not BACKUP_DIR: return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    out = os.path.join(BACKUP_DIR, f"backlogr-{dt.date.today().isoformat()}.db")
    tmp = out + ".tmp"
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    with closing(core.connect()) as src, closing(sqlite3.connect(tmp)) as dst:
        src.backup(dst)
    os.replace(tmp, out)
    for old in sorted(glob.glob(os.path.join(BACKUP_DIR, "backlogr-*.db")))[:-BACKUP_KEEP]:
        os.remove(old)
    return out

def nightly_export():
    last = None
    while True:
        n = dt.datetime.now()
        if n.hour == 2 and last != n.date():
            last = n.date()
            for f in (_export, backup):
                try: logging.warning("nightly: %s", f())
                except Exception as e: logging.error("nightly job failed: %s", e)
        time.sleep(300)

def serve():
    import asyncio, uvicorn, contextlib
    app, inner = build_app()
    async def main():
        async with inner.router.lifespan_context(inner):
            servers = [uvicorn.Server(uvicorn.Config(app, host=h, port=PORT, lifespan="off", log_level="warning")) for h in HOSTS]
            await asyncio.gather(*(s.serve() for s in servers))
    asyncio.run(main())

def main_entry():
    core.connect().close()
    if "--stdio" in sys.argv:
        mcp.run("stdio")
    else:
        if not TOKEN: sys.exit("BACKLOGR_TOKEN is required for HTTP mode")
        threading.Thread(target=nightly_export, daemon=True).start()
        serve()

if __name__ == "__main__":
    main_entry()
