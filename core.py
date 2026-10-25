"""Backlogr core: SQLite system of record for all bot work."""
import base64, hashlib, hmac, secrets, time
from urllib.parse import urlencode
import difflib, json, os, re, sqlite3, uuid, datetime as dt
from pathlib import Path

DB_PATH = os.environ.get("BACKLOGR_DB", os.path.expanduser("~/backlogr/data/backlogr.db"))
EXPORT_PATH = os.environ.get("BACKLOGR_EXPORT", os.path.expanduser("~/backlogr/exports/backlogr-export.json"))
STATUSES = ("todo", "in_progress", "waiting_on_sean", "blocked", "done", "declined")
SOURCES = ("direct_to_bot", "via_chukar", "bot_initiated")
OPEN = ("todo", "in_progress", "waiting_on_sean", "blocked")

SCHEMA = """
CREATE TABLE IF NOT EXISTS used_links(
 nonce TEXT PRIMARY KEY, task_id TEXT NOT NULL, action TEXT NOT NULL, used_at TEXT NOT NULL);
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
    ("ask", "TEXT"),            # exact one-line ask for Sean while waiting_on_sean
    ("waiting_since", "TEXT"),  # when the row last entered waiting_on_sean
    ("approved_by", "TEXT"),    # the human who approved (never a bot)
    ("approved_at", "TEXT"),    # when they approved
    ("approval_source", "TEXT"),  # where, e.g. "Sean in Chukar chat 11:17"
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
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=15000")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    _migrate(c)
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
        raise TaskError("ask is required when status is waiting_on_sean: one line saying exactly what Sean must decide or do")

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
    if not src: raise TaskError('approval_source is required with approved_by, e.g. "Sean in Chukar chat 11:17"')
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
    _require(c, tid); ts = now()
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO links(task_id,ts,ref,kind) VALUES(?,?,?,?)", (tid, ts, ref, kind))
    c.execute("INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)", (tid, ts, actor, f"link added ({kind}): {ref}"))
    c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, tid))
    c.execute("COMMIT")
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
               due_before=None, q=None, source=None):
    """Filters combine with AND. status: one status, a list, or "a,b". due_before: ISO date/time; rows
    without a due are excluded. q: case-insensitive text at the start of a word (so "PR" does not match "in_progress")
    in title, done_means, notes or link refs."""
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
    out = [_row(r) for r in c.execute(sql + " ORDER BY COALESCE(due,'9999'), updated_at", a)]
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
    return out

def short(t):
    """One-line summary of a task row."""
    s = f"{t['id']}  [{t['status']}]  {t['owner_bot']}: {t['title']}"
    if t.get("due"): s += f"  (due {t['due']})"
    return s

def sean_queue(c, ref=None):
    """Everything waiting on Sean, oldest first, each with its exact ask and age in hours."""
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

# ---- act: one-call human actions (dashboard buttons) ----
LINK_ACTIONS = ("done", "na", "question", "later")
ACTS = (*LINK_ACTIONS, "undo")
_ACT_FIELDS = ("status", "evidence", "test_results", "approved_by", "approved_at", "approval_source", "open_question")

def act(c, tid, action, by="Sean", reason=None):
    """done | na | question | later | undo, taken by a person (default Sean). Notes start with "act <action>" in the log.
    done: status done. If `by` is in BACKLOGR_HUMANS (and BACKLOGR_APPROVERS, when set) their word is the evidence,
    the tests gate is skipped and their approval is recorded if needed; otherwise the normal gates apply.
    na: status declined, reason in the note. question: status unchanged, open_question set for the owner bot.
    later: status unchanged, reason recorded as a note.
    undo: restores the fields the last act changed, if nothing else touched the task since."""
    if action not in ACTS: raise TaskError(f"action must be one of {ACTS}")
    by = (by or "").strip() or "Sean"; reason = (reason or "").strip() or None
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
    if action == "later":
        return update(c, tid, by, note=f"act later by {by}: {reason}",
                      _extra={"last_act": snap()}, _expect=seq)
    return update(c, tid, by, note=f"act question by {by} for {cur['owner_bot']}: {reason}",
                  _extra={"last_act": snap(), "open_question": reason}, _expect=seq)


class LinkError(TaskError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _link_secret():
    secret = os.environ.get("BACKLOGR_LINK_SECRET", "")
    if not secret:
        raise LinkError(404, "Action links are disabled")
    return secret.encode()


def _link_sig(task, action, expiry, nonce):
    message = f"v1|{task}|{action}|{expiry}|{nonce}".encode()
    return base64.urlsafe_b64encode(hmac.new(_link_secret(), message, hashlib.sha256).digest()).decode().rstrip("=")


def sign_link(task, action, ttl_seconds=None, text=None):
    _link_secret()
    if action not in LINK_ACTIONS:
        raise TaskError("Invalid link action")
    public = os.environ.get("BACKLOGR_PUBLIC_URL", "").rstrip("/")
    if not public:
        raise TaskError("BACKLOGR_PUBLIC_URL is required for action links")
    try:
        ttl = int(ttl_seconds if ttl_seconds is not None else os.environ.get("BACKLOGR_LINK_TTL", "604800"))
    except (ValueError, TypeError):
        raise TaskError("Link TTL must be an integer")
    expiry, nonce = str(int(time.time()) + ttl), secrets.token_urlsafe(24)
    params = dict(t=task, a=action, e=expiry, n=nonce, s=_link_sig(task, action, expiry, nonce))
    if text is not None: params["text"] = text
    return public + "/act?" + urlencode(params)


def verify_link(c, params):
    _link_secret()
    task, action, expiry, nonce, sig = (params.get(k, "") for k in ("t", "a", "e", "n", "s"))
    expected = _link_sig(task, action, expiry, nonce)
    if not hmac.compare_digest(sig.encode(), expected.encode()) or action not in LINK_ACTIONS or not nonce:
        raise LinkError(403, "Invalid action link")
    try:
        expired = int(expiry) <= time.time()
    except ValueError:
        raise LinkError(403, "Invalid action link")
    if expired: raise LinkError(410, "This link has expired")
    if not c.execute("SELECT 1 FROM tasks WHERE id=?", (task,)).fetchone():
        raise LinkError(404, "Task not found")
    return get(c, task)


def use_link(c, params, text=None):
    c.execute("BEGIN IMMEDIATE")
    try:
        verify_link(c, params)
        try:
            c.execute("INSERT INTO used_links VALUES(?,?,?,?)",
                      (params["n"], params["t"], params["a"], now()))
        except sqlite3.IntegrityError:
            raise LinkError(409, "This link was already used")
        result = act(c, params["t"], params["a"],
                     by=os.environ.get("BACKLOGR_LINK_ACTOR", "").strip() or "owner", reason=text)
        c.execute("COMMIT")
        return result
    except Exception:
        c.execute("ROLLBACK")
        raise
