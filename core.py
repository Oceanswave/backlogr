"""Backlogr core: SQLite system of record for all bot work."""
import json, os, sqlite3, uuid, datetime as dt
from pathlib import Path

DB_PATH = os.environ.get("BACKLOGR_DB", os.path.expanduser("~/backlogr/data/backlogr.db"))
EXPORT_PATH = os.environ.get("BACKLOGR_EXPORT", os.path.expanduser("~/backlogr/exports/backlogr-export.json"))
STATUSES = ("todo", "in_progress", "waiting_on_sean", "blocked", "done", "declined")
SOURCES = ("direct_to_bot", "via_chukar", "bot_initiated")
OPEN = ("todo", "in_progress", "waiting_on_sean", "blocked")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, done_means TEXT NOT NULL DEFAULT '',
 owner_bot TEXT NOT NULL, due TEXT, status TEXT NOT NULL DEFAULT 'todo',
 source TEXT NOT NULL, approval_needed INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS updates(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
 ts TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS links(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
 ts TEXT NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'other');
CREATE TRIGGER IF NOT EXISTS updates_no_edit BEFORE UPDATE ON updates
 BEGIN SELECT RAISE(ABORT,'updates log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS updates_no_delete BEFORE DELETE ON updates
 BEGIN SELECT RAISE(ABORT,'updates log is append-only'); END;
CREATE INDEX IF NOT EXISTS t_status ON tasks(status);
CREATE INDEX IF NOT EXISTS t_owner ON tasks(owner_bot);
"""

def now():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")

def _parse(ts):
    d = dt.datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return d

def connect(path=None):
    path = path or DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=15000")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    return c

class TaskError(ValueError):
    pass

def _check(status=None, source=None, due=None):
    if status is not None and status not in STATUSES:
        raise TaskError(f"status must be one of {STATUSES}")
    if source is not None and source not in SOURCES:
        raise TaskError(f"source must be one of {SOURCES}")
    if due:
        try: _parse(due)
        except Exception: raise TaskError("due must be ISO 8601, e.g. 2026-10-25 or 2026-10-25T17:00-04:00")

def _new_id(c):
    while True:
        i = "T-" + uuid.uuid4().hex[:6]
        if not c.execute("SELECT 1 FROM tasks WHERE id=?", (i,)).fetchone():
            return i

def create(c, title, owner_bot, source, actor, done_means="", due=None, status="todo",
           approval_needed=False, note=None, links=None, created_at=None, id=None):
    if not title or not owner_bot or not actor:
        raise TaskError("title, owner_bot and actor are required")
    _check(status, source, due)
    ts = created_at or now()
    c.execute("BEGIN IMMEDIATE")
    try:
        tid = id or _new_id(c)
        c.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (tid, title, done_means or "", owner_bot, due, status, source,
                   int(bool(approval_needed)), ts, ts))
        c.execute("INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)",
                  (tid, ts, actor, note or f"created ({status})"))
        for l in links or []:
            c.execute("INSERT INTO links(task_id,ts,ref,kind) VALUES(?,?,?,?)",
                      (tid, ts, l["ref"], l.get("kind", "other")))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return get(c, tid)

def _require(c, tid):
    if not c.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
        raise TaskError(f"no task {tid}")

def update(c, tid, actor, status=None, due=None, owner_bot=None, note=None,
           approval_needed=None, title=None, done_means=None):
    if not actor: raise TaskError("actor is required")
    _check(status, None, due)
    _require(c, tid)
    cur = get(c, tid)
    changes, parts = {}, []
    for k, v in (("status", status), ("due", due), ("owner_bot", owner_bot),
                 ("title", title), ("done_means", done_means)):
        if v is not None and v != cur[k]:
            changes[k] = v; parts.append(f"{k}: {cur[k]} -> {v}")
    if approval_needed is not None and bool(approval_needed) != cur["approval_needed"]:
        changes["approval_needed"] = int(bool(approval_needed))
        parts.append(f"approval_needed -> {bool(approval_needed)}")
    if not changes and not note:
        raise TaskError("nothing to update: give a field change or a note")
    ts = now()
    c.execute("BEGIN IMMEDIATE")
    try:
        if changes:
            sets = ", ".join(f"{k}=?" for k in changes)
            c.execute(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
                      (*changes.values(), ts, tid))
        else:
            c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, tid))
        text = "; ".join(parts + ([note] if note else []))
        c.execute("INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)", (tid, ts, actor, text))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return get(c, tid)

def add_link(c, tid, actor, ref, kind="other"):
    if not ref: raise TaskError("ref (url or text) is required")
    _require(c, tid); ts = now()
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO links(task_id,ts,ref,kind) VALUES(?,?,?,?)", (tid, ts, ref, kind))
    c.execute("INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)", (tid, ts, actor, f"link added ({kind}): {ref}"))
    c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, tid))
    c.execute("COMMIT")
    return get(c, tid)

def _row(r):
    d = dict(r); d["approval_needed"] = bool(d["approval_needed"]); return d

def get(c, tid, with_log=True):
    r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not r: raise TaskError(f"no task {tid}")
    d = _row(r)
    if with_log:
        d["updates"] = [dict(ts=u["ts"], actor=u["actor"], note=u["note"]) for u in
                        c.execute("SELECT * FROM updates WHERE task_id=? ORDER BY seq", (tid,))]
        d["links"] = [dict(ref=l["ref"], kind=l["kind"], ts=l["ts"]) for l in
                      c.execute("SELECT * FROM links WHERE task_id=? ORDER BY seq", (tid,))]
    return d

def _overdue(t, ref):
    if not t["due"] or t["status"] not in OPEN: return False
    d = _parse(t["due"])
    if len(t["due"]) == 10:  # date only: overdue after end of that day
        d = d + dt.timedelta(days=1)
    return d < ref

def list_tasks(c, owner=None, status=None, overdue=False, stale_hours=None, include_closed=False, ref=None):
    ref = ref or dt.datetime.now().astimezone()
    q, a = "SELECT * FROM tasks WHERE 1=1", []
    if owner: q += " AND lower(owner_bot)=lower(?)"; a.append(owner)
    if status:
        sts = [status] if isinstance(status, str) else list(status)
        for s in sts: _check(status=s)
        q += f" AND status IN ({','.join('?'*len(sts))})"; a += sts
    elif not include_closed:
        q += f" AND status IN ({','.join('?'*len(OPEN))})"; a += list(OPEN)
    out = [_row(r) for r in c.execute(q + " ORDER BY COALESCE(due,'9999'), updated_at", a)]
    if overdue: out = [t for t in out if _overdue(t, ref)]
    if stale_hours is not None:
        lim = ref - dt.timedelta(hours=stale_hours)
        out = [t for t in out if t["status"] in OPEN and _parse(t["updated_at"]) < lim]
    return out

def flags(c, stale_hours=24, created_within_hours=24, ref=None):
    ref = ref or dt.datetime.now().astimezone()
    lim = ref - dt.timedelta(hours=created_within_hours)
    allt = [_row(r) for r in c.execute("SELECT * FROM tasks ORDER BY created_at")]
    return {
        "as_of": ref.isoformat(timespec="seconds"),
        "overdue": list_tasks(c, overdue=True, ref=ref),
        "stale": list_tasks(c, stale_hours=stale_hours, ref=ref),
        "blocked": list_tasks(c, status="blocked", ref=ref),
        "waiting_on_sean": list_tasks(c, status="waiting_on_sean", ref=ref),
        "created_recently": [t for t in allt if _parse(t["created_at"]) >= lim],
    }

def bulk_import(c, items, actor):
    created, errors = [], []
    for i, it in enumerate(items):
        try:
            it = dict(it); links = it.pop("links", None)
            created.append(create(c, actor=actor, links=links, **it)["id"])
        except Exception as e:
            errors.append({"index": i, "title": it.get("title"), "error": str(e)})
    return {"created": created, "errors": errors}

def export(c, path=None):
    path = path or EXPORT_PATH
    data = {"exported_at": now(), "tasks": [get(c, r["id"]) for r in c.execute("SELECT id FROM tasks ORDER BY created_at")]}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, indent=1)
    os.replace(tmp, path)
    return {"path": path, "count": len(data["tasks"])}
