"""MCP server (streamable HTTP on 127.0.0.1, or stdio with --stdio) for Backlogr."""
import sys, os, threading, time, datetime as dt, logging, hmac, json, glob, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from typing import Optional
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import core

PORT = int(os.environ.get("BACKLOGR_PORT", "8796"))
HOSTS = [h.strip() for h in os.environ.get("BACKLOGR_HOSTS", "127.0.0.1").split(",") if h.strip()]
TOKEN = os.environ.get("BACKLOGR_TOKEN", "")
BACKUP_DIR = os.environ.get("BACKLOGR_BACKUP_DIR", "")
BACKUP_KEEP = int(os.environ.get("BACKLOGR_BACKUP_KEEP", "14"))
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

@mcp.tool()
def task_create(title: str, owner_bot: str, source: str, actor: str, done_means: str = "",
                due: Optional[str] = None, status: str = "todo", approval_needed: bool = False,
                note: Optional[str] = None, ask: Optional[str] = None,
                approved_by: Optional[str] = None, approval_source: Optional[str] = None,
                evidence: Optional[str] = None, test_results: Optional[dict[str, str]] = None) -> dict:
    """Create a task. source: direct_to_bot | via_chukar | bot_initiated. status: todo|in_progress|waiting_on_sean|blocked|done|declined. due: ISO 8601. done_means: one line saying what done looks like. ask: required with status waiting_on_sean, one line saying exactly what Sean must decide or do. approved_by/approval_source: the human approval (never a bot name); required before an approval_needed task can be in_progress or done. evidence/test_results: see task_update. actor: your bot name."""
    return core.create(_c(), title=title, owner_bot=owner_bot, source=source, actor=actor,
                       done_means=done_means, due=due, status=status, approval_needed=approval_needed, note=note, ask=ask,
                       approved_by=approved_by, approval_source=approval_source, evidence=evidence, test_results=test_results)

@mcp.tool()
def task_update(id: str, actor: str, status: Optional[str] = None, due: Optional[str] = None,
                owner_bot: Optional[str] = None, note: Optional[str] = None,
                approval_needed: Optional[bool] = None, ask: Optional[str] = None,
                approved_by: Optional[str] = None, approval_source: Optional[str] = None,
                evidence: Optional[str] = None, test_results: Optional[dict[str, str]] = None) -> dict:
    """Update a task's status/due/owner/approval_needed and/or append a note to its log. Moving to waiting_on_sean requires ask (one line: exactly what Sean must decide or do). Record a human approval with approved_by (the human, never a bot) and approval_source (e.g. "Sean in Chukar chat 11:17"); an approval_needed task cannot move to in_progress or done without one. Moving to done requires evidence (link, commit sha, path or command output summary), and test_results {test name: "pass"|"fail"} when done_means mentions tests. actor: your bot name."""
    return core.update(_c(), id, actor, status=status, due=due, owner_bot=owner_bot, note=note, approval_needed=approval_needed, ask=ask,
                       approved_by=approved_by, approval_source=approval_source, evidence=evidence, test_results=test_results)

@mcp.tool()
def task_add_link(id: str, actor: str, ref: str, kind: str = "other") -> dict:
    """Attach a link (URL or plain text) to a task. kind e.g. pr, doc, order, message, page, file, other."""
    return core.add_link(_c(), id, actor, ref, kind)

@mcp.tool()
def task_get(id: str) -> dict:
    """Get one task with its full updates log and links."""
    return core.get(_c(), id)

@mcp.tool()
def task_list(owner: Optional[str] = None, status: Optional[str] = None, overdue: bool = False,
              stale_hours: Optional[float] = None, include_closed: bool = False, due_before: Optional[str] = None,
              q: Optional[str] = None, source: Optional[str] = None) -> list:
    """List tasks (open only unless status or include_closed given). Filters (AND): owner, status (one or comma list e.g. "todo,in_progress"), overdue, stale_hours (no update in N hours), due_before (ISO date), q (text in title/done_means/notes/links), source."""
    return core.list_tasks(_c(), owner=owner, status=status, overdue=overdue, stale_hours=stale_hours, include_closed=include_closed,
                           due_before=due_before, q=q, source=source)

