import os, sys, json, sqlite3, datetime as dt, subprocess, time, socket, pytest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
import core, cli

@pytest.fixture
def c(tmp_path):
    return core.connect(str(tmp_path / "t.db"))

def mk(c, **kw):
    a = dict(title="Research X", owner_bot="Forge", source="via_coordinator", actor="Coordinator"); a.update(kw)
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
    w = mk(c, title="wait", status="waiting_on_owner", ask="OK to spend $40?")
    d = mk(c, title="closed", status="done", due="2026-10-01", evidence="sha abc123")
    assert {t["title"] for t in core.list_tasks(c, ref=ref)} == {"overdue","blocked","wait"}
    assert [t["title"] for t in core.list_tasks(c, owner="mint", ref=ref)] == ["blocked"]
    assert [t["title"] for t in core.list_tasks(c, overdue=True, ref=ref)] == ["overdue"]
    assert [t["title"] for t in core.list_tasks(c, stale_hours=24, ref=ref)] == ["overdue"]
    assert len(core.list_tasks(c, include_closed=True, ref=ref)) == 4
    f = core.flags(c, stale_hours=24, created_within_hours=24, ref=ref)
    assert [t["title"] for t in f["blocked"]] == ["blocked"]
    assert [t["title"] for t in f["waiting_on_owner"]] == ["wait"]
    assert "overdue" not in [t["title"] for t in f["created_recently"]]
    # same-day date-only due is not overdue
    mk(c, title="today", due="2026-10-24")
    assert "today" not in [t["title"] for t in core.list_tasks(c, overdue=True, ref=ref)]

def test_import_and_export(c, tmp_path):
    r = core.bulk_import(c, [dict(title="a", owner_bot="Mint", source="bot_initiated", links=[{"ref":"x","kind":"pr"}]),
                             dict(title="b", owner_bot="Mint", source="bad")], "Coordinator")
    assert len(r["created"]) == 1 and r["errors"][0]["title"] == "b"
    out = core.export(c, str(tmp_path / "e.json"))
    data = json.load(open(out["path"])); assert data["tasks"][0]["links"][0]["kind"] == "pr"

def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "cli.db"))
    assert cli.main(["--json","new","Buy tee","--owner","Mint","--source","via_coordinator","--by","Coordinator","--approval"]) == 0
    tid = json.loads(capsys.readouterr().out)["id"]
    assert cli.main(["approve", tid, "--by","Mint","--approver","Operator","--source","chat"]) == 0
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
    assert cli.main(["--json","new","Remote","--owner","Mint","--source","via_coordinator","--by","Coordinator"]) == 0
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
    capsys.readouterr()
    assert cli.main(["act", tid, "later", "--reason", "Review tomorrow"]) == 0
    capsys.readouterr()
    assert cli.main(["get", tid, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked"
    assert result["updates"][-1]["note"] == "act later by owner: Review tomorrow"
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
                    assert s_name == "backlogr" and names == {"task_create","task_update","task_add_link","task_get","task_list","task_flags","task_import","task_stale","task_waiting","task_act","rule_list","rule_get","rule_add","rule_untested","playbook_list","playbook_get","playbook_add","rules_import","rule_remove","playbook_remove"}
                    res = await s.call_tool("task_create", dict(title="hi", owner_bot="Mint", source="direct_to_bot", actor="Mint"))
                    tid = json.loads(res.content[0].text)["id"]
                    res = await s.call_tool("task_update", dict(id=tid, actor="Mint", status="blocked", note="needs card"))
                    res = await s.call_tool("task_flags", {})
                    assert json.loads(res.content[0].text)["blocked"][0]["id"] == tid
                    bad = await s.call_tool("task_update", dict(id=tid, actor="Mint", status="nope"))
                    assert bad.isError
                    imported = await s.call_tool("rules_import", {
                        "items": {"rules": [{"id": "R-demo", "rule": "Check examples", "test": "UNTESTED"}],
                                  "playbooks": [{"id": "P-demo", "name": "Checks", "rule_ids": ["R-demo"]}]},
                        "actor": "Agent"})
                    assert json.loads(imported.content[0].text)["created"] == 2
                    for name, args in [("rule_list", {"playbook": "P-demo", "testable": False}),
                                       ("rule_get", {"id": "R-demo"}), ("rule_untested", {}),
                                       ("playbook_list", {}), ("playbook_get", {"id": "P-demo"}),
                                       ("rule_add", {"id": "R-other", "rule": "Check", "test": "pytest"}),
                                       ("playbook_add", {"id": "P-other", "name": "Other", "rule_ids": ["R-other"]})]:
                        result = await s.call_tool(name, args)
                        assert not result.isError, result
                    bad_rule = await s.call_tool("rule_add", {"id": "R-demo", "rule": "Duplicate"})
                    assert bad_rule.isError
        asyncio.run(go())

def test_export_creates_parent_dir(c, tmp_path):
    mk(c)
    out = core.export(c, str(tmp_path / "new" / "dir" / "e.json"))
    assert json.load(open(out["path"]))["tasks"]

def test_quiet_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "q.db"))
    assert cli.main(["new","Quiet","--owner","Mint","--source","via_coordinator","--by","Coordinator","--done-means","x"*300]) == 0
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
        cli.main(["new", t, "--owner", o, "--source", "via_coordinator", "--by", "Coordinator", "--status", "in_progress"])
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
    core.create(core.connect(), "cli old", "Mint", "via_coordinator", "Coordinator", status="in_progress", created_at=three)
    assert cli.main(["stale", "--hours", "2"]) == 0
    out = capsys.readouterr().out
    assert "Mint (1)" in out and "1 stale" in out

