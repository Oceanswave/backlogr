"""Backlogr core: SQLite system of record for all bot work."""
import difflib, json, os, re, sqlite3, uuid, datetime as dt
from pathlib import Path
from contextlib import contextmanager

__version__ = "1.8.0"
SCHEMA_VERSION = 2

def owner():
    return os.environ.get("BACKLOGR_OWNER", "owner")

DB_PATH = os.environ.get("BACKLOGR_DB", os.path.expanduser("~/backlogr/data/backlogr.db"))
EXPORT_PATH = os.environ.get("BACKLOGR_EXPORT", os.path.expanduser("~/backlogr/exports/backlogr-export.json"))
STATUSES = ("todo", "in_progress", "waiting_on_sean", "blocked", "done", "declined")
SOURCES = ("direct_to_bot", "via_chukar", "bot_initiated")
OPEN = ("todo", "in_progress", "waiting_on_sean", "blocked")

SCHEMA = """
CREATE TABLE IF NOT EXISTS playbooks(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, skill TEXT NOT NULL DEFAULT '',
 test TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rules(
 id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', rule TEXT NOT NULL CHECK(length(trim(rule)) > 0),
 test TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
 testable INTEGER NOT NULL CHECK(testable IN (0,1)), category TEXT NOT NULL DEFAULT '',
 applies_to TEXT NOT NULL DEFAULT '', playbook_id TEXT REFERENCES playbooks(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL,
 CHECK(testable=0 OR (length(trim(test)) > 0 AND upper(substr(trim(test),1,8)) != 'UNTESTED')));
CREATE TABLE IF NOT EXISTS playbook_rules(
 playbook_id TEXT NOT NULL REFERENCES playbooks(id), rule_id TEXT NOT NULL REFERENCES rules(id),
 PRIMARY KEY(playbook_id, rule_id));
CREATE INDEX IF NOT EXISTS rule_playbooks ON playbook_rules(rule_id);
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

# Columns added after 1.0.0. connect() adds any that are missing (ALTER TABLE ADD COLUMN), so
# restarting the server migrates an old DB in place. Never remove or rename a column here.
ADDED_COLUMNS = [
    ("ask", "TEXT"),            # exact one-line ask for the owner while waiting_on_sean
    ("waiting_since", "TEXT"),  # when the row last entered waiting_on_sean
    ("approved_by", "TEXT"),    # the human who approved (never a bot)
    ("approved_at", "TEXT"),    # when they approved
    ("approval_source", "TEXT"),  # where, e.g. "Operator in chat"
    ("evidence", "TEXT"),       # what proves done: link, sha, path or command output summary
    ("test_results", "TEXT"),   # JSON {test name: "pass"|"fail"} when done_means mentions tests
    ("key", "TEXT"),            # caller's stable idempotency key; unique when set
    ("open_question", "TEXT"),  # a human's question for the owner bot (act question); cleared when the owner updates
    ("last_act", "TEXT"),       # JSON snapshot taken by the last act(), used by act undo
]
SIMILAR = 0.8  # title similarity above this triggers a duplicate warning on create

def _migrate(c):
    have = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    for name, typ in ADDED_COLUMNS:
        if name not in have:
            try: c.execute(f"ALTER TABLE tasks ADD COLUMN {name} {typ}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e): raise
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS t_key ON tasks(key) WHERE key IS NOT NULL")

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
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript(SCHEMA)
        _migrate(c)
        if c.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    except Exception:
        c.close()
        raise
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

def _need_ask(ask):
    if not (ask or "").strip():
        raise TaskError(f"ask is required when status is waiting_on_sean: one line saying exactly what {owner()} must decide or do")

def _bots(c, *extra):
    """Names that can never approve: BACKLOGR_BOTS, every owner and actor seen in the DB, plus extra (actor, owner),
    minus BACKLOGR_HUMANS (people who may act on tasks themselves without becoming 'bots')."""
    names = {x.strip().lower() for x in os.environ.get("BACKLOGR_BOTS", "").split(",")}
    names |= {(r[0] or "").strip().lower() for r in c.execute("SELECT DISTINCT owner_bot FROM tasks")}
    names |= {(r[0] or "").strip().lower() for r in c.execute("SELECT DISTINCT actor FROM updates")}
    names |= {(x or "").strip().lower() for x in extra}
    return names - {""} - _humans()

def _last_seq(c, tid):
    return c.execute("SELECT MAX(seq) FROM updates WHERE task_id=?", (tid,)).fetchone()[0]

def _humans():
    return {x.strip().lower() for x in os.environ.get("BACKLOGR_HUMANS", "").split(",") if x.strip()}

def _check_approver(c, approved_by, *bots):
    who = (approved_by or "").strip()
    if not who: raise TaskError("approved_by is required: the human who approved")
    if who.lower() in _bots(c, *bots):
        raise TaskError(f"approved_by {who!r} is a bot; bot-relayed approval never counts")
    approvers = {x.strip().lower() for x in os.environ.get("BACKLOGR_APPROVERS", "").split(",") if x.strip()}
    if approvers and who.lower() not in approvers:
        raise TaskError(f"approved_by must be one of BACKLOGR_APPROVERS, not {who!r}")
    return who

def _approval(c, approved_by, approval_source, *bots):
    who = _check_approver(c, approved_by, *bots)
    src = (approval_source or "").strip()
    if not src: raise TaskError(f'approval_source is required with approved_by, e.g. "{owner()} in chat"')
    return who, src

def _gate(c, status, need, approved_by, *bots):
    """approval_needed rows cannot be in_progress or done without a valid human approver (rechecked every time)."""
    if need and status in ("in_progress", "done"):
        if not approved_by:
            raise TaskError(f"approval_needed: record a human approval (approved_by, approval_source) before moving to {status}")
        _check_approver(c, approved_by, *bots)

def _tests(test_results):
    """Accept {name: pass|fail} or ["name=pass", ...]; return a clean dict."""
    if not test_results: return {}
    items = test_results.items() if isinstance(test_results, dict) else (str(x).rsplit("=", 1) if "=" in str(x) else (x, "") for x in test_results)
    out = {}
    for k, v in items:
        k, v = str(k).strip(), str(v).strip().lower()
        if not k or v not in ("pass", "fail"):
            raise TaskError(f"test results must be name=pass|fail, got {k!r}={v!r}")
        out[k] = v
    return out

def _done_gate(done_means, evidence, tests):
    if not (evidence or "").strip():
        raise TaskError("evidence is required to mark done: a link, commit sha, path or command output summary")
    if re.search(r"\btests?\b", done_means or "", re.I) and not tests:
        raise TaskError("done_means mentions tests: record pass/fail per test (test_results / --test name=pass|fail)")

def _tests_note(tests):
    if not tests: return []
    p = sum(v == "pass" for v in tests.values())
    return [f"tests: {p} pass, {len(tests) - p} fail" + "".join(f"; FAIL {k}" for k, v in tests.items() if v == "fail")]

def similar(c, title, exclude=None, threshold=SIMILAR):
    """Open tasks whose title is more than `threshold` similar to `title` (difflib ratio, case-insensitive)."""
    t = (title or "").strip().lower(); out = []
    for r in c.execute(f"SELECT id, title FROM tasks WHERE status IN ({','.join('?'*len(OPEN))})", OPEN):
        if r["id"] == exclude: continue
        score = difflib.SequenceMatcher(None, t, r["title"].strip().lower()).ratio()
        if score > threshold: out.append(dict(id=r["id"], title=r["title"], similarity=round(score, 2)))
    return sorted(out, key=lambda x: -x["similarity"])

def create(c, title, owner_bot, source, actor, done_means="", due=None, status="todo",
           approval_needed=False, note=None, links=None, created_at=None, id=None, ask=None,
           approved_by=None, approval_source=None, approved_at=None, evidence=None, test_results=None, key=None):
    """Create a task. With `key`, an existing row with that key is returned unchanged (existing=True).
    The result carries `warnings` when open tasks have a title more than SIMILAR alike."""
    if not title or not owner_bot or not actor:
        raise TaskError("title, owner_bot and actor are required")
    _check(status, source, due)
    if status == "waiting_on_sean": _need_ask(ask)
    tests = _tests(test_results); evidence = (evidence or "").strip() or None
    ts = created_at or now(); key = (key or "").strip() or None
    c.execute("BEGIN IMMEDIATE")
    try:
        if key:
            hit = c.execute("SELECT id FROM tasks WHERE key=?", (key,)).fetchone()
            if hit:
                c.execute("COMMIT"); r = get(c, hit["id"]); r["existing"] = True; return r
        dupes = similar(c, title)
        if approved_by is not None or approval_source:
            approved_by, approval_source = _approval(c, approved_by, approval_source, actor, owner_bot)
        _gate(c, status, approval_needed, approved_by, actor, owner_bot)
        if status == "done": _done_gate(done_means, evidence, tests)
        tid = id or _new_id(c)
        row = dict(id=tid, title=title, done_means=done_means or "", owner_bot=owner_bot, due=due, status=status,
                   source=source, approval_needed=int(bool(approval_needed)), created_at=ts, updated_at=ts,
                   ask=(ask or "").strip() or None, waiting_since=ts if status == "waiting_on_sean" else None,
                   approved_by=approved_by, approved_at=(approved_at or ts) if approved_by else None,
                   approval_source=approval_source if approved_by else None,
                   evidence=evidence, test_results=json.dumps(tests) if tests else None, key=key)
        c.execute(f"INSERT INTO tasks({','.join(row)}) VALUES({','.join('?'*len(row))})", tuple(row.values()))
        c.execute("INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)",
                  (tid, ts, actor, note or f"created ({status})"))
        for l in links or []:
            c.execute("INSERT INTO links(task_id,ts,ref,kind) VALUES(?,?,?,?)",
                      (tid, ts, l["ref"], l.get("kind", "other")))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    r = get(c, tid); r["existing"] = False
    if dupes: r["warnings"] = [f"similar title: {d['id']} {d['title']!r} ({d['similarity']})" for d in dupes]
    return r

def _require(c, tid):
    if not c.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
        raise TaskError(f"no task {tid}")

def update(c, tid, actor, status=None, due=None, owner_bot=None, note=None,
           approval_needed=None, title=None, done_means=None, ask=None,
           approved_by=None, approval_source=None, approved_at=None, evidence=None, test_results=None, _attest=False, _extra=None, _expect=None):
    if not actor: raise TaskError("actor is required")
    _check(status, None, due)
    tests = _tests(test_results); evidence = (evidence or "").strip() or None
    # validate and write under one write lock so concurrent updates cannot slip past the gates
    own_transaction = not c.in_transaction
    if own_transaction: c.execute("BEGIN IMMEDIATE")
    try:
        _require(c, tid)
        cur = get(c, tid)
        if _expect is not None and _last_seq(c, tid) != _expect:
            raise TaskError("task changed since it was read; retry")
        ask = (ask or "").strip() or None
        entering_wait = status == "waiting_on_sean" and cur["status"] != "waiting_on_sean"
        if entering_wait: _need_ask(ask)
        changes, parts = {}, []
        owners = (actor, cur["owner_bot"], owner_bot)
        if approved_by is not None or approval_source:
            who, src = _approval(c, approved_by, approval_source, *owners)
            changes.update(approved_by=who, approved_at=approved_at or now(), approval_source=src)
            parts.append(f"approved by {who} ({src})")
        # Either the current or the requested flag counts, so clearing approval_needed is itself gated.
        need = cur["approval_needed"] or bool(approval_needed)
        if cur["approval_needed"] and approval_needed is not None and not approval_needed:
            # clearing the requirement needs the same human approval, whatever the status
            _gate(c, "done", True, changes.get("approved_by") or cur.get("approved_by"), *owners)
        if not _attest and ((status is not None and status != cur["status"]) or approval_needed is not None):
            _gate(c, status or cur["status"], need, changes.get("approved_by") or cur.get("approved_by"), *owners)
        # check the resulting row (supplied values over stored ones) on entering done or when a done row's criteria change
        if (status or cur["status"]) == "done" and (cur["status"] != "done" or done_means is not None):
            _done_gate("" if _attest else (done_means if done_means is not None else cur["done_means"]),
                       evidence or cur.get("evidence"), tests or cur.get("test_results"))
        if evidence: changes["evidence"] = evidence; parts.append(f"evidence: {evidence}")
        if tests: changes["test_results"] = json.dumps(tests); parts += _tests_note(tests)
        if entering_wait: changes["waiting_since"] = now()
        if _extra: changes.update(_extra)
        # any update by the owner answers an open question and ends the undo window of a human act
        if not _extra and (actor or "").strip().lower() == cur["owner_bot"].strip().lower():
            if cur.get("open_question"): changes["open_question"] = None
        if not _extra and cur.get("last_act"): changes["last_act"] = None
        for k, v in (("status", status), ("due", due), ("owner_bot", owner_bot),
                     ("title", title), ("done_means", done_means), ("ask", ask)):
            if v is not None and v != cur[k]:
                changes[k] = v; parts.append(f"{k}: {cur[k]} -> {v}")
        if approval_needed is not None and bool(approval_needed) != cur["approval_needed"]:
            changes["approval_needed"] = int(bool(approval_needed))
            parts.append(f"approval_needed -> {bool(approval_needed)}")
        if not changes and not note:
            raise TaskError("nothing to update: give a field change or a note")
        ts = now()
        if changes:
            sets = ", ".join(f"{k}=?" for k in changes)
            c.execute(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
                      (*changes.values(), ts, tid))
        else:
            c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, tid))
        text = "; ".join(([note] if note and _extra else []) + parts + ([note] if note and not _extra else []))  # act notes lead
        c.execute("INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)", (tid, ts, actor, text))
        if own_transaction: c.execute("COMMIT")
    except Exception:
        if own_transaction: c.execute("ROLLBACK")
        raise
    return get(c, tid)

def add_link(c, tid, actor, ref, kind="other"):
    if not ref: raise TaskError("ref (url or text) is required")
    ts = now()
    c.execute("BEGIN IMMEDIATE")
    try:
        _require(c, tid)
        c.execute("INSERT INTO links(task_id,ts,ref,kind) VALUES(?,?,?,?)", (tid, ts, ref, kind))
        c.execute("INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)", (tid, ts, actor, f"link added ({kind}): {ref}"))
        c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, tid))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return get(c, tid)

def _row(r):
    d = dict(r); d["approval_needed"] = bool(d["approval_needed"])
    if "test_results" in d: d["test_results"] = json.loads(d["test_results"]) if d["test_results"] else None
    return d

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

def _due_dt(due, end_of_day=False):
    d = _parse(due)
    if len(due) == 10 and end_of_day: d = d + dt.timedelta(days=1)
    return d

def list_tasks(c, owner=None, status=None, overdue=False, stale_hours=None, include_closed=False, ref=None,
               due_before=None, q=None, source=None, limit=None, offset=0):
    """Filters combine with AND. status: one status, a list, or "a,b". due_before: ISO date/time; rows
    without a due are excluded. q: case-insensitive text at the start of a word (so "PR" does not match "in_progress")
    in title, done_means, notes or link refs. Pagination applies after all filters; limit defaults to unlimited."""
    for name, value in (("limit", limit), ("offset", offset)):
        if (name == "offset" or value is not None) and (type(value) is not int or value < 0):
            raise TaskError(f"{name} must be a non-negative integer")
    ref = ref or dt.datetime.now().astimezone()
    sql, a = "SELECT * FROM tasks WHERE 1=1", []
    if owner: sql += " AND lower(owner_bot)=lower(?)"; a.append(owner)
    if status:
        sts = [x.strip() for x in status.split(",")] if isinstance(status, str) else list(status)
        sts = [x for x in sts if x]
        for x in sts: _check(status=x)
        sql += f" AND status IN ({','.join('?'*len(sts))})"; a += sts
    elif not include_closed:
        sql += f" AND status IN ({','.join('?'*len(OPEN))})"; a += list(OPEN)
    if source:
        _check(source=source); sql += " AND source=?"; a.append(source)
    sql += " ORDER BY COALESCE(due,'9999'), updated_at, id"
    if not (q or due_before or overdue or stale_hours is not None):
        return [_row(r) for r in c.execute(sql + " LIMIT ? OFFSET ?", (*a, -1 if limit is None else limit, offset))]
    out = [_row(r) for r in c.execute(sql, a)]
    if q:
        rx = re.compile(r"(?<![0-9A-Za-z_])" + re.escape(q), re.I)
        def hit(t):
            texts = [t["title"], t["done_means"]]
            texts += [r[0] for r in c.execute("SELECT note FROM updates WHERE task_id=?", (t["id"],))]
            texts += [r[0] for r in c.execute("SELECT ref FROM links WHERE task_id=?", (t["id"],))]
            return any(rx.search(x or "") for x in texts)
        out = [t for t in out if hit(t)]
    if due_before:
        _check(due=due_before); lim = _due_dt(due_before)
        out = [t for t in out if t["due"] and _due_dt(t["due"]) < lim]
    if overdue: out = [t for t in out if _overdue(t, ref)]
    if stale_hours is not None:
        lim = ref - dt.timedelta(hours=stale_hours)
        out = [t for t in out if t["status"] in OPEN and _parse(t["updated_at"]) < lim]
    return out[offset:] if limit is None else out[offset:offset + limit]

def short(t):
    """One-line summary of a task row."""
    s = f"{t['id']}  [{t['status']}]  {t['owner_bot']}: {t['title']}"
    if t.get("due"): s += f"  (due {t['due']})"
    return s

def sean_queue(c, ref=None):
    """Everything waiting on the owner, oldest first, each with its exact ask and age in hours."""
    ref = ref or dt.datetime.now().astimezone()
    out = []
    for r in c.execute("SELECT * FROM tasks WHERE status='waiting_on_sean'"):
        t = _row(r); since = t.get("waiting_since") or t["updated_at"]
        out.append(dict(id=t["id"], title=t["title"], owner_bot=t["owner_bot"], ask=t.get("ask") or None,
                        since=since, age_hours=round((ref - _parse(since)).total_seconds() / 3600, 1), due=t["due"]))
    out.sort(key=lambda x: -x["age_hours"])
    return out

def stale(c, hours=2, ref=None):
    """in_progress rows with no log entry (note or change) in the last N hours, grouped by owner, oldest first."""
    ref = ref or dt.datetime.now().astimezone()
    lim = ref - dt.timedelta(hours=hours)
    groups = {}
    for r in c.execute("SELECT t.*, (SELECT ts FROM updates u WHERE u.task_id=t.id ORDER BY seq DESC LIMIT 1) AS last_note_at"
                       " FROM tasks t WHERE status='in_progress'"):
        t = _row(r); last = _parse(t["last_note_at"] or t["updated_at"])
        if last < lim:
            t["hours_since_note"] = round((ref - last).total_seconds() / 3600, 1)
            groups.setdefault(t["owner_bot"], []).append(t)
    for v in groups.values(): v.sort(key=lambda t: -t["hours_since_note"])
    return {"as_of": ref.isoformat(timespec="seconds"), "hours": hours,
            "count": sum(len(v) for v in groups.values()), "by_owner": dict(sorted(groups.items()))}

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
        "questions_for_owner": [t for t in allt if t.get("open_question") and t["status"] in OPEN],
    }

def bulk_import(c, items, actor, *, restore=False):
    if isinstance(items, dict): items = items.get("tasks")
    if not isinstance(items, list): raise TaskError("import requires a list or an object with tasks")
    created, skipped, errors = [], [], []
    columns = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    for i, item in enumerate(items):
        try:
            it = dict(item)
            if it.get("id") and c.execute("SELECT 1 FROM tasks WHERE id=?", (it["id"],)).fetchone():
                skipped.append(it["id"]); continue
            if not restore or "updates" not in it:
                # Snapshot metadata is not creation input; create enforces all gates.
                for field in ("updates", "updated_at", "waiting_since", "open_question", "last_act"):
                    it.pop(field, None)
                created.append(create(c, actor=actor, **it)["id"])
                continue
            # Trusted local restore only: preserve fields and append-only history verbatim.
            _check(it.get("status"), it.get("source"), it.get("due"))
            row = {k: v for k, v in it.items() if k in columns}
            if isinstance(row.get("test_results"), dict): row["test_results"] = json.dumps(row["test_results"])
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(f"INSERT INTO tasks({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
                for u in it["updates"]:
                    c.execute("INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)", (it["id"], u["ts"], u["actor"], u["note"]))
                for l in it.get("links", []):
                    c.execute("INSERT INTO links(task_id,ts,ref,kind) VALUES(?,?,?,?)", (it["id"], l["ts"], l["ref"], l["kind"]))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK"); raise
            created.append(it["id"])
        except Exception as e:
            errors.append({"index": i, "title": item.get("title") if isinstance(item, dict) else None, "error": str(e)})
    return {"created": created, "skipped": skipped, "errors": errors}

def export(c, path=None):
    path = path or EXPORT_PATH
    data = {"exported_at": now(), "tasks": [get(c, r["id"]) for r in c.execute("SELECT id FROM tasks ORDER BY created_at")]}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f, indent=1)
    os.replace(tmp, path)
    return {"path": path, "count": len(data["tasks"])}

# ---- act: one-call human actions (dashboard buttons) ----
ACTS = ("done", "na", "question", "later", "reply", "undo")
_ACT_FIELDS = ("status", "evidence", "test_results", "approved_by", "approved_at", "approval_source", "open_question")

def act(c, tid, action, by=None, reason=None):
    """done | na | question | later | reply | undo, taken by a person (default BACKLOGR_OWNER). Notes start with "act <action>" in the log.
    done: status done. If `by` is in BACKLOGR_HUMANS (and BACKLOGR_APPROVERS, when set) their word is the evidence,
    the tests gate is skipped and their approval is recorded if needed; otherwise the normal gates apply.
    na: status declined, reason in the note. question: status unchanged, open_question set for the owner bot.
    later: status unchanged, reason recorded as a note.
    undo: restores the fields the last act changed, if nothing else touched the task since."""
    if action not in ACTS: raise TaskError(f"action must be one of {ACTS}")
    by = (by or "").strip() or owner(); reason = (reason or "").strip() or None
    seq = _last_seq(c, tid)  # read first: any write after this makes update() refuse the act
    cur = get(c, tid, with_log=False)
    if action == "undo":
        if not cur.get("last_act"): raise TaskError("nothing to undo: no act since the last change")
        last = c.execute("SELECT note FROM updates WHERE seq=?", (seq,)).fetchone()
        if not last or not last[0].startswith("act ") or last[0].startswith("act undo"):
            raise TaskError("nothing to undo: the task changed after the last act")
        la = json.loads(cur["last_act"]); before = la["before"]
        extra = {k: (json.dumps(v) if k == "test_results" and isinstance(v, dict) else v) for k, v in before.items()}
        extra["last_act"] = None
        return update(c, tid, by, _expect=seq, note=f"act undo of {la['action']} by {by}: status {cur['status']} -> {before['status']}",
                      _extra=extra)
    before = {k: cur.get(k) for k in _ACT_FIELDS}
    snap = lambda: json.dumps({"action": action, "by": by, "before": before})
    if action == "done":
        if cur["status"] == "done": raise TaskError("already done")
        human = by.lower() in _humans()
        if human:
            try: _check_approver(c, by)
            except TaskError: human = False
        extra = {"last_act": snap(), "open_question": None}
        note = f"act done by {by}" + (f": {reason}" if reason else "")
        if human and cur["approval_needed"] and not cur.get("approved_by"):
            extra.update(approved_by=by, approved_at=now(), approval_source=f"{by} via act done")
            note += f"; approved by {by}"
        return update(c, tid, by, status="done", note=note, _attest=human, _extra=extra, _expect=seq,
                      evidence=f"confirmed done by {by}" + (f": {reason}" if reason else ""))
    if action == "na":
        if cur["status"] == "declined": raise TaskError("already declined")
        return update(c, tid, by, status="declined", note=f"act na by {by}" + (f": {reason}" if reason else ""),
                      _extra={"last_act": snap()}, _expect=seq)
    if not reason: raise TaskError(f"{action} text is required (--reason)")
    if action == "reply":
        if len(reason) > 4000: raise TaskError("reply text must be at most 4000 characters")
        return update(c, tid, by, note=f"act reply by {by}: {reason}",
                      _extra={"last_act": snap(), "open_question": reason}, _expect=seq)
    if action == "later":
        return update(c, tid, by, note=f"act later by {by}: {reason}",
                      _extra={"last_act": snap()}, _expect=seq)
    return update(c, tid, by, note=f"act question by {by} for {cur['owner_bot']}: {reason}",
                  _extra={"last_act": snap(), "open_question": reason}, _expect=seq)


# Rules and playbooks share savepoints so bulk imports are atomic, including links.
@contextmanager
def _rules_transaction(c):
    outer = not c.in_transaction
    c.execute('BEGIN IMMEDIATE' if outer else 'SAVEPOINT rules_write')
    try:
        yield
        c.execute('COMMIT' if outer else 'RELEASE rules_write')
    except Exception:
        if outer: c.execute('ROLLBACK')
        else:
            c.execute('ROLLBACK TO rules_write')
            c.execute('RELEASE rules_write')
        raise


def _text(value, field, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise TaskError(f'{field} must be a nonempty string' if required else f'{field} must be a string')
    return value


def rule_get(c, id):
    _text(id, 'id', True)
    row = c.execute('SELECT * FROM rules WHERE id=?', (id,)).fetchone()
    if row is None: raise TaskError(f'unknown rule: {id}')
    result = dict(row)
    result['testable'] = bool(result['testable'])
    return result


def rule_list(c, playbook=None, testable=None, category=None):
    if testable is not None and type(testable) is not bool: raise TaskError('testable must be a boolean')
    if playbook is not None: _text(playbook, 'playbook', True)
    if category is not None: _text(category, 'category')
    clauses, values = [], []
    if playbook is not None:
        clauses.append('id IN (SELECT rule_id FROM playbook_rules WHERE playbook_id=?)'); values.append(playbook)
    if testable is not None:
        clauses.append('testable=?'); values.append(testable)
    if category is not None:
        clauses.append('category=?'); values.append(category)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    return [rule_get(c, row[0]) for row in c.execute('SELECT id FROM rules' + where + ' ORDER BY id', values)]


def rule_untested(c):
    """Return every rule whose testable flag is false."""
    return rule_list(c, testable=False)


def rule_add(c, id, rule, title='', test='', evidence='', source='', testable=None,
             category='', applies_to='', playbook_id=None, actor='owner', replace=False):
    fields = dict(id=id, title=title, rule=rule, test=test, evidence=evidence, source=source,
                  category=category, applies_to=applies_to)
    for field, value in fields.items(): _text(value, field, field in ('id', 'rule'))
    _text(actor, 'actor', True)
    if testable is not None and type(testable) is not bool: raise TaskError('testable must be a boolean')
    if type(replace) is not bool: raise TaskError('replace must be a boolean')
    if testable is True and not test.strip(): raise TaskError('testable=true requires a test')
    fields['testable'] = bool(test.strip() and not test.strip().upper().startswith('UNTESTED') and testable is not False)
    if playbook_id is not None: _text(playbook_id, 'playbook_id', True)
    fields['playbook_id'] = playbook_id
    with _rules_transaction(c):
        if playbook_id is not None: playbook_get(c, playbook_id)
        exists = c.execute('SELECT id FROM rules WHERE id=?', (id,)).fetchone()
        old = rule_get(c, id) if exists else None
        if old and not replace: raise TaskError(f'duplicate rule id: {id}; use replace')
        if old and all(old[k] == v for k, v in fields.items()): return old
        ts = now()
        if old:
            assignments = ','.join(f'{k}=?' for k in fields if k != 'id')
            c.execute(f'UPDATE rules SET {assignments},updated_at=? WHERE id=?',
                      [v for k, v in fields.items() if k != 'id'] + [ts, id])
            if old['playbook_id'] and old['playbook_id'] != playbook_id:
                c.execute('DELETE FROM playbook_rules WHERE playbook_id=? AND rule_id=?', (old['playbook_id'], id))
                c.execute('UPDATE playbooks SET updated_at=? WHERE id=?', (ts, old['playbook_id']))
        else:
            fields.update(created_at=ts, updated_at=ts, created_by=actor)
            c.execute(f"INSERT INTO rules ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", list(fields.values()))
        if playbook_id:
            added = c.execute('INSERT OR IGNORE INTO playbook_rules VALUES (?,?)', (playbook_id, id)).rowcount
            if added: c.execute('UPDATE playbooks SET updated_at=? WHERE id=?', (ts, playbook_id))
        return rule_get(c, id)


def playbook_get(c, id):
    _text(id, 'id', True)
    row = c.execute('SELECT * FROM playbooks WHERE id=?', (id,)).fetchone()
    if row is None: raise TaskError(f'unknown playbook: {id}')
    result = dict(row)
    result['rule_ids'] = [r[0] for r in c.execute('SELECT rule_id FROM playbook_rules WHERE playbook_id=? ORDER BY rule_id', (id,))]
    result['rules'] = [rule_get(c, rid) for rid in result['rule_ids']]
    return result


def playbook_list(c):
    return [playbook_get(c, r[0]) for r in c.execute('SELECT id FROM playbooks ORDER BY id')]


def playbook_add(c, id, name, skill='', test='', description='', rule_ids=None):
    fields = dict(id=id, name=name, skill=skill, test=test, description=description)
    for field, value in fields.items(): _text(value, field, field in ('id', 'name'))
    if rule_ids is not None and (not isinstance(rule_ids, list) or any(not isinstance(r, str) for r in rule_ids)):
        raise TaskError('rule_ids must be a list of strings')
    with _rules_transaction(c):
        exists = c.execute('SELECT id FROM playbooks WHERE id=?', (id,)).fetchone()
        old = playbook_get(c, id) if exists else None
        # Primary membership always implies a link; explicit links can span playbooks.
        primary = [r[0] for r in c.execute('SELECT id FROM rules WHERE playbook_id=?', (id,))]
        ids = sorted(set((rule_ids if rule_ids is not None else (old['rule_ids'] if old else [])) + primary))
        for rid in ids: rule_get(c, rid)
        if old and all(old[k] == v for k, v in fields.items()) and old['rule_ids'] == ids: return old
        ts = now()
        c.execute('''INSERT INTO playbooks(id,name,skill,test,description,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name,skill=excluded.skill,test=excluded.test,
        description=excluded.description,updated_at=excluded.updated_at''', (id, name, skill, test, description, ts, ts))
        c.execute('DELETE FROM playbook_rules WHERE playbook_id=?', (id,))
        c.executemany('INSERT INTO playbook_rules VALUES (?,?)', [(id, rid) for rid in ids])
        return playbook_get(c, id)


def rules_import(c, items, actor):
    """Atomically upsert a rules/playbooks object; counts are per record, excluding embedded rules."""
    _text(actor, 'actor', True)
    if not isinstance(items, dict) or set(items) - {'rules', 'playbooks'}: raise TaskError('expected rules/playbooks object')
    for key in ('rules', 'playbooks'):
        rows = items.get(key, [])
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows): raise TaskError(f'{key} must be a list of objects')
        ids = [r.get('id') for r in rows]
        if any(not isinstance(i, str) or not i.strip() for i in ids) or len(set(ids)) != len(ids):
            raise TaskError(f'{key} requires unique nonempty ids')
    counts = dict(created=0, updated=0, unchanged=0)
    with _rules_transaction(c):
        before_rules = {r['id']: r for r in rule_list(c)}
        before_books = {p['id']: p for p in playbook_list(c)}
        try:
            # Create shells first to resolve rule -> playbook references in the same file.
            for p in items.get('playbooks', []):
                if p['id'] not in before_books:
                    playbook_add(c, **{k: v for k, v in p.items() if k != 'rule_ids'})
            for row in items.get('rules', []):
                r = dict(row)
                if 'playbook' in r:
                    alias = r.pop('playbook')
                    if 'playbook_id' in r and r['playbook_id'] != alias: raise TaskError('conflicting playbook references')
                    r['playbook_id'] = alias
                if isinstance(r.get('applies_to'), list):
                    if any(not isinstance(v, str) for v in r['applies_to']): raise TaskError('applies_to list must contain strings')
                    r['applies_to'] = json.dumps(r['applies_to'], ensure_ascii=False)
                rule_add(c, **r, actor=actor, replace=True)
            for p in items.get('playbooks', []): playbook_add(c, **p)
        except TypeError as e:
            raise TaskError(f'invalid import fields: {e}') from e
        for key, before, getter in (('rules', before_rules, rule_get), ('playbooks', before_books, playbook_get)):
            for row in items.get(key, []):
                old, new = before.get(row['id']), getter(c, row['id'])
                ignored = {'created_at', 'updated_at', 'created_by', 'rules'}
                changed = old and {k: v for k, v in old.items() if k not in ignored} != {k: v for k, v in new.items() if k not in ignored}
                counts['created' if old is None else 'updated' if changed else 'unchanged'] += 1
    return counts
