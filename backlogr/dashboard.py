"""Live, read-only Backlogr dashboard: tasks, timeline and rules rendered from SQLite on each request."""
import json
import logging
import os
import re
import zlib
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo
from html import escape
from urllib.parse import urlsplit

from . import core

# Display time zone: BACKLOGR_TZ (an IANA name such as Europe/Berlin), else the server's local zone.
TZ = ZoneInfo(os.environ["BACKLOGR_TZ"]) if os.environ.get("BACKLOGR_TZ") else datetime.now().astimezone().tzinfo


def bot(name):
    """Display name for an owner or actor."""
    return (name or "").strip() or "unassigned"


def ts(s):
    try:
        value = datetime.fromisoformat(s)
        return value if value.tzinfo else value.replace(tzinfo=TZ)
    except Exception: return datetime.min.replace(tzinfo=TZ)

BOILER = [r"(?:\b(?:approval_needed|due|owner_bot|title): [^;]*? -> [^;]*?; ?)+", r"Seeded from Bot Team Dashboard \([^)]*\)\.\s*(Detail:\s*)?", r"Backfilled \d+/\d+ from [^.]*?\([^)]*\)\.\s*",
          r"Source not recorded on dashboard; defaulted to via_\w+, verify with owner\.?", r"^status: \w+ -> \w+;\s*"]
def trunc(s, n=400):
    s = re.sub(r"\s+", " ", s or "").strip()
    for b in BOILER: s = re.sub(b, "", s).strip()
    if re.fullmatch(r"created \(\w+\)", s): s = ""
    if len(s) <= n: return s
    cut = s[:n]
    p = max(cut.rfind(". "), cut.rfind("; "))
    space = cut.rfind(" ")
    cut = cut[:p+1] if p > n * 0.6 else cut[:space] if space > 0 else cut
    return cut.rstrip(" ,;:") + "…"

def link(l):
    ref = l.get("ref", "")
    m = re.match(r"https://github\.com/[^/]+/([^/]+)/pull/(\d+)", ref)
    if m: return {"label": f"{m.group(1)} #{m.group(2)}", "href": ref}
    if ref.startswith("http"):
        lab = re.sub(r"^https?://(www\.)?", "", ref).rstrip("/")
        return {"label": lab[:48], "href": ref}
    return None

def fmt_due(d):
    if not d: return None
    try:
        dt = datetime.fromisoformat(d.replace(" ", "T"))
        return dt.strftime("%a %b %-d") + (dt.strftime(", %-I:%M %p") if ("T" in d or " " in d) else "")
    except Exception:
        return d

@lru_cache(maxsize=None)
def categories(path=None):
    """[(name, regex)] from the JSON file BACKLOGR_DASHBOARD_CATEGORIES (a list of [name, regex] pairs; first match
    wins, case-insensitive, on the title and then on notes and link refs). Without it, cards are not grouped."""
    path = path or os.environ.get("BACKLOGR_DASHBOARD_CATEGORIES")
    if not path: return ()
    with open(path) as f:
        pairs = tuple((str(name), str(rx)) for name, rx in json.load(f))
    for _, rx in pairs: re.compile(rx)
    return pairs


def category(t):
    hay = " ".join([t.get("title", "")] + [u.get("note", "") for u in t.get("updates", [])] + [l.get("ref", "") for l in t.get("links", [])])
    for name, rx in categories():
        if re.search(rx, t.get("title", ""), re.I): return name
    for name, rx in categories():
        if re.search(rx, hay, re.I): return name
    return "Other"


def item(t):
    b = bot(t.get("owner_bot"))
    title = t.get("title", "")
    title = re.sub(r"^\s*%s\s*:\s*" % re.escape(b), "", title, flags=re.I)
    notes = [u for u in t.get("updates", []) if u.get("note")]
    notes.sort(key=lambda u: ts(u.get("ts", "")))
    if t["status"] == "waiting_on_owner" and (t.get("ask") or "").strip():
        detail = trunc(t["ask"])  # the decision itself, rendered as "Ask: ..."
    else:
        detail = trunc(notes[-1]["note"], 200 if t["status"] == "done" else 400) if notes else ""
    seen, links = set(), []
    for l in t.get("links", []):
        x = link(l)
        if x and x["href"] not in seen:
            seen.add(x["href"]); links.append(x)
    it = {"id": t["id"], "title": title, "bot": b, "status": t["status"], "cat": category(t), "detail": detail.strip()}
    if links: it["links"] = links
    d = fmt_due(t.get("due"))
    if d: it["due"] = d
    return it