@mcp.tool()
def task_flags(stale_hours: float = 24, created_within_hours: float = 24) -> dict:
    """Check-in view: overdue, stale, blocked, waiting_on_sean, and tasks created in the last N hours."""
    return core.flags(_c(), stale_hours=stale_hours, created_within_hours=created_within_hours)

@mcp.tool()
def task_sean() -> list:
    """Sean queue: every waiting_on_sean task, oldest first, with its exact ask and age in hours."""
    return core.sean_queue(_c())

@mcp.tool()
def task_stale(hours: float = 2) -> dict:
    """in_progress tasks with no note or change in the last N hours, grouped by owner (oldest first)."""
    return core.stale(_c(), hours=hours)

@mcp.tool()
def task_import(items: list[dict], actor: str) -> dict:
    """Bulk create tasks. Each item takes task_create fields except actor (title, owner_bot, source, done_means, due, status, approval_needed, note, created_at) plus optional links [{ref, kind}]."""
    return core.bulk_import(_c(), items, actor)


# ---- plain JSON API used by the `backlogr` CLI (same auth) ----
API = {
    "create": lambda c, a: core.create(c, **a),
    "update": lambda c, a: core.update(c, a.pop("id"), a.pop("actor"), **a),
    "add_link": lambda c, a: core.add_link(c, a["id"], a["actor"], a["ref"], a.get("kind", "other")),
    "get": lambda c, a: core.get(c, a["id"]),
    "list": lambda c, a: core.list_tasks(c, **a),
    "flags": lambda c, a: core.flags(c, **a),
    "stale": lambda c, a: core.stale(c, **a),
    "sean": lambda c, a: core.sean_queue(c),
    "import": lambda c, a: core.bulk_import(c, a["items"], a["actor"]),
    "export_data": lambda c, a: {"exported_at": core.now(), "tasks": [core.get(c, r["id"]) for r in c.execute("SELECT id FROM tasks ORDER BY created_at")]},
}

def build_app():
    from starlette.responses import JSONResponse, PlainTextResponse
    inner = mcp.streamable_http_app()
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        path = scope["path"]
        if path == "/healthz":
            return await PlainTextResponse("ok")(scope, receive, send)
        hdr = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        if not TOKEN or not hmac.compare_digest(hdr.encode(), f"Bearer {TOKEN}".encode()):
            return await JSONResponse({"error": "unauthorized"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        if path.startswith("/api/"):
            op = path[5:]
            if op not in API or scope["method"] != "POST":
                return await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
            body = b""
            while True:
                m = await receive(); body += m.get("body", b"")
                if not m.get("more_body"): break
            try:
                r = API[op](core.connect(), json.loads(body or b"{}")); code = 200
            except core.TaskError as e:
                r, code = {"error": str(e)}, 400
            except (TypeError, KeyError, ValueError) as e:
                r, code = {"error": f"bad request: {e}"}, 400
            return await JSONResponse(r, status_code=code)(scope, receive, send)
        return await inner(scope, receive, send)
    return app, inner

def backup():
    if not BACKUP_DIR: return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    out = os.path.join(BACKUP_DIR, f"backlogr-{dt.date.today().isoformat()}.db")
    src = core.connect(); dst = sqlite3.connect(out + ".tmp")
    src.backup(dst); dst.close(); src.close(); os.replace(out + ".tmp", out)
    for old in sorted(glob.glob(os.path.join(BACKUP_DIR, "backlogr-*.db")))[:-BACKUP_KEEP]:
        os.remove(old)
    return out

def nightly_export():
    last = None
    while True:
        n = dt.datetime.now()
        if n.hour == 2 and last != n.date():
            last = n.date()
            for f in (lambda: core.export(_c()), backup):
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
