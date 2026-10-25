import os, sys, json, sqlite3, datetime as dt, subprocess, time, socket, pytest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
import core, cli

@pytest.fixture
def c(tmp_path):
    return core.connect(str(tmp_path / "t.db"))

def mk(c, **kw):
    a = dict(title="Research X", owner_bot="Forge", source="via_chukar", actor="Chukar"); a.update(kw)
    return core.create(c, **a)

def test_create_fields(c):
    t = mk(c, done_means="report sent", due="2026-10-25", approval_needed=True)
    for k in ("id","title","done_means","owner_bot","due","status","source","approval_needed","created_at","updated_at"):
        assert k in t
    assert t["status"] == "todo" and t["approval_needed"] is True and len(t["updates"]) == 1

@pytest.mark.parametrize("kw", [dict(status="nope"), dict(source="git"), dict(due="tomorrow"), dict(title="")])
def test_validation(c, kw):
    with pytest.raises(core.TaskError): mk(c, **kw)

def test_update_logs_and_links(c):
    t = mk(c)
    t = core.update(c, t["id"], "Forge", status="in_progress", note="started")
    t = core.add_link(c, t["id"], "Forge", "https://example.com/doc", "doc")
    t = core.update(c, t["id"], "Forge", owner_bot="Mint", due="2026-10-26T17:00-04:00")
    assert t["owner_bot"] == "Mint" and len(t["updates"]) == 4 and t["links"][0]["kind"] == "doc"
    assert "status: todo -> in_progress" in t["updates"][1]["note"]
    with pytest.raises(core.TaskError): core.update(c, t["id"], "Forge")
    with pytest.raises(core.TaskError): core.update(c, "T-nope", "Forge", note="x")

def test_updates_append_only(c):
    t = mk(c)
    with pytest.raises(sqlite3.DatabaseError): c.execute("DELETE FROM updates")
    with pytest.raises(sqlite3.DatabaseError): c.execute("UPDATE updates SET note='x'")

def test_list_filters_and_flags(c):
    ref = dt.datetime(2026, 10, 24, 16, 0).astimezone()
    old = (ref - dt.timedelta(hours=48)).isoformat()
    a = mk(c, title="overdue", due="2026-10-23", created_at=old)
    b = mk(c, title="blocked", owner_bot="Mint", status="blocked")
    w = mk(c, title="wait", status="waiting_on_sean", ask="OK to spend $40?")
    d = mk(c, title="closed", status="done", due="2026-10-01", evidence="sha abc123")
    assert {t["title"] for t in core.list_tasks(c, ref=ref)} == {"overdue","blocked","wait"}
    assert [t["title"] for t in core.list_tasks(c, owner="mint", ref=ref)] == ["blocked"]
    assert [t["title"] for t in core.list_tasks(c, overdue=True, ref=ref)] == ["overdue"]
    assert [t["title"] for t in core.list_tasks(c, stale_hours=24, ref=ref)] == ["overdue"]
    assert len(core.list_tasks(c, include_closed=True, ref=ref)) == 4
    f = core.flags(c, stale_hours=24, created_within_hours=24, ref=ref)
    assert [t["title"] for t in f["blocked"]] == ["blocked"]
    assert [t["title"] for t in f["waiting_on_sean"]] == ["wait"]
    assert "overdue" not in [t["title"] for t in f["created_recently"]]
    # same-day date-only due is not overdue
    mk(c, title="today", due="2026-10-24")
    assert "today" not in [t["title"] for t in core.list_tasks(c, overdue=True, ref=ref)]

def test_import_and_export(c, tmp_path):
    r = core.bulk_import(c, [dict(title="a", owner_bot="Mint", source="bot_initiated", links=[{"ref":"x","kind":"pr"}]),
                             dict(title="b", owner_bot="Mint", source="bad")], "Chukar")
    assert len(r["created"]) == 1 and r["errors"][0]["title"] == "b"
    out = core.export(c, str(tmp_path / "e.json"))
    data = json.load(open(out["path"])); assert data["tasks"][0]["links"][0]["kind"] == "pr"