def load_rules(c):
    try:
        rules, unt, pbs = core.rule_list(c), core.rule_untested(c), core.playbook_list(c)
    except Exception:
        logging.exception("Dashboard rules load failed")
        return {"total": 0, "tested": 0, "untested": 0, "playbooks": [], "list": []}
    uids = {r["id"] for r in unt}
    rows = []
    for p in sorted(pbs, key=lambda p: p.get("name") or p["id"]):
        ids = set(p.get("rule_ids") or [r["id"] for r in rules if r.get("playbook_id") == p["id"]])
        rows.append({"name": p.get("name") or p["id"], "skill": p.get("skill") or "", "rules": len(ids), "untested": len(ids & uids)})
    loose = [r for r in rules if not r.get("playbook_id")]
    if loose: rows.append({"name": "(no playbook)", "skill": "", "rules": len(loose), "untested": sum(r["id"] in uids for r in loose)})
    nat = lambda i: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", i)]
    lst = [{"id": r["id"], "title": trunc(r.get("title", ""), 140), "playbook": r.get("playbook_id") or "(none)"} for r in sorted(unt, key=lambda r: (r.get("playbook_id") or "~", nat(r["id"])))]
    return {"total": len(rules), "tested": len(rules) - len(uids), "untested": len(uids), "playbooks": rows, "list": lst}


def load_data(c, now=None):
    """Build one request's model from a consistent SQLite read transaction."""
    now = now or datetime.now(TZ)
    with c:
        c.execute("BEGIN")
        tasks = [core.get(c, t['id']) for t in core.list_tasks(c, include_closed=True)]
        rules = load_rules(c)
        updates = c.execute("SELECT u.*, t.title, t.owner_bot FROM updates u "
                            "JOIN tasks t ON t.id = u.task_id ORDER BY u.seq DESC").fetchall()
    by_upd = lambda t: ts(t.get('updated_at', ''))
    waiting = sorted((t for t in tasks if t['status'] == 'waiting_on_owner'), key=by_upd, reverse=True)
    order = {'blocked': 0, 'in_progress': 1, 'todo': 2}
    progress = sorted((t for t in tasks if t['status'] in order), key=by_upd, reverse=True)
    progress.sort(key=lambda t: order[t['status']])
    done = sorted((t for t in tasks if t['status'] == 'done'), key=by_upd, reverse=True)
    events = []
    for u in updates:
        when = ts(u['ts'])
        if now - timedelta(hours=48) <= when <= now:
            actor = u['actor'].strip()
            note = trunc(u['note'], 160)
            if not note and u['note'].startswith('status:'):
                note = u['note'][:160]
            events.append((when, {'time': when.astimezone(TZ).strftime('%-I:%M %p'),
                                  'bot': bot(actor or u['owner_bot']),
                                  'text': f"{u['task_id']} {u['title']}: {note}"}))
    # The query's descending sequence is the stable tie-breaker for equal timestamps.
    events.sort(key=lambda e: e[0], reverse=True)
    days = []
    for when, event in events[:80]:
        day = when.astimezone(TZ).strftime('%a %b %-d')
        if not days or days[-1]['day'] != day:
            days.append({'day': day, 'events': []})
        days[-1]['events'].append(event)
    return {'AS_OF': now.astimezone(TZ).strftime('%a %b %-d, %Y, %-I:%M %p %Z'), 'OWNER': core.owner(),
            'TOTAL': len(tasks), 'DECLINED': sum(t['status'] == 'declined' for t in tasks),
            'OLDER_DONE': max(len(done) - 40, 0), 'WAITING': [item(t) for t in waiting],
            'PROGRESS': [item(t) for t in progress], 'DONE': [item(t) for t in done[:40]],
            'DAYS': days, 'RULES': rules, 'CATS': [c for c, _ in categories()] + ['Other']}


PALETTE = ['#6ea8fe', '#f2b84b', '#4cc38a', '#c38df2', '#f27e6b',
           '#4fc3d9', '#e86fb0', '#a3c45a', '#f29e4c', '#8a9cf2']