def test_waiting_queue(c, tmp_path, monkeypatch, capsys):
    a = mk(c, title="buy tee")
    with pytest.raises(core.TaskError): core.update(c, a["id"], "Mint", status="waiting_on_owner")
    with pytest.raises(core.TaskError): core.update(c, a["id"], "Mint", status="waiting_on_owner", ask="  ")
    with pytest.raises(core.TaskError): mk(c, title="x", status="waiting_on_owner")
    old = (dt.datetime.now().astimezone() - dt.timedelta(hours=5)).isoformat(timespec="seconds")
    b = mk(c, title="older", status="waiting_on_owner", ask="Pick venue A or B", created_at=old)
    core.update(c, a["id"], "Mint", status="waiting_on_owner", ask="OK to spend $40 on the tee?")
    core.update(c, a["id"], "Mint", note="pinged")  # staying in waiting needs no new ask
    mk(c, title="not waiting", status="blocked")
    q = core.waiting_queue(c)
    assert len(q) == len(core.list_tasks(c, status="waiting_on_owner")) == 2
    assert [t["id"] for t in q] == [b["id"], a["id"]] and q[0]["ask"] == "Pick venue A or B" and q[0]["age_hours"] >= 4.9
    # legacy row (pre-migration, no ask) still counted
    c.execute("UPDATE tasks SET status='waiting_on_owner', ask=NULL WHERE title='not waiting'")
    assert len(core.waiting_queue(c)) == 3
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "q.db"))
    assert cli.main(["new", "x", "--owner", "Mint", "--source", "via_coordinator", "--by", "Coordinator", "--status", "waiting_on_owner"]) == 2
    assert cli.main(["new", "x", "--owner", "Mint", "--source", "via_coordinator", "--by", "Coordinator", "--status", "waiting_on_owner", "--ask", "Approve?"]) == 0
    capsys.readouterr()
    assert cli.main(["sean"]) == 0
    out = capsys.readouterr().out; assert "Approve?" in out and "1 waiting on owner" in out