def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "cli.db"))
    assert cli.main(["--json","new","Buy tee","--owner","Mint","--source","via_chukar","--by","Chukar","--approval"]) == 0
    tid = json.loads(capsys.readouterr().out)["id"]
    assert cli.main(["approve", tid, "--by","Mint","--approver","Sean","--source","chat"]) == 0
    assert cli.main(["update", tid, "--by","Mint","--status","in_progress","--link","https://x","--kind","order"]) == 0
    assert cli.main(["done", tid, "--by","Mint","--note","arrived","--evidence","tracking 1Z999"]) == 0
    capsys.readouterr()
    assert cli.main(["--json","get", tid]) == 0
    t = json.loads(capsys.readouterr().out); assert t["status"] == "done" and len(t["updates"]) == 5
    assert cli.main(["list","--all"]) == 0 and "1 task(s)" in capsys.readouterr().out
    assert cli.main(["flags"]) == 0
    with pytest.raises(SystemExit): cli.main(["update", tid, "--by","x","--status","bogus"])

TOK = "test-token-0123456789abcdef"

@pytest.fixture
def server(tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = dict(os.environ, BACKLOGR_DB=str(tmp_path / "m.db"), BACKLOGR_PORT=str(port), BACKLOGR_TOKEN=TOK)
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try: socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError: time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    p.terminate(); p.wait(5)

def _req(url, data=None, auth=None):
    import urllib.request, urllib.error
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if auth: h["Authorization"] = auth
    r = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp: return resp.status, resp.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

def test_auth(server):
    assert _req(server + "/healthz") == (200, b"ok")
    init = json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}).encode()
    assert _req(server + "/mcp", init)[0] == 401
    assert _req(server + "/mcp", init, "Bearer wrong")[0] == 401
    assert _req(server + "/mcp", init, TOK)[0] == 401
    assert _req(server + "/api/list", b"{}")[0] == 401
    assert _req(server + "/mcp", init, f"Bearer {TOK}")[0] == 200

def test_server_requires_token(tmp_path):
    env = dict(os.environ, BACKLOGR_DB=str(tmp_path / "x.db")); env.pop("BACKLOGR_TOKEN", None)
    r = subprocess.run([sys.executable, os.path.join(HERE, "server.py")], env=env, capture_output=True, timeout=20)
    assert r.returncode != 0

