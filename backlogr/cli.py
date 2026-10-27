"""backlogr CLI. Talks to the remote service when BACKLOGR_URL is set (token from BACKLOGR_TOKEN_FILE), else the local DB. Run `backlogr -h`."""
import argparse, ipaddress, json, os, sys, urllib.parse, urllib.request, urllib.error
from . import core
import stat, sqlite3
from http.client import HTTPException
from pathlib import Path
from contextlib import closing

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib forwards the Authorization header on redirects, even to another host; refuse to follow them."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise core.TaskError(f"HTTP {code}: refusing to follow a redirect to {newurl}; set BACKLOGR_URL to the final URL")

_opener = urllib.request.build_opener(_NoRedirect)

def _loopback(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    try: return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError: return False

class Remote:
    """Mirrors the core.* calls used below over the authed /api/ endpoints."""
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token
        if self.url.startswith("http://") and not _loopback(self.url):
            print(f"warning: sending the Backlogr token over plain HTTP to {self.url}; use https", file=sys.stderr)
    def _call(self, op, **a):
        req = urllib.request.Request(f"{self.url}/api/{op}", data=json.dumps(a).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with _opener.open(req, timeout=20) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            try: msg = json.load(e).get("error", str(e))
            except Exception: msg = str(e)
            raise core.TaskError(f"HTTP {e.code}: {msg}")
    def create(self, c, title, owner_bot, source, actor, done_means="", due=None, status="todo", approval_needed=False, note=None, ask=None, **kw):
        return self._call("create", title=title, owner_bot=owner_bot, source=source, actor=actor, done_means=done_means,
                          due=due, status=status, approval_needed=approval_needed, note=note, ask=ask, **kw)
    def update(self, c, tid, actor, **kw): return self._call("update", id=tid, actor=actor, **kw)
    def add_link(self, c, tid, actor, ref, kind="other"): return self._call("add_link", id=tid, actor=actor, ref=ref, kind=kind)
    def get(self, c, tid): return self._call("get", id=tid)
    def list_tasks(self, c, owner=None, status=None, overdue=False, stale_hours=None, include_closed=False, due_before=None, q=None, source=None, limit=None, offset=0):
        return self._call("list", owner=owner, status=status, overdue=overdue, stale_hours=stale_hours, include_closed=include_closed,
                          due_before=due_before, q=q, source=source, limit=limit, offset=offset)
    def flags(self, c, stale_hours=24, created_within_hours=24):
        return self._call("flags", stale_hours=stale_hours, created_within_hours=created_within_hours)
    def act(self, c, tid, action, by=None, reason=None): return self._call("act", id=tid, action=action, by=by, reason=reason)
    def waiting_queue(self, c): return self._call("waiting")
    def stale(self, c, hours=2): return self._call("stale", hours=hours)
    def bulk_import(self, c, items, actor): return self._call("import", items=items, actor=actor)
    def rule_list(self, c, **args): return self._call("rule_list", **args)
    def rule_get(self, c, **args): return self._call("rule_get", **args)
    def rule_add(self, c, **args): return self._call("rule_add", **args)
    def rule_untested(self, c, **args): return self._call("rule_untested", **args)
    def playbook_list(self, c, **args): return self._call("playbook_list", **args)
    def playbook_get(self, c, **args): return self._call("playbook_get", **args)
    def playbook_add(self, c, **args): return self._call("playbook_add", **args)
    def rules_import(self, c, **args): return self._call("rules_import", **args)
    def rule_remove(self, c, **args): return self._call("rule_remove", **args)
    def playbook_remove(self, c, **args): return self._call("playbook_remove", **args)
    def export(self, c, path=None):
        data = self._call("export_data"); path = path or core.EXPORT_PATH
        core.write_private(path, lambda f: json.dump(data, f, indent=1))
        return {"path": path, "count": len(data["tasks"]), "source": self.url}

def backend():
    url = os.environ.get("BACKLOGR_URL")
    if not url: return core, core.connect()
    tf = os.environ.get("BACKLOGR_TOKEN_FILE", os.path.expanduser("~/backlogr/.token"))
    with open(tf) as f: token = f.read().strip()
    return Remote(url, token), None

def doctor():
    url = os.environ.get("BACKLOGR_URL")
    tf = Path(os.environ.get("BACKLOGR_TOKEN_FILE", os.path.expanduser("~/backlogr/.token")))
    print(f"version: {core.__version__}")
    print(f"owner: {core.owner()}")
    print(f"mode: {'remote' if url else 'local'}")
    ok = True
    token = os.environ.get("BACKLOGR_TOKEN", "")
    try:
        mode = stat.S_IMODE(tf.stat().st_mode)
        with tf.open() as f: token = f.read().strip()
        print(f"token file: mode {mode:03o}; token {'set' if token else 'unset'}")
        if url and (mode != 0o600 or not token): ok = False
    except OSError:
        print(f"token file: unavailable; environment token {'set' if token else 'unset'}")
        if url: ok = False
    try:
        if url:
            print(f"URL: {url}")
            with urllib.request.urlopen(url.rstrip('/') + '/healthz', timeout=20) as r:
                if r.status != 200: raise core.TaskError("healthz failed")
            print("healthz: OK")
            B, _ = backend()
            B.list_tasks(None, limit=1)
            print("authenticated list: OK")
        else:
            path = Path(core.DB_PATH)
            print(f"DB: {path}")
            if path.exists() and (not path.stat().st_mode & 0o222 or not os.access(path, os.W_OK)):
                raise core.TaskError("DB is not writable")
            with closing(core.connect()) as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute("ROLLBACK")
                columns = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
                required = {"id", "title", "done_means", "owner_bot", "due", "status", "source", "approval_needed", "created_at", "updated_at"} | {n for n, _ in core.ADDED_COLUMNS}
                if not required <= columns: raise core.TaskError("schema columns missing")
                version = c.execute("PRAGMA user_version").fetchone()[0]
                if version != core.SCHEMA_VERSION: raise core.TaskError("schema version mismatch")
                for table, fields in (("updates", {"seq", "task_id", "ts", "actor", "note"}), ("links", {"seq", "task_id", "ts", "ref", "kind"}),
                                      ("rules", {"id", "title", "rule", "test", "evidence", "source", "testable", "category", "applies_to", "playbook_id", "created_at", "updated_at", "created_by"}),
                                      ("playbooks", {"id", "name", "skill", "test", "description", "created_at", "updated_at"}),
                                      ("playbook_rules", {"playbook_id", "rule_id"})):
                    if not fields <= {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                        raise core.TaskError(f"schema columns missing: {table}")
            print(f"DB writable: OK; schema version {version}, columns: OK")
    except (core.TaskError, OSError, ValueError, sqlite3.Error, HTTPException) as e:
        print(f"error: {e}", file=sys.stderr); ok = False
    return 0 if ok else 1

def main(argv=None):
    p = argparse.ArgumentParser(prog="backlogr", description="Backlogr CLI (no task, no work).")
    p.add_argument("--version", action="version", version=core.__version__)
    p.add_argument("--json", action="store_true", help="JSON output")
    s = p.add_subparsers(dest="cmd", required=True)
    jp = argparse.ArgumentParser(add_help=False)
    jp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="full JSON output")
    _add = s.add_parser
    s.add_parser = lambda *a, **k: _add(*a, parents=[jp], **k)
    n = s.add_parser("new"); n.add_argument("title"); n.add_argument("--owner", required=True)
    n.add_argument("--source", required=True, type=core.canon, choices=core.SOURCES); n.add_argument("--by", required=True, help="actor bot")
    n.add_argument("--done-means", default=""); n.add_argument("--due"); n.add_argument("--status", default="todo", type=core.canon, choices=core.STATUSES)
    n.add_argument("--approval", action="store_true"); n.add_argument("--note")
    n.add_argument("--ask", help=f"required with --status waiting_on_owner: exactly what {core.owner()} must decide or do")
    n.add_argument("--key", help="stable idempotency key: if a task has it, return that task instead of creating one")
    n.add_argument("--evidence", help="required with --status done"); n.add_argument("--test", action="append", metavar="NAME=pass|fail")
    u = s.add_parser("update"); u.add_argument("id"); u.add_argument("--by", required=True)
    u.add_argument("--status", type=core.canon, choices=core.STATUSES)
    u.add_argument("--due", type=lambda v: "" if v.strip().lower() in ("", "none") else v, help='ISO date/time; "none" clears it'); u.add_argument("--owner"); u.add_argument("--note")
    u.add_argument("--approval", choices=["yes", "no"]); u.add_argument("--ask", help="required when moving to waiting_on_owner")
    u.add_argument("--link"); u.add_argument("--kind", default="other")
    u.add_argument("--evidence", help="required with --status done"); u.add_argument("--test", action="append", metavar="NAME=pass|fail")
    ac = s.add_parser("act", help="one-call human action: done | na | question | later | reply | undo")
    ac.add_argument("id"); ac.add_argument("action", choices=core.ACTS)
    ac.add_argument("--reason", help="N/A reason, question/note text (required for question/later/reply), or a done note")
    ac.add_argument("--by", default=core.owner(), help="the person acting (default BACKLOGR_OWNER)")
    ap = s.add_parser("approve", help="record a human approval on an approval_needed task")
    ap.add_argument("id"); ap.add_argument("--by", required=True, help="actor bot recording it")
    ap.add_argument("--approver", required=True, help="the human who approved (never a bot)")
    ap.add_argument("--source", required=True, help=f'where, e.g. "{core.owner()} in chat"')
    d = s.add_parser("done"); d.add_argument("id"); d.add_argument("--by", required=True); d.add_argument("--note"); d.add_argument("--link"); d.add_argument("--kind", default="other")
    d.add_argument("--evidence", required=True, help="link, commit sha, path or command output summary")
    d.add_argument("--test", action="append", metavar="NAME=pass|fail", help="per-test result; required when done_means mentions tests")
    g = s.add_parser("get"); g.add_argument("id"); g.add_argument("--short", action="store_true", help="one line")
    l = s.add_parser("list"); l.add_argument("--owner"); l.add_argument("--status", help="status or comma list, e.g. todo,in_progress")
    l.add_argument("--overdue", action="store_true"); l.add_argument("--stale", "--stale-hours", dest="stale", type=float, metavar="HOURS")
    l.add_argument("--due-before", metavar="DATE"); l.add_argument("--q", metavar="TEXT", help="word-start text match in title, done_means, notes or links")
    l.add_argument("--source", type=core.canon, choices=core.SOURCES); l.add_argument("--all", action="store_true")
    l.add_argument("--limit", type=int); l.add_argument("--offset", type=int, default=0)
    s.add_parser("doctor", help="check configuration and connectivity")
    f = s.add_parser("flags"); f.add_argument("--stale", type=float, default=24); f.add_argument("--recent", type=float, default=24)
    s.add_parser("waiting", aliases=["sean"], help="waiting_on_owner queue, oldest first, with each exact ask")
    st = s.add_parser("stale", help="in_progress rows with no note in N hours, by owner"); st.add_argument("--hours", type=float, default=2)
    i = s.add_parser("import"); i.add_argument("file"); i.add_argument("--by", required=True)
    i.add_argument("--restore", action="store_true", help="restore trusted snapshots verbatim (local only)")
    s.add_parser("export").add_argument("--path")
    for section in ('rules', 'playbooks'):
        group_parser = s.add_parser(section)
        commands = group_parser.add_subparsers(dest='operation', required=True)
        def command(name): return commands.add_parser(name, parents=[jp])
        listing = command('list')
        getting = command('get'); getting.add_argument('id')
        adding = command('add'); adding.add_argument('id')
        command('remove').add_argument('id')
        if section == 'rules':
            listing.add_argument('--playbook'); listing.add_argument('--category')
            listing.add_argument('--untested', action='store_true'); listing.add_argument('--testable', choices=['yes', 'no'])
            command('untested')
            importing = command('import'); importing.add_argument('file'); importing.add_argument('--by', required=True)
            adding.add_argument('--rule', required=True); adding.add_argument('--title', default='')
            adding.add_argument('--evidence', default=''); adding.add_argument('--source', default='')
            adding.add_argument('--category', default=''); adding.add_argument('--applies-to', default='')
            adding.add_argument('--playbook', dest='playbook_id'); adding.add_argument('--testable', choices=['yes', 'no'])
            adding.add_argument('--replace', action='store_true'); adding.add_argument('--by', default=None, help='default BACKLOGR_OWNER')
        else:
            adding.add_argument('--name', required=True); adding.add_argument('--skill', default='')
            adding.add_argument('--description', default=''); adding.add_argument('--rules')
        adding.add_argument('--test', default='')
    a = p.parse_args(argv); a.json = getattr(a, "json", False)
    if a.cmd == "sean": a.cmd = "waiting"  # pre-2.0 alias
    if a.cmd == "doctor": return doctor()
    c = None
    try:
        if a.cmd == "import" and a.restore and os.environ.get("BACKLOGR_URL"):
            raise core.TaskError("--restore is only available in local mode (unset BACKLOGR_URL)")
        B, c = backend()
        if a.cmd in ('rules', 'playbooks'):
            prefix = 'rule' if a.cmd == 'rules' else 'playbook'
            args = {}
            if a.operation == 'import':
                with open(a.file) as f: args = dict(items=json.load(f), actor=a.by)
                op = 'rules_import'
            else:
                op = prefix + '_' + a.operation
                if a.operation in ('get', 'remove'): args = dict(id=a.id)
                elif a.operation == 'list' and a.cmd == 'rules':
                    args = dict(playbook=a.playbook, category=a.category,
                                testable=None if a.testable is None else a.testable == 'yes')
                elif a.operation == 'add':
                    if a.cmd == 'rules':
                        args = {k: getattr(a, k) for k in ('id', 'rule', 'title', 'test', 'evidence', 'source', 'category', 'applies_to', 'playbook_id', 'replace')}
                        args.update(actor=a.by, testable=None if a.testable is None else a.testable == 'yes')
                    else:
                        args = {k: getattr(a, k) for k in ('id', 'name', 'skill', 'test', 'description')}
                        args['rule_ids'] = [v.strip() for v in a.rules.split(',') if v.strip()] if a.rules is not None else None
            r = getattr(B, op)(c, **args)
            if a.cmd == 'rules' and a.operation == 'list' and a.untested:
                r = [v for v in r if not v['testable']]
        elif a.cmd == "new":
            r = B.create(c, a.title, a.owner, a.source, a.by, a.done_means, a.due, a.status, a.approval, a.note, ask=a.ask,
                         **({"evidence": a.evidence} if a.evidence else {}), **({"test_results": a.test} if a.test else {}),
                         **({"key": a.key} if a.key else {}))
        elif a.cmd == "update":
            r = None
            # field changes first: if a gate rejects them, the link is not added either
            if any(x is not None for x in (a.status, a.due, a.owner, a.note, a.approval, a.ask, a.evidence, a.test)) or not a.link:
                r = B.update(c, a.id, a.by, status=a.status, due=a.due, owner_bot=a.owner, note=a.note, ask=a.ask,
                                evidence=a.evidence, test_results=a.test,
                                approval_needed=None if a.approval is None else a.approval == "yes")
            if a.link: r = B.add_link(c, a.id, a.by, a.link, a.kind)
        elif a.cmd == "act": r = B.act(c, a.id, a.action, a.by, a.reason)
        elif a.cmd == "approve":
            r = B.update(c, a.id, a.by, approved_by=a.approver, approval_source=a.source)
        elif a.cmd == "done":
            r = B.update(c, a.id, a.by, status="done", note=a.note, evidence=a.evidence, test_results=a.test)
            if a.link: r = B.add_link(c, a.id, a.by, a.link, a.kind)
        elif a.cmd == "get": r = B.get(c, a.id)
        elif a.cmd == "list": r = B.list_tasks(c, a.owner, a.status, a.overdue, a.stale, a.all, due_before=a.due_before, q=a.q, source=a.source, limit=a.limit, offset=a.offset)
        elif a.cmd == "flags": r = B.flags(c, a.stale, a.recent)
        elif a.cmd == "stale": r = B.stale(c, a.hours)
        elif a.cmd == "waiting": r = B.waiting_queue(c)
        elif a.cmd == "import":
            with open(a.file) as f: items = json.load(f)
            r = B.bulk_import(c, items, a.by, restore=True) if a.restore else B.bulk_import(c, items, a.by)
        elif a.cmd == "export": r = B.export(c, a.path)
    except (core.TaskError, OSError, ValueError, sqlite3.Error, HTTPException) as e:
        print(f"error: {e}", file=sys.stderr); return 2
    finally:
        if c is not None: c.close()
    for w in (r.get("warnings") or []) if isinstance(r, dict) else []: print(f"warning: {w}", file=sys.stderr)
    if a.json: print(json.dumps(r, indent=1)); return 0
    if a.cmd == "get" and a.short:
        print(core.short(r))
    elif a.cmd in ("list",):
        for t in r: print(core.short(t))
        print(f"{len(r)} task(s)")
    elif a.cmd == "waiting":
        for t in r: print(f"{t['id']}  {t['age_hours']}h  {t['owner_bot']}: {t['ask'] or '(no ask recorded) ' + t['title']}")
        print(f"{len(r)} waiting on {core.owner()}")
    elif a.cmd == "stale":
        for o, ts in r["by_owner"].items():
            print(f"{o} ({len(ts)})")
            for t in ts: print(f"  {t['id']}  {t['hours_since_note']}h  {t['title']}")
        print(f"{r['count']} stale (no note in {r['hours']:g}h)")
    elif a.cmd == "flags":
        for k in ("overdue", "stale", "blocked", "waiting_on_owner", "questions_for_owner", "created_recently"):
            print(f"{k}: {len(r[k])}")
            for t in r[k]: print(f"  {t['id']}  {t['owner_bot']}: {t['title']}")
    elif a.cmd in ("new", "update", "done", "approve", "act"):
        print(f"{r['id']} {r['status']}" + (" (existing)" if r.get("existing") else ""))  # quiet by default; --json for the full row
    else:
        print(json.dumps(r, indent=1))
    return 0

if __name__ == "__main__": sys.exit(main())