def test_migrates_v1_db(tmp_path):
    p = str(tmp_path / "v1.db"); old = sqlite3.connect(p)
    old.executescript(core.SCHEMA)  # 1.0.0 schema: no added columns
    old.execute("INSERT INTO tasks VALUES('T-old','t','', 'Mint', NULL,'todo','via_coordinator',0,'2026-10-25T10:00:00-04:00','2026-10-25T10:00:00-04:00')")
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
    for bot in ("Forge", "coordinator", "Mint "):  # owner, an actor seen in the log, and BACKLOGR_BOTS
        monkeypatch.setenv("BACKLOGR_BOTS", "Mint")
        with pytest.raises(core.TaskError, match="bot"):
            core.update(c, t["id"], "Forge", approved_by=bot, approval_source="relayed")
    with pytest.raises(core.TaskError, match="required"):
        core.update(c, t["id"], "Forge", approved_by="  ", approval_source="x")
    with pytest.raises(core.TaskError):  # cannot approve and start in one step as a bot either
        mk(c, approval_needed=True, status="in_progress", approved_by="Forge", approval_source="x")
    assert core.get(c, t["id"])["approved_by"] is None
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "t.db"))
    assert cli.main(["approve", t["id"], "--by", "Forge", "--approver", "Operator", "--source", "Operator in chat"]) == 0
    r = core.get(c, t["id"])
    assert (r["approved_by"], r["approval_source"]) == ("Operator", "Operator in chat") and r["approved_at"]
    assert "approved by Operator" in r["updates"][-1]["note"]
    assert core.update(c, t["id"], "Forge", status="in_progress")["status"] == "in_progress"
    monkeypatch.setenv("BACKLOGR_APPROVERS", "Operator")
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
    monkeypatch.setenv("BACKLOGR_APPROVERS", "Forge,Operator")
    with pytest.raises(core.TaskError, match="bot"):
        core.update(c, w["id"], "Coordinator", approved_by="Forge", approval_source="x")
    monkeypatch.delenv("BACKLOGR_APPROVERS")
    core.update(c, w["id"], "Coordinator", approved_by="Pat", approval_source="chat")
    monkeypatch.setenv("BACKLOGR_BOTS", "Mint,Pat")
    with pytest.raises(core.TaskError, match="bot"):
        core.update(c, w["id"], "Forge", status="in_progress")
    with pytest.raises(core.TaskError, match="approval_source"):
        core.update(c, w["id"], "Forge", approved_by="Operator")
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

def test_idempotent_create(c, tmp_path, monkeypatch, capsys):
    a = mk(c, title="Review PR 12", key="forge:pr-12")
    b = mk(c, title="Review PR 12 again", key="forge:pr-12")
    assert a["id"] == b["id"] and b["existing"] and not a["existing"]
    assert c.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):  # unique index backs the key
        c.execute("UPDATE tasks SET key='forge:pr-12' WHERE id=?", (mk(c, title="other")["id"],))
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "t.db"))
    args = ["new", "Order printer ink", "--owner", "Mint", "--source", "via_coordinator", "--by", "Coordinator", "--key", "mint:ink"]
    assert cli.main(args) == 0; first = capsys.readouterr().out.split()[0]
    assert cli.main(args) == 0; out = capsys.readouterr().out
    assert out == f"{first} todo (existing)\n"
    d = mk(c, title="Order printer ink!")  # no key, near-identical title: created, with a warning
    assert d["id"] != first and any(first in w for w in d["warnings"])
    assert cli.main(["new", "Order printer inks", "--owner", "Mint", "--source", "via_coordinator", "--by", "Coordinator"]) == 0
    assert "warning: similar title" in capsys.readouterr().err
    assert "warnings" not in mk(c, title="Something unrelated entirely")