def test_cli_remote(server, tmp_path, monkeypatch, capsys):
    tf = tmp_path / "tok"; tf.write_text(TOK + "\n")
    monkeypatch.setenv("BACKLOGR_URL", server); monkeypatch.setenv("BACKLOGR_TOKEN_FILE", str(tf))
    assert cli.main(["--json","new","Remote","--owner","Mint","--source","via_chukar","--by","Chukar"]) == 0
    tid = json.loads(capsys.readouterr().out)["id"]
    assert cli.main(["update", tid, "--by","Mint","--status","blocked","--link","https://x"]) == 0
    capsys.readouterr()
    assert cli.main(["--json","get", tid]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    assert cli.main(["list"]) == 0 and "1 task(s)" in capsys.readouterr().out
    assert cli.main(["flags"]) == 0
    assert cli.main(["update", "T-nope", "--by","Mint","--note","x"]) == 2
    assert cli.main(["export","--path", str(tmp_path / "e.json")]) == 0
    assert len(json.load(open(tmp_path / "e.json"))["tasks"]) == 1
    tf.write_text("wrong"); assert cli.main(["list"]) == 2

def test_mcp_http_end_to_end(server):
    if True:
        import asyncio
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async def go():
            async with streamablehttp_client(server + "/mcp", headers={"Authorization": f"Bearer {TOK}"}) as (r, w, _):
                async with ClientSession(r, w) as s:
                    s_name = (await s.initialize()).serverInfo.name
                    names = {t.name for t in (await s.list_tools()).tools}
                    assert s_name == "backlogr" and names == {"task_create","task_update","task_add_link","task_get","task_list","task_flags","task_import","task_stale","task_sean"}
                    res = await s.call_tool("task_create", dict(title="hi", owner_bot="Mint", source="direct_to_bot", actor="Mint"))
                    tid = json.loads(res.content[0].text)["id"]
                    res = await s.call_tool("task_update", dict(id=tid, actor="Mint", status="blocked", note="needs card"))
                    res = await s.call_tool("task_flags", {})
                    assert json.loads(res.content[0].text)["blocked"][0]["id"] == tid
                    bad = await s.call_tool("task_update", dict(id=tid, actor="Mint", status="nope"))
                    assert bad.isError
        asyncio.run(go())

def test_export_creates_parent_dir(c, tmp_path):
    mk(c)
    out = core.export(c, str(tmp_path / "new" / "dir" / "e.json"))
    assert json.load(open(out["path"]))["tasks"]

def test_quiet_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "q.db"))
    assert cli.main(["new","Quiet","--owner","Mint","--source","via_chukar","--by","Chukar","--done-means","x"*300]) == 0
    out = capsys.readouterr().out; tid = out.split()[0]
    assert out == f"{tid} todo\n"
    assert cli.main(["update", tid, "--by","Mint","--status","in_progress","--note","n"*500]) == 0
    out = capsys.readouterr().out; assert out == f"{tid} in_progress\n" and len(out) < 200
    assert cli.main(["update", tid, "--by","Mint","--note","more","--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == tid
    assert cli.main(["--json","get", tid]) == 0 and len(capsys.readouterr().out) > 200

def test_list_real_filters(c, tmp_path, monkeypatch, capsys):
    a = mk(c, title="Open PR for docs", owner_bot="Forge", due="2026-10-20")
    core.update(c, a["id"], "Forge", status="in_progress")
    b = mk(c, title="Write tests", owner_bot="Forge", source="bot_initiated")
    core.update(c, b["id"], "Forge", status="in_progress", note="links the PR")
    mk(c, title="Other PR", owner_bot="Mint", status="in_progress")
    mk(c, title="PR waiting", owner_bot="Forge", status="todo")
    mk(c, title="100% done_x", owner_bot="Forge", status="blocked")
    ids = lambda **k: {t["id"] for t in core.list_tasks(c, **k)}
    assert ids(owner="Forge", status="in_progress", q="PR") == {a["id"], b["id"]}
    assert len(ids(owner="Forge", status="in_progress,todo", q="pr")) == 3
    assert ids(source="bot_initiated") == {b["id"]}
    assert ids(due_before="2026-10-21") == {a["id"]} and ids(due_before="2026-10-20") == set()
    assert len(ids(q="100%")) == 1 and len(ids(q="done_x")) == 1 and ids(q="progress") == set()
    with pytest.raises(core.TaskError): core.list_tasks(c, status="in_progress,bogus")
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "f.db"))
    for t, o in (("Open PR", "Forge"), ("Docs", "Forge"), ("PR elsewhere", "Mint")):
        cli.main(["new", t, "--owner", o, "--source", "via_chukar", "--by", "Chukar", "--status", "in_progress"])
    capsys.readouterr()
    assert cli.main(["list", "--owner", "Forge", "--status", "in_progress", "--q", "PR"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "1 task(s)" and "Open PR" in out[0]
    tid = out[0].split()[0]
    assert cli.main(["get", tid, "--short"]) == 0
    line = capsys.readouterr().out
    assert line.count("\n") == 1 and tid in line and "Open PR" in line

def test_stale_detector(c, tmp_path, monkeypatch, capsys):
    now = dt.datetime.now().astimezone()
    three = (now - dt.timedelta(hours=3)).isoformat(timespec="seconds")
    a = mk(c, title="old work", owner_bot="Forge", status="in_progress", created_at=three)
    mk(c, title="old todo", owner_bot="Forge", status="todo", created_at=three)
    mk(c, title="fresh", owner_bot="Mint", status="in_progress")
    r = core.stale(c, hours=2)
    assert r["count"] == 1 and [t["id"] for t in r["by_owner"]["Forge"]] == [a["id"]]
    assert r["by_owner"]["Forge"][0]["hours_since_note"] >= 2.9
    core.update(c, a["id"], "Forge", note="still going")
    assert core.stale(c, hours=2)["count"] == 0
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "s.db"))
    core.create(core.connect(), "cli old", "Mint", "via_chukar", "Chukar", status="in_progress", created_at=three)
    assert cli.main(["stale", "--hours", "2"]) == 0
    out = capsys.readouterr().out
    assert "Mint (1)" in out and "1 stale" in out

