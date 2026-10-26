import asyncio
from urllib.parse import urlsplit, parse_qs, urlencode

import pytest
import core
import cli
import server


@pytest.fixture
def links(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "links.db"))
    monkeypatch.setenv("BACKLOGR_LINK_SECRET", "test-link-secret")
    monkeypatch.setenv("BACKLOGR_PUBLIC_URL", "https://example.invalid/backlogr")
    monkeypatch.setenv("BACKLOGR_LINK_ACTOR", "owner")
    monkeypatch.delenv("BACKLOGR_URL", raising=False)
    c = core.connect()
    task = core.create(c, title="<script>alert(1)</script>", owner_bot="Bot",
                       source="direct_to_bot", actor="Bot")
    app, _ = server.build_app()
    yield c, task, app
    c.close()


def request(app, url, method="GET", fields=None):
    parts = urlsplit(url)
    body = urlencode(fields or {}).encode()
    sent = []
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(message):
        sent.append(message)
    asyncio.run(app({"type": "http", "path": "/act", "method": method,
                     "query_string": parts.query.encode(),
                     "headers": [(b"content-type", b"application/x-www-form-urlencoded")]},
                    receive, send))
    return sent[0]["status"], b"".join(m.get("body", b"") for m in sent).decode(), dict(sent[0]["headers"])