def test_act(c, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BACKLOGR_OWNER", "Operator")
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("BACKLOGR_HUMANS", "Operator")
    t = mk(c, title="sample", done_means="unit tests pass", approval_needed=True)
    # done (CLI, default --by Operator): human word is the evidence and the approval; history records it
    assert cli.main(["act", t["id"], "done", "--reason", "looked at it"]) == 0
    assert capsys.readouterr().out == f"{t['id']} done\n"
    r = core.get(c, t["id"])
    assert r["status"] == "done" and r["approved_by"] == "Operator" and r["evidence"].startswith("confirmed done by Operator")
    assert r["updates"][-1]["actor"] == "Operator" and r["updates"][-1]["note"].startswith("act done by Operator: looked at it; approved by Operator; evidence:")
    # undo restores status and approval fields; a second undo has nothing to undo
    assert cli.main(["act", t["id"], "undo"]) == 0
    r = core.get(c, t["id"]); assert r["status"] == "todo" and r["approved_by"] is None and r["evidence"] is None
    assert r["updates"][-1]["note"] == "act undo of done by Operator: status done -> todo"
    with pytest.raises(core.TaskError, match="nothing to undo"): core.act(c, t["id"], "undo")
    # na -> declined with reason; undo
    assert core.act(c, t["id"], "na", reason="not needed now")["status"] == "declined"
    assert core.get(c, t["id"])["updates"][-1]["note"] == "act na by Operator: not needed now; status: todo -> declined"
    assert core.act(c, t["id"], "undo")["status"] == "todo"
    # question: status kept, marked for the owner, shown in flags; owner's next update clears it
    with pytest.raises(core.TaskError, match="question"): core.act(c, t["id"], "question")
    q = core.act(c, t["id"], "question", reason="which vendor?")
    assert q["status"] == "todo" and q["open_question"] == "which vendor?"
    assert [x["id"] for x in core.flags(c)["questions_for_owner"]] == [t["id"]]
    core.update(c, t["id"], "Forge", note="answer: vendor A")
    assert core.get(c, t["id"])["open_question"] is None
    with pytest.raises(core.TaskError, match="nothing to undo"): core.act(c, t["id"], "undo")  # window closed
    # Operator acting does not make Operator a "bot" for approvals; a bot acting gets the normal gates
    assert core.update(c, t["id"], "Forge", approved_by="Operator", approval_source="chat")["approved_by"] == "Operator"
    u = mk(c, title="bot act", done_means="unit tests pass")
    with pytest.raises(core.TaskError, match="tests"): core.act(c, u["id"], "done", by="Forge")
    with pytest.raises(SystemExit): cli.main(["act", t["id"], "bogus"])

def test_act_safety(c, monkeypatch):
    monkeypatch.setenv("BACKLOGR_HUMANS", "Operator,Bob"); monkeypatch.setenv("BACKLOGR_APPROVERS", "Operator")
    t = mk(c, title="gated", done_means="unit tests pass", approval_needed=True)
    # not an allowed approver -> normal gates
    with pytest.raises(core.TaskError): core.act(c, t["id"], "done", by="Bob")
    # not in BACKLOGR_HUMANS -> normal gates, even with an act-looking note in history
    core.update(c, t["id"], "Coord", note="act done by Coord")
    with pytest.raises(core.TaskError): core.act(c, t["id"], "done", by="Coord")
    # stale snapshot: a write between read and apply is refused
    core.act(c, t["id"], "na", reason="x")
    seq = core._last_seq(c, t["id"]); core.update(c, t["id"], "Forge", note="concurrent")
    with pytest.raises(core.TaskError, match="changed"):
        core.update(c, t["id"], "Operator", status="todo", _extra={"last_act": None}, _expect=seq)

def test_act_undo_after_link_refused(c, monkeypatch):
    monkeypatch.setenv("BACKLOGR_HUMANS", "Operator")
    t = mk(c, title="linked")
    core.act(c, t["id"], "done")
    core.add_link(c, t["id"], "Forge", "https://example.com/pr/1", "pr")
    with pytest.raises(core.TaskError, match="nothing to undo"): core.act(c, t["id"], "undo")


def test_cli_remote_rules(server, tmp_path, monkeypatch, capsys):
    token = tmp_path / 'token'; token.write_text(TOK); token.chmod(0o600)
    monkeypatch.setenv('BACKLOGR_URL', server)
    monkeypatch.setenv('BACKLOGR_TOKEN_FILE', str(token))
    def run(args):
        assert cli.main(['--json'] + args) == 0
        return json.loads(capsys.readouterr().out)
    fixture = os.path.join(HERE, 'tests', 'fixtures', 'rules-playbooks.json')
    assert run(['rules', 'import', fixture, '--by', 'Agent'])['created'] == 3
    assert run(['rules', 'import', fixture, '--by', 'Agent'])['unchanged'] == 3
    assert run(['rules', 'get', 'M1'])['testable'] is True
    assert run(['rules', 'list', '--playbook', 'P-check', '--testable', 'yes'])[0]['id'] == 'M1'
    assert run(['rules', 'untested'])[0]['id'] == 'M2'
    assert run(['playbooks', 'list'])[0]['id'] == 'P-check'
    assert len(run(['playbooks', 'get', 'P-check'])['rules']) == 2
    assert run(['rules', 'add', 'R-extra', '--rule', 'Check'])['id'] == 'R-extra'
    assert run(['playbooks', 'add', 'P-extra', '--name', 'Extra', '--rules', 'R-extra'])['rule_ids'] == ['R-extra']