STATUS = {'waiting_on_owner': ('Waiting on owner', '#f2b84b'), 'blocked': ('Blocked', '#f26b6b'),
          'in_progress': ('In progress', '#6ea8fe'), 'todo': ('To do', '#7d8590'), 'done': ('Done', '#4cc38a')}
# Generic glyphs; each name gets a stable glyph and color from its hash.
GLYPHS = [
    '<circle cx="10" cy="8" r="4"/><path d="M4 17c1-4 11-4 12 0"/>',
    '<rect x="3" y="3" width="14" height="5" rx="1"/><rect x="3" y="11" width="14" height="5" rx="1"/>',
    '<path d="M3 8v4h3l7 4V4L6 8z"/>',
    '<path d="M4 8a9 9 0 0112 0M6.5 11a5 5 0 017 0"/><circle cx="10" cy="14.5" r="1.2" fill="currentColor"/>',
    '<path d="M3 5l4-2 6 2 4-2v12l-4 2-6-2-4 2z M7 3v12 M13 5v12"/>',
    '<path d="M6 4l9 6-9 6z"/>',
    '<rect x="4" y="3" width="12" height="14" rx="1"/><path d="M7 7h6M7 10h6M7 13h4"/>',
    '<path d="M3 17L10 3l7 14z M6.5 11h7"/>',
    '<circle cx="10" cy="10" r="7"/>',
    '<path d="M10 2c3 3 4 7 2 11H8C6 9 7 5 10 2z M8 13l-2 4 M12 13l2 4"/>',
]
CSS = '''
:root{color-scheme:dark;--bg:#0f1115;--elevated:#171a21;--border:#2a2f3a;--text:#e8eaed;--secondary:#b3b8c2;--tertiary:#7d8590;--accent:#6ea8fe;--warning:#f2b84b;--danger:#f26b6b;--success:#4cc38a}

*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif}
main{padding:24px;max-width:1800px;margin:auto}
h1{font-size:28px;margin:0 0 8px}
h2{font-size:20px;margin:0;font-weight:600}
p{margin:0}
a{color:var(--accent);font-size:12px;overflow-wrap:anywhere}
a:hover{text-decoration:none}
.secondary{color:var(--secondary)}
.muted{color:var(--tertiary);font-size:12px}
.mono{font-family:ui-monospace,monospace;font-size:12px;color:var(--tertiary)}
.row{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
.spread{justify-content:space-between}
.intro{color:var(--secondary);margin-bottom:12px}
.chips{margin-bottom:8px}
.assignees{margin-bottom:16px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--border);border-radius:8px}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor}
.bot{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;color:var(--secondary)}
.bot.big{font-size:14px}
.bot svg{flex-shrink:0}
.box{border:1px solid var(--border);border-radius:8px;padding:12px 14px;display:flex;flex-direction:column;gap:8px}
.assigned{border:2px solid var(--warning);margin-bottom:16px}
.decision{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline;border-top:1px solid var(--border);padding-top:6px}
.decision>.mono{min-width:70px}
.decision-body{flex:1 1 320px;display:flex;flex-direction:column;gap:2px}
.waiting{display:inline-flex;gap:6px;align-items:center}
.links{display:flex;flex-wrap:wrap;gap:8px}
.columns{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}
.col{flex:1 1 260px;min-width:260px;display:flex;flex-direction:column;gap:8px}
.timeline{flex:2 1 520px}
.head{display:flex;justify-content:space-between;align-items:baseline;border-bottom:2px solid;padding-bottom:6px;margin-bottom:2px}
.head h2{font-size:18px}
.sub{font-size:12px;color:var(--tertiary);font-weight:600;margin-top:6px;text-transform:uppercase;letter-spacing:.04em}
.category{font-size:12px;color:var(--secondary);font-weight:600;margin-top:4px}
.group{display:flex;flex-direction:column;gap:8px}
.card{background:var(--elevated);border:1px solid var(--border);border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:4px}
.title{font-weight:500}
.detail{font-size:12px;color:var(--secondary);overflow-wrap:anywhere}
.track{border-left:2px solid var(--border);margin-left:6px;margin-top:6px}
.event{display:flex;gap:10px;padding:6px 0 6px 14px;position:relative;align-items:baseline;flex-wrap:wrap}
.event:before{content:"";position:absolute;left:-5px;top:11px;width:8px;height:8px;border-radius:50%;background:var(--accent)}
.event time{min-width:64px}
.event-bot{min-width:120px}
.event-text{flex:1 1 200px;overflow-wrap:anywhere}
.rules{margin-top:16px}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid var(--border)}
th{color:var(--tertiary);font-weight:600}
.num{text-align:right}
.warning{color:var(--warning)}
summary{cursor:pointer;font-size:12px;color:var(--secondary);font-weight:600}
.untested{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline;border-top:1px solid var(--border);padding:3px 0}
.untested .mono{min-width:48px}
.rule-title{flex:1 1 320px;font-size:12px}
footer{margin-top:24px;text-align:center;color:var(--tertiary);font-size:12px}
@media(max-width:560px){main{padding:12px}
.col{min-width:0;width:100%}
.head{gap:8px}
}

'''