def test_sean_queue(c, tmp_path, monkeypatch, capsys):
    a = mk(c, title="buy tee")
    with pytest.raises(core.TaskError): core.update(c, a["id"], "Mint", status="waiting_on_sean")
    with pytest.raises(core.TaskError): core.update(c, a["id"], "Mint", status="waiting_on_sean", ask="  ")
    with pytest.raises(core.TaskError): mk(c, title="x", status="waiting_on_sean")
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=5)).isoformat(timespec="seconds")
    b = mk(c, title="older", status="waiting_on_sean", ask="Pick venue A or B", created_at=old)
    core.update(c, a["id"], "Mint", status="waiting_on_sean", ask="OK to spend $40 on the tee?")
    core.update(c, a["id"], "Mint", note="pinged")  # staying in waiting needs no new ask
    mk(c, title="not waiting", status="blocked")
    q = core.sean_queue(c)
    assert len(q) == len(core.list_tasks(c, status="waiting_on_sean")) == 2
    assert [t["id"] for t in q] == [b["id"], a["id"]] and q[0]["ask"] == "Pick venue A or B" and q[0]["age_hours"] >= 4.9
    # legacy row (pre-migration, no ask) still counted
    c.execute("UPDATE tasks SET status='waiting_on_sean', ask=NULL WHERE title='not waiting'")
    assert len(core.sean_queue(c)) == 3
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "q.db"))
    assert cli.main(["new", "x", "--owner", "Mint", "--source", "via_chukar", "--by", "Chukar", "--status", "waiting_on_sean"]) == 2
    assert cli.main(["new", "x", "--owner", "Mint", "--source", "via_chukar", "--by", "Chukar", "--status", "waiting_on_sean", "--ask", "Approve?"]) == 0
    capsys.readouterr()
    assert cli.main(["sean"]) == 0
    out = capsys.readouterr().out; assert "Approve?" in out and "1 waiting on Sean" in out

def test_migrates_v1_db(tmp_path):
    p = str(tmp_path / "v1.db"); old = sqlite3.connect(p)
    old.executescript(core.SCHEMA)  # 1.0.0 schema: no added columns
    old.execute("INSERT INTO tasks VALUES('T-old','t','', 'Mint', NULL,'todo','via_chukar',0,'2026-10-25T10:00:00-04:00','2026-10-25T10:00:00-04:00')")
    old.commit(); old.close()
    c = core.connect(p)
    cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    assert {n for n, _ in core.ADDED_COLUMNS} <= cols
    assert core.get(c, "T-old")["title"] == "t"
    core.connect(p)  # idempotent

def test_approval_record(c, tmp_path, monkeypatch, capsys):
    t = mk(c, approval_needed=True)
    with pytest.raises(core.TaskError, match="approval"):
        core.update(c, t["id"], "Forge", status="in_progress")
    for bot in ("Forge", "chukar", "Mint "):  # owner, an actor seen in the log, and BACKLOGR_BOTS
        monkeypatch.setenv("BACKLOGR_BOTS", "Mint")
        with pytest.raises(core.TaskError, match="bot"):
            core.update(c, t["id"], "Forge", approved_by=bot, approval_source="relayed")
    with pytest.raises(core.TaskError, match="required"):
        core.update(c, t["id"], "Forge", approved_by="  ", approval_source="x")
    with pytest.raises(core.TaskError):  # cannot approve and start in one step as a bot either
        mk(c, approval_needed=True, status="in_progress", approved_by="Forge", approval_source="x")
    assert core.get(c, t["id"])["approved_by"] is None
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "t.db"))
    assert cli.main(["approve", t["id"], "--by", "Forge", "--approver", "Sean", "--source", "Sean in Chukar chat 11:17"]) == 0
    r = core.get(c, t["id"])
    assert (r["approved_by"], r["approval_source"]) == ("Sean", "Sean in Chukar chat 11:17") and r["approved_at"]
    assert "approved by Sean" in r["updates"][-1]["note"]
    assert core.update(c, t["id"], "Forge", status="in_progress")["status"] == "in_progress"
    monkeypatch.setenv("BACKLOGR_APPROVERS", "Sean")
    with pytest.raises(core.TaskError, match="APPROVERS"):
        core.update(c, t["id"], "Forge", approved_by="Bob", approval_source="x")
    monkeypatch.delenv("BACKLOGR_APPROVERS")
    # review findings: toggles cannot bypass, new owner cannot self-approve, allowlist never admits a bot,
    # stored approvers are rechecked, and a source is required
    v = mk(c, title="v"); core.update(c, v["id"], "Forge", status="in_progress")
    with pytest.raises(core.TaskError, match="approval"):
        core.update(c, v["id"], "Forge", approval_needed=True)
    w = mk(c, title="w", approval_needed=True)
    with pytest.raises(core.TaskError, match="approval"):
        core.update(c, w["id"], "Forge", approval_needed=False)  # clearing while todo is gated too
    with pytest.raises(core.TaskError, match="approval"):
        core.update(c, w["id"], "Forge", status="done", approval_needed=False)
    with pytest.raises(core.TaskError, match="bot"):
        mk(c, title="self", owner_bot="Nova", approval_needed=True, status="done", approved_by="Nova", approval_source="x")
    monkeypatch.setenv("BACKLOGR_APPROVERS", "Forge,Sean")
    with pytest.raises(core.TaskError, match="bot"):
        core.update(c, w["id"], "Chukar", approved_by="Forge", approval_source="x")
    monkeypatch.delenv("BACKLOGR_APPROVERS")
    core.update(c, w["id"], "Chukar", approved_by="Pat", approval_source="chat")
    monkeypatch.setenv("BACKLOGR_BOTS", "Mint,Pat")
    with pytest.raises(core.TaskError, match="bot"):
        core.update(c, w["id"], "Forge", status="in_progress")
    with pytest.raises(core.TaskError, match="approval_source"):
        core.update(c, w["id"], "Forge", approved_by="Sean")
    u = mk(c, title="no approval needed")
    assert core.update(c, u["id"], "Forge", status="in_progress")["status"] == "in_progress"

