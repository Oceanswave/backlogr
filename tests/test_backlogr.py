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
    w = mk(c, title="wait", status="waiting_on_sean")
    d = mk(c, title="closed", status="done", due="2026-10-01")
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
    assert cli.main(["update", tid, "--by","Mint","--status","in_progress","--link","https://x","--kind","order"]) == 0
    assert cli.main(["done", tid, "--by","Mint","--note","arrived"]) == 0
    capsys.readouterr()
    assert cli.main(["--json","get", tid]) == 0
    t = json.loads(capsys.readouterr().out); assert t["status"] == "done" and len(t["updates"]) == 4
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
                    assert s_name == "backlogr" and names == {"task_create","task_update","task_add_link","task_get","task_list","task_flags","task_import"}
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
