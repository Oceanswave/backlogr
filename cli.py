"""backlogr CLI. Talks to the remote service when BACKLOGR_URL is set (token from BACKLOGR_TOKEN_FILE), else the local DB. Run `backlogr -h`."""
import argparse, json, os, sys, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

class Remote:
    """Mirrors the core.* calls used below over the authed /api/ endpoints."""
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token
    def _call(self, op, **a):
        req = urllib.request.Request(f"{self.url}/api/{op}", data=json.dumps(a).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            try: msg = json.load(e).get("error", str(e))
            except Exception: msg = str(e)
            raise core.TaskError(f"HTTP {e.code}: {msg}")
    def create(self, c, title, owner_bot, source, actor, done_means="", due=None, status="todo", approval_needed=False, note=None):
        return self._call("create", title=title, owner_bot=owner_bot, source=source, actor=actor, done_means=done_means,
                          due=due, status=status, approval_needed=approval_needed, note=note)
    def update(self, c, tid, actor, **kw): return self._call("update", id=tid, actor=actor, **kw)
    def add_link(self, c, tid, actor, ref, kind="other"): return self._call("add_link", id=tid, actor=actor, ref=ref, kind=kind)
    def get(self, c, tid): return self._call("get", id=tid)
    def list_tasks(self, c, owner=None, status=None, overdue=False, stale_hours=None, include_closed=False):
        return self._call("list", owner=owner, status=status, overdue=overdue, stale_hours=stale_hours, include_closed=include_closed)
    def flags(self, c, stale_hours=24, created_within_hours=24):
        return self._call("flags", stale_hours=stale_hours, created_within_hours=created_within_hours)
    def bulk_import(self, c, items, actor): return self._call("import", items=items, actor=actor)
    def export(self, c, path=None):
        data = self._call("export_data"); path = path or core.EXPORT_PATH; tmp = path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(tmp, "w") as f: json.dump(data, f, indent=1)
        os.replace(tmp, path); return {"path": path, "count": len(data["tasks"]), "source": self.url}

def backend():
    url = os.environ.get("BACKLOGR_URL")
    if not url: return core, core.connect()
    tf = os.environ.get("BACKLOGR_TOKEN_FILE", os.path.expanduser("~/backlogr/.token"))
    return Remote(url, open(tf).read().strip()), None

def main(argv=None):
    p = argparse.ArgumentParser(prog="backlogr", description="Backlogr CLI (no task, no work).")
    p.add_argument("--json", action="store_true", help="JSON output")
    s = p.add_subparsers(dest="cmd", required=True)
    n = s.add_parser("new"); n.add_argument("title"); n.add_argument("--owner", required=True)
    n.add_argument("--source", required=True, choices=core.SOURCES); n.add_argument("--by", required=True, help="actor bot")
    n.add_argument("--done-means", default=""); n.add_argument("--due"); n.add_argument("--status", default="todo", choices=core.STATUSES)
    n.add_argument("--approval", action="store_true"); n.add_argument("--note")
    u = s.add_parser("update"); u.add_argument("id"); u.add_argument("--by", required=True)
    u.add_argument("--status", choices=core.STATUSES); u.add_argument("--due"); u.add_argument("--owner"); u.add_argument("--note")
    u.add_argument("--approval", choices=["yes", "no"]); u.add_argument("--link"); u.add_argument("--kind", default="other")
    d = s.add_parser("done"); d.add_argument("id"); d.add_argument("--by", required=True); d.add_argument("--note"); d.add_argument("--link"); d.add_argument("--kind", default="other")
    g = s.add_parser("get"); g.add_argument("id")
    l = s.add_parser("list"); l.add_argument("--owner"); l.add_argument("--status", choices=core.STATUSES)
    l.add_argument("--overdue", action="store_true"); l.add_argument("--stale", type=float, metavar="HOURS"); l.add_argument("--all", action="store_true")
    f = s.add_parser("flags"); f.add_argument("--stale", type=float, default=24); f.add_argument("--recent", type=float, default=24)
    i = s.add_parser("import"); i.add_argument("file"); i.add_argument("--by", required=True)
    s.add_parser("export").add_argument("--path")
    a = p.parse_args(argv); B, c = backend()
    try:
        if a.cmd == "new":
            r = B.create(c, a.title, a.owner, a.source, a.by, a.done_means, a.due, a.status, a.approval, a.note)
        elif a.cmd == "update":
            r = None
            if a.link: r = B.add_link(c, a.id, a.by, a.link, a.kind)
            if any(x is not None for x in (a.status, a.due, a.owner, a.note, a.approval)) or not a.link:
                r = B.update(c, a.id, a.by, status=a.status, due=a.due, owner_bot=a.owner, note=a.note,
                                approval_needed=None if a.approval is None else a.approval == "yes")
        elif a.cmd == "done":
            if a.link: B.add_link(c, a.id, a.by, a.link, a.kind)
            r = B.update(c, a.id, a.by, status="done", note=a.note)
        elif a.cmd == "get": r = B.get(c, a.id)
        elif a.cmd == "list": r = B.list_tasks(c, a.owner, a.status, a.overdue, a.stale, a.all)
        elif a.cmd == "flags": r = B.flags(c, a.stale, a.recent)
        elif a.cmd == "import": r = B.bulk_import(c, json.load(open(a.file)), a.by)
        elif a.cmd == "export": r = B.export(c, a.path)
    except core.TaskError as e:
        print(f"error: {e}", file=sys.stderr); return 2
    if a.json: print(json.dumps(r, indent=1)); return 0
    if a.cmd in ("list",):
        for t in r: print(f"{t['id']}  [{t['status']}]  {t['owner_bot']}: {t['title']}" + (f"  (due {t['due']})" if t['due'] else ""))
        print(f"{len(r)} task(s)")
    elif a.cmd == "flags":
        for k in ("overdue", "stale", "blocked", "waiting_on_sean", "created_recently"):
            print(f"{k}: {len(r[k])}")
            for t in r[k]: print(f"  {t['id']}  {t['owner_bot']}: {t['title']}")
    else:
        print(json.dumps(r, indent=1))
    return 0

if __name__ == "__main__": sys.exit(main())