def bot_tag(name, big=False):
    h = zlib.crc32(name.lower().encode())
    color, glyph = PALETTE[h % len(PALETTE)], GLYPHS[h // len(PALETTE) % len(GLYPHS)]
    return (f'<span class="bot{" big" if big else ""}"><svg width="16" height="16" viewBox="0 0 20 20" '
            f'aria-hidden="true" style="color:{color}" fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round">{glyph}</svg>{escape(name)}</span>')


def pill(status, owner):
    word, color = STATUS[status]
    if status == 'waiting_on_owner': word = f'Waiting on {owner}'
    return f'<span class="pill" style="color:{color}"><span class="dot"></span>{escape(word)}</span>'


def render_links(links):
    safe = []
    for l in links or []:
        # The reference keeps HTTP references; never render unsafe schemes or legacy action URLs.
        try:
            url = urlsplit(l['href'])
        except ValueError:
            continue
        if url.scheme not in ('http', 'https') or not url.netloc or '/act' in url.path:
            continue
        safe.append(f'<a href="{escape(l["href"], quote=True)}" target="_blank" rel="noopener">{escape(l["label"])}</a>')
    return '<div class="links">' + ''.join(safe) + '</div>' if safe else ''


def item_card(i, owner):
    due = f' · due {i["due"]}' if i.get('due') else ''
    detail = f'<span class="detail">{escape(i["detail"])}</span>' if i['detail'] else ''
    return (f'<article class="card"><div class="row spread">{bot_tag(i["bot"], True)}{pill(i["status"], owner)}</div>'
            f'<div class="mono">{escape(i["id"] + due)}</div><span class="title">{escape(i["title"])}</span>'
            f'{detail}{render_links(i.get("links"))}</article>')


def grouped(items, cats, owner):
    if cats == ['Other']:
        return ''.join(item_card(i, owner) for i in items)
    out = []
    for cat in cats:
        xs = [i for i in items if i['cat'] == cat]
        if xs:
            out.append(f'<div class="group"><div class="category">{escape(cat)} · {len(xs)}</div>' +
                       ''.join(item_card(i, owner) for i in xs) + '</div>')
    return ''.join(out)


def heading(label, count, color):
    return f'<div class="head" style="border-color:{color}"><h2>{label}</h2><span class="muted">{count}</span></div>'


def render(data):
    w, p, d = data['WAITING'], data['PROGRESS'], data['DONE']
    owner, cats = data['OWNER'], data['CATS']
    group = lambda items: grouped(items, cats, owner)
    done_count = len(d) + data['OLDER_DONE']
    counts = {s: sum(i['status'] == s for i in w + p) for s in STATUS}
    counts['done'] = done_count
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<meta http-equiv="refresh" content="60"><title>Backlogr Dashboard</title>',
           f'<style>{CSS}</style></head><body><main><h1>Backlogr Dashboard</h1>',
           f'<div class="intro">Live from Backlogr as of {escape(data["AS_OF"])}. {data["TOTAL"]} tasks: '
           f'{len(w)} waiting on {escape(owner)}, {len(p)} in progress, {done_count} done, {data["DECLINED"]} declined (hidden). Refreshes every 60s.</div>',
           '<div class="row chips">']
    for s, n in counts.items():
        out.append(f'<span class="chip">{pill(s, owner)}<strong>{n}</strong></span>')
    out.append('</div><div class="row assignees"><span class="muted">Open tasks by assignee:</span>')
    who = [(b, sum(i['bot'] == b for i in w + p)) for b in sorted({i['bot'] for i in w + p})]
    for b, n in sorted(who, key=lambda x: -x[1]):
        if n:
            out.append(f'<span class="chip">{bot_tag(b)}<strong>{n}</strong></span>')
    out.append(f'</div><section class="box assigned"><div class="row spread"><h2>Assigned to {escape(owner)}</h2>'
               f'<span class="muted">{len(w)} decisions · due-dated first, then newest</span></div>')
    for i in [i for i in w if i.get('due')] + [i for i in w if not i.get('due')]:
        title = i['title'] + (f' · due {i["due"]}' if i.get('due') else '')
        detail = f'<span class="detail">Ask: {escape(i["detail"])}</span>' if i['detail'] else ''
        out.append(f'<div class="decision"><span class="mono">{escape(i["id"])}</span><div class="decision-body">'
                   f'<strong>{escape(title)}</strong>{detail}{render_links(i.get("links"))}</div>'
                   f'<span class="waiting muted">waiting: {bot_tag(i["bot"])}</span></div>')
    out.append('</section><div class="columns"><section class="col">' +
               heading(f'Waiting on {escape(owner)}', len(w), '#f2b84b') + group(w) + '</section><section class="col timeline">')
    out.append(heading('Timeline', sum(len(day['events']) for day in data['DAYS']), '#6ea8fe'))
    for day in data['DAYS']:
        out.append(f'<div><div class="sub">{escape(day["day"])}</div><div class="track">')
        for ev in day['events']:
            out.append(f'<div class="event"><time class="mono">{escape(ev["time"])}</time>'
                       f'<span class="event-bot">{bot_tag(ev["bot"])}</span><span class="event-text">{escape(ev["text"])}</span></div>')
        out.append('</div></div>')
    out.append('</section><section class="col">' + heading('Tasks', len(p) + done_count, '#4cc38a'))
    for s in ('blocked', 'in_progress', 'todo'):
        xs = [i for i in p if i['status'] == s]
        if xs or s == 'in_progress':
            out.append(f'<div class="sub">{STATUS[s][0]} · {len(xs)}</div>' + group(xs))
    out.append(f'<div class="sub">Finished · {done_count} (latest {len(d)} shown)</div>' + group(d))
    if data['OLDER_DONE']:
        out.append(f'<span class="muted">{data["OLDER_DONE"]} older finished tasks are in Backlogr.</span>')
    r = data['RULES']
    out.append('</section></div><section class="box rules"><div class="row spread"><h2>Rules &amp; playbooks</h2>'
               f'<span class="muted">{r["total"]} rules · {r["tested"]} tested · '
               f'<strong class="{"warning" if r["untested"] else ""}">{r["untested"]} UNTESTED</strong> · '
               f'{sum(bool(p["skill"]) for p in r["playbooks"])} playbooks</span></div>'
               '<span class="detail">Bots: read your playbook&#x27;s rules at job start with '
               '<span class="mono">backlogr playbooks get &lt;name&gt;</span>.</span>'
               '<div class="table-wrap"><table><thead><tr><th>Playbook</th><th class="num">Rules</th>'
               '<th class="num">Untested</th><th>Skill</th></tr></thead><tbody>')
    for pb in r['playbooks']:
        out.append(f'<tr><td>{escape(pb["name"])}</td><td class="num">{pb["rules"]}</td>'
                   f'<td class="num {"warning" if pb["untested"] else ""}">{pb["untested"]}</td>'
                   f'<td class="mono">{escape(pb["skill"] or "-")}</td></tr>')
    out.append('</tbody></table></div>')
    if r['list']:
        out.append(f'<details><summary>{len(r["list"])} UNTESTED rules (need gate scripts) · list via `backlogr rules untested`</summary>')
        for rule in r['list']:
            out.append(f'<div class="untested"><span class="mono">{escape(rule["id"])}</span>'
                       f'<span class="rule-title">{escape(rule["title"])}</span><span class="muted">{escape(rule["playbook"])}</span></div>')
        out.append('</details>')
    out.append(f'</section><footer>Backlogr {escape(core.__version__)}</footer></main></body></html>')
    return ''.join(out)