def test_done_needs_evidence(c, tmp_path, monkeypatch, capsys):
    t = mk(c, done_means="report sent")
    with pytest.raises(core.TaskError, match="evidence"):
        core.update(c, t["id"], "Forge", status="done")
    with pytest.raises(core.TaskError, match="evidence"):
        mk(c, status="done")
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "t.db"))
    with pytest.raises(SystemExit):  # CLI: --evidence is required
        cli.main(["done", t["id"], "--by", "Forge"])
    assert core.get(c, t["id"])["status"] == "todo"
    assert cli.main(["done", t["id"], "--by", "Forge", "--evidence", "https://example.com/report"]) == 0
    r = core.get(c, t["id"]); assert r["status"] == "done" and r["evidence"] == "https://example.com/report"
    # done_means mentions tests: per-test pass/fail is required and recorded
    u = mk(c, title="ship parser", done_means="unit tests pass and CI green")
    with pytest.raises(core.TaskError, match="tests"):
        core.update(c, u["id"], "Forge", status="done", evidence="sha 1234567")
    with pytest.raises(core.TaskError, match="pass\\|fail"):
        core.update(c, u["id"], "Forge", status="done", evidence="sha 1234567", test_results=["parse=ok"])
    assert cli.main(["done", u["id"], "--by", "Forge", "--evidence", "sha 1234567",
                     "--test", "parse=pass", "--test", "ci=fail"]) == 0
    r = core.get(c, u["id"])
    assert r["test_results"] == {"parse": "pass", "ci": "fail"}
    assert "tests: 1 pass, 1 fail; FAIL ci" in r["updates"][-1]["note"]
    v = mk(c, title="mcp dict form", done_means="test: smoke")
    assert core.update(c, v["id"], "Forge", status="done", evidence="log", test_results={"smoke": "PASS"})["test_results"] == {"smoke": "pass"}

def test_done_evidence_review_cases(c, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "t.db"))
    assert cli.main(["new", "already shipped", "--owner", "Forge", "--source", "bot_initiated", "--by", "Forge",
                     "--status", "done", "--done-means", "tests green", "--evidence", "sha abc", "--test", "unit=pass"]) == 0
    tid = capsys.readouterr().out.split()[0]
    assert core.get(c, tid)["test_results"] == {"unit": "pass"}
    t = mk(c, done_means="report sent")
    core.update(c, t["id"], "Forge", status="done", evidence="sent")
    with pytest.raises(core.TaskError, match="tests"):  # changing a done row's criteria re-checks them
        core.update(c, t["id"], "Forge", done_means="unit tests pass")
    u = mk(c, done_means="unit tests pass")  # staged: evidence and results first, then done
    core.update(c, u["id"], "Forge", evidence="sha 1", test_results={"unit": "pass"})
    assert core.update(c, u["id"], "Forge", status="done")["status"] == "done"