def params(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_flow(links):
    c, t, app = links
    url = core.sign_link(t["id"], "done")
    before = core.get(c, t["id"])
    for _ in range(2):
        status, html, headers = request(app, url)
        assert status == 200 and '<form method="post"' in html
        assert "<script>" not in html and "&lt;script&gt;" in html
        assert headers[b"cache-control"] == b"no-store"
        assert headers[b"referrer-policy"] == b"no-referrer"
        assert headers[b"x-frame-options"] == b"DENY"
        assert headers[b"content-security-policy"] == b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
        assert core.get(c, t["id"]) == before
    assert request(app, url, "POST", params(url))[0] == 200
    assert core.get(c, t["id"])["status"] == "done"
    status, html, _ = request(app, url, "POST", params(url))
    assert status == 409 and "This link was already used" in html


@pytest.mark.parametrize("key,value", [("t", "T-unknown"), ("a", "na"), ("e", "1"), ("s", "bad")])
def test_tamper(links, key, value):
    c, t, app = links
    fields = params(core.sign_link(t["id"], "done"))
    fields[key] = value
    url = "/act?" + urlencode(fields)
    assert request(app, url)[0] == 403
    assert request(app, url, "POST", fields)[0] == 403


def test_expired_unknown_disabled(links, monkeypatch):
    c, t, app = links
    url = core.sign_link(t["id"], "done", -1)
    assert request(app, url)[0] == 410
    assert request(app, url, "POST", params(url))[0] == 410
    assert request(app, core.sign_link("T-missing", "done"))[0] == 404
    monkeypatch.delenv("BACKLOGR_LINK_SECRET")
    assert request(app, url)[0] == 404
    assert request(app, url, "POST", params(url))[0] == 404
    with pytest.raises(core.TaskError, match="disabled"):
        core.sign_link(t["id"], "done")


@pytest.mark.parametrize("action", ["question", "later"])
def test_text_rollback_and_undo(links, action):
    c, t, app = links
    url = core.sign_link(t["id"], action, text="untrusted prefill")
    fields = params(url)
    fields["text"] = " "
    assert request(app, url, "POST", fields)[0] == 400
    assert c.execute("SELECT count(*) FROM used_links").fetchone()[0] == 0
    fields["text"] = "<script>note</script>"
    status, html, _ = request(app, url, "POST", fields)
    assert status == 200 and "<script>" not in html
    result = core.get(c, t["id"])
    assert result["status"] == "todo"
    if action == "later":
        assert result["updates"][-1]["note"] == "act later by owner: <script>note</script>"
    else:
        assert result["open_question"] == fields["text"]
    assert core.act(c, t["id"], "undo", by="owner")["open_question"] is None
    assert request(app, url, "POST", fields)[0] == 409


def test_body_limit(links):
    c, t, app = links
    url = core.sign_link(t["id"], "later")
    fields = dict(params(url), text="x" * 16384)
    assert request(app, url, "POST", fields)[0] == 413


def test_cli_local(links, capsys):
    c, t, app = links
    assert cli.main(["link", t["id"], "--action", "done", "--ttl", "3600"]) == 0
    url = capsys.readouterr().out.strip()
    core.verify_link(c, params(url))
    assert cli.main(["link", t["id"], "--all"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == list(core.SIGNABLE_ACTIONS)
    for line in lines: core.verify_link(c, params(line.split()[1]))


def test_cli_remote(links, monkeypatch, capsys):
    c, t, app = links
    calls = []
    remote = cli.Remote("https://example.invalid", "test-token")
    def call(op, **args):
        calls.append(op)
        return server.API[op](c, args)
    monkeypatch.setattr(remote, "_call", call)
    monkeypatch.setattr(cli, "backend", lambda: (remote, None))
    assert cli.main(["link", t["id"], "--all", "--ttl", "3600"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert calls == ["get"] + ["link"] * 5
    for line in lines: core.verify_link(c, params(line.split()[1]))
    assert server.task_link(t["id"], "later").startswith("https://example.invalid/backlogr/act?")

def test_default_actor_and_ttl(links, monkeypatch):
    c, t, app = links
    monkeypatch.delenv("BACKLOGR_LINK_ACTOR")
    monkeypatch.setenv("BACKLOGR_LINK_TTL", "3600")
    monkeypatch.setattr(core.time, "time", lambda: 1000)
    url = core.sign_link(t["id"], "later")
    fields = params(url)
    assert fields["e"] == "4600"
    assert request(app, url, "POST", dict(fields, text="Tomorrow"))[0] == 200
    assert core.get(c, t["id"])["updates"][-1]["actor"] == "owner"


def test_reply_reusable(links):
    c, t, app = links
    url = core.sign_link(t["id"], "reply", text="prefilled")
    status, html, _ = request(app, url)
    assert status == 200 and "Your reply" in html and "prefilled" in html
    assert '>Send</button>' in html and ' required' in html
    for text in ("first reply", "one is done, the other isn't"):
        assert request(app, url, "POST", dict(params(url), text=" "))[0] == 400
        assert request(app, url, "POST", dict(params(url), text="x" * 4001))[0] == 400
        status, html, _ = request(app, url, "POST", dict(params(url), text=text))
        assert status == 200 and "Reply saved. Status unchanged: todo" in html
        assert '<textarea name="text" required></textarea>' in html
        result = core.get(c, t["id"])
        assert result["status"] == "todo" and result["open_question"] == text
        assert result["updates"][-1]["note"] == f"act reply by owner: {text}"
        assert t["id"] in [x["id"] for x in core.flags(c)["questions_for_owner"]]
    assert c.execute("SELECT count(*) FROM used_links").fetchone()[0] == 0
    assert core.act(c, t["id"], "undo")["open_question"] == "first reply"
    expired = core.sign_link(t["id"], "reply", -1)
    assert request(app, expired)[0] == 410
    assert request(app, expired, "POST", params(expired))[0] == 410
    fields = dict(params(url), s="bad")
    assert request(app, "/act?" + urlencode(fields))[0] == 403
    assert request(app, url, "POST", fields)[0] == 403


@pytest.mark.parametrize("action,target,message,repeat", [
    ("done", "done", "marked done", "already done"),
    ("na", "declined", "recorded as N/A", "already N/A"),
])
def test_go_action_and_repeat(links, action, target, message, repeat):
    c, t, app = links
    url = core.sign_link(t["id"], action) + "&go=1"
    status, html, headers = request(app, url)
    assert status == 200 and f'{t["id"]} {message}' in html
    assert "<form" not in html and "&lt;script&gt;" in html and "<script>" not in html
    assert headers[b"content-security-policy"] == b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
    result = core.get(c, t["id"])
    assert result["status"] == target
    assert result["updates"][-1]["note"].startswith(f"act {action} by owner;")
    status, html, _ = request(app, url)
    assert status == 200 and f'{t["id"]} {repeat}' in html
    assert core.get(c, t["id"]) == result
    assert c.execute("SELECT count(*) FROM used_links").fetchone()[0] == 1
    core.act(c, t["id"], "undo")
    before = core.get(c, t["id"])
    assert request(app, url)[0] == 409
    assert core.get(c, t["id"]) == before


@pytest.mark.parametrize("invalid", ["signature", "expired"])
def test_go_invalid_no_change(links, invalid):
    c, t, app = links
    fields = params(core.sign_link(t["id"], "done", -1 if invalid == "expired" else 3600))
    fields["go"] = "1"
    if invalid == "signature":
        fields["s"] = "bad"
    before = core.get(c, t["id"])
    assert request(app, "/act?" + urlencode(fields))[0] == (410 if invalid == "expired" else 403)
    assert core.get(c, t["id"]) == before
    assert c.execute("SELECT count(*) FROM used_links").fetchone()[0] == 0


@pytest.mark.parametrize("go", [None, "0", "true", ""])
def test_get_without_go_one_shows_form(links, go):
    c, t, app = links
    url = core.sign_link(t["id"], "done")
    if go is not None:
        url += "&" + urlencode({"go": go})
    before = core.get(c, t["id"])
    status, html, _ = request(app, url)
    assert status == 200 and '<form method="post"' in html
    assert core.get(c, t["id"]) == before


@pytest.mark.parametrize("action", ["reply", "done", "na"])
def test_go_with_note(links, action):
    c, t, app = links
    text = '<script>note & "reply"</script>'
    url = core.sign_link(t["id"], action, text=text) + "&go=1"
    status, html, _ = request(app, url)
    assert status == 200 and '<form' not in html
    assert 'Note: &lt;script&gt;note &amp; &quot;reply&quot;&lt;/script&gt;' in html
    assert '<script>' not in html
    result = core.get(c, t["id"])
    assert result["updates"][-1]["note"].startswith(f"act {action} by owner: {text}")
    if action == "reply":
        assert f'Reply saved on {t["id"]}' in html
        assert result["status"] == "todo" and result["open_question"] == text
        assert request(app, url)[0] == 200
        assert len(core.get(c, t["id"])["updates"]) == len(result["updates"]) + 1
        assert c.execute("SELECT count(*) FROM used_links").fetchone()[0] == 0
