from contextlib import closing
from datetime import datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
import json

import pytest
from starlette.testclient import TestClient

from backlogr import core
from backlogr import dashboard
from backlogr import server


@pytest.fixture(autouse=True)
def eastern(monkeypatch):
    monkeypatch.setattr(dashboard, 'TZ', ZoneInfo('America/New_York'))


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('BACKLOGR_OWNER', 'Operator')
    monkeypatch.setattr(core, 'DB_PATH', str(tmp_path / 'dashboard.db'))
    with closing(core.connect()) as c:
        yield c


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(server, 'TOKEN', 'dashboard-test-token')
    app, _ = server.build_app()
    with closing(TestClient(app, base_url='http://localhost',
                            headers={'Authorization': 'Bearer dashboard-test-token'})) as client:
        yield client


def make(c, title='Work', status='todo', **kw):
    return core.create(c, title, kw.pop('owner_bot', 'Forge'), 'direct_to_bot', 'Forge',
                       status=status, ask='Which option?' if status == 'waiting_on_owner' else None,
                       evidence='verified' if status == 'done' else None, **kw)


def test_counts_and_freshness(db, client):
    for s in core.STATUSES:
        make(db, s, status=s)
    model = client.get('/dashboard.json').json()
    tasks = core.list_tasks(db, include_closed=True)
    assert model['TOTAL'] == len(tasks)
    assert len(model['WAITING']) == len(core.list_tasks(db, status='waiting_on_owner'))
    assert len(model['PROGRESS']) == len(core.list_tasks(db, status='blocked,in_progress,todo'))
    assert len(model['DONE']) + model['OLDER_DONE'] == len(core.list_tasks(db, status='done'))
    assert model['DECLINED'] == len(core.list_tasks(db, status='declined'))
    t = next(t for t in tasks if t['status'] == 'todo')
    core.update(db, t['id'], 'Forge', status='done', evidence='completed')
    fresh = client.get('/dashboard.json')
    assert fresh.headers['cache-control'] == 'no-store'
    assert t['id'] in [t['id'] for t in fresh.json()['DONE']]
    assert t['id'] not in [t['id'] for t in fresh.json()['PROGRESS']]


@pytest.mark.parametrize('path', ['/dashboard', '/dashboard.json'])
def test_auth_methods_and_hosts(db, monkeypatch, path):
    monkeypatch.setattr(server, 'TOKEN', 'dashboard-test-token')
    monkeypatch.setattr(server, 'TRUST_TAILSCALE', True)
    app, _ = server.build_app()
    with closing(TestClient(app, base_url='http://localhost')) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={'Authorization': 'Bearer wrong'}).status_code == 401
        assert client.get(path, headers={'Tailscale-User-Login': ' '}).status_code == 401
        for headers in ({'Tailscale-User-Login': 'operator@example.com'},
                        {'Authorization': 'Bearer dashboard-test-token'}):
            assert client.get(path, headers=headers).status_code == 200
            head = client.head(path, headers=headers)
            assert head.status_code == 200 and head.content == b''
            for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
                r = client.request(method, path, headers=headers)
                assert r.status_code == 405 and r.headers['allow'] == 'GET, HEAD'
        assert client.get(path, headers={'Host': 'untrusted.example',
                                        'Tailscale-User-Login': 'operator@example.com'}).status_code == 421
        # Tailscale identity is scoped to the dashboard, never API or MCP authorization.
        assert client.post('/api/list', headers={'Tailscale-User-Login': 'operator@example.com'}, json={}).status_code == 401
        monkeypatch.setattr(server, 'TOKEN', '')
        assert client.get(path, headers={'Authorization': 'Bearer '}).status_code == 401
        assert client.get(path, headers={'Tailscale-User-Login': 'operator@example.com'}).status_code == 200


@pytest.mark.parametrize('path', ['/dashboard', '/dashboard.json'])
def test_tailscale_header_ignored_unless_trusted(db, monkeypatch, path):
    monkeypatch.setattr(server, 'TOKEN', 'dashboard-test-token')
    monkeypatch.setattr(server, 'TRUST_TAILSCALE', False)
    app, _ = server.build_app()
    with closing(TestClient(app, base_url='http://localhost')) as client:
        assert client.get(path, headers={'Tailscale-User-Login': 'operator@example.com'}).status_code == 401
        r = client.get(path, headers={'Authorization': 'Bearer dashboard-test-token'})
        assert r.status_code == 200
        assert "frame-ancestors 'none'" in r.headers['content-security-policy']
        assert r.headers['x-content-type-options'] == 'nosniff'
        assert client.get(path, headers={'Authorization': b'Bearer \xff'}).status_code == 401
        assert client.post('/api/list', headers={'Authorization': b'Bearer \xff'}, json={}).status_code == 401


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_html_read_only_and_escaping(db, client):
    t = make(db, '<script>alert("x")</script>', status='waiting_on_owner',
             note='<img src=x onerror=alert(1)>', due='2026-10-27', owner_bot='Mint')
    core.add_link(db, t['id'], 'Forge', 'https://example.com/?q="<script>')
    core.add_link(db, t['id'], 'Forge', 'https://example.com/backlogr/act?token=old')
    core.update(db, t['id'], 'Forge', note='<img src=x onerror=alert(1)>')
    core.rule_add(db, 'R1', 'Check', title='<script>rule</script>', actor='Forge')
    html = client.get('/dashboard').text
    parsed = Tags(html)
    assert not {'form', 'button', 'script', 'img'} & {tag for tag, _ in parsed.tags}
    assert '"/act' not in html and 'Reply above' not in html
    assert '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;' in html
    assert '&lt;img src=x onerror=alert(1)&gt;' in html
    assert '&lt;script&gt;rule&lt;/script&gt;' in html
    assert ('meta', {'http-equiv': 'refresh', 'content': '60'}) in parsed.tags
    for tag, attrs in parsed.tags:
        if tag == 'a':
            assert attrs['target'] == '_blank' and attrs['rel'] == 'noopener'
            assert attrs['href'].startswith('https://') and '/act' not in attrs['href']
        assert tag not in ('script', 'link')
    labels = ['Backlogr Dashboard', 'Open tasks by assignee:', 'Assigned to Operator',
              '>Waiting on Operator</h2>', '>Timeline</h2>', '>Tasks</h2>', 'Rules &amp; playbooks', f'Backlogr {core.__version__}']
    positions = [html.index(label) for label in labels]
    assert positions == sorted(positions)


def test_derivation_and_order(db, tmp_path, monkeypatch):
    cats = tmp_path / 'categories.json'
    cats.write_text(json.dumps([['Reviews', r'review'], ['Infra', r'dns|homelab']]))
    monkeypatch.setenv('BACKLOGR_DASHBOARD_CATEGORIES', str(cats))
    dashboard.categories.cache_clear()
    now = datetime(2026, 10, 26, 12, tzinfo=dashboard.TZ)
    old = now - timedelta(days=3)
    make(db, 'Forge: Code review', status='waiting_on_owner', created_at=old.isoformat(),
         due='2026-10-27', note='latest detail')
    newest = make(db, 'Homelab: DNS', status='waiting_on_owner', created_at=now.isoformat(), owner_bot='Homelab')
    make(db, 'Active', status='in_progress', created_at=now.isoformat())
    blocked = make(db, 'Blocked', status='blocked', created_at=old.isoformat())
    for n in range(42):
        make(db, f'Finished {n}', status='done', created_at=(old + timedelta(minutes=n)).isoformat())
    model = dashboard.load_data(db, now)
    assert model['WAITING'][0]['id'] == newest['id']
    assert model['WAITING'][0]['bot'] == 'Homelab' and model['WAITING'][0]['title'] == 'DNS'
    assert model['WAITING'][0]['detail'] == 'Which option?'
    assert model['WAITING'][0]['cat'] == 'Infra'
    assert model['WAITING'][1]['title'] == 'Code review'
    assert model['WAITING'][1]['cat'] == 'Reviews'
    assert model['WAITING'][1]['due'] == 'Tue Oct 27'
    assert model['PROGRESS'][0]['id'] == blocked['id']
    assert len(model['DONE']) == 40 and model['OLDER_DONE'] == 2
    assert model['DONE'][0]['title'] == 'Finished 41'
    assert model['DONE'][-1]['title'] == 'Finished 2'
    assert dashboard.trunc('status: todo -> in_progress; ready') == 'ready'
    assert model['CATS'] == ['Reviews', 'Infra', 'Other']
    assert dashboard.bot('  Mint ') == 'Mint' and dashboard.bot('') == 'unassigned'
    assert dashboard.trunc('x' * 500, 10) == 'x' * 10 + '…'
    dashboard.categories.cache_clear()
    assert dashboard.link({'ref': 'https://github.com/example/project/pull/7'})['label'] == 'project #7'


def test_timeline_window_limit_timezone_and_actor(db):
    now = datetime(2026, 10, 26, 12, tzinfo=dashboard.TZ)
    t = make(db, 'Timeline', owner_bot='Mint', created_at=(now - timedelta(days=3)).isoformat())
    for n in range(85):
        when = now - timedelta(minutes=n)
        db.execute('INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)',
                   (t['id'], when.isoformat(), 'Homelab' if n == 0 else '', f'event {n}'))
    model = dashboard.load_data(db, now)
    events = [e for d in model['DAYS'] for e in d['events']]
    assert len(events) == 80
    assert model['DAYS'][0]['day'] == 'Mon Oct 26'
    assert events[0] == {'time': '12:00 PM', 'bot': 'Homelab', 'text': f'{t["id"]} Timeline: event 0'}
    assert events[1]['bot'] == 'Mint'
    assert events[-1]['text'].endswith('event 79')


def test_timeline_et_days_and_48_hour_boundary(db):
    now = datetime(2026, 10, 26, 12, tzinfo=dashboard.TZ)
    make(db, 'Boundary', created_at=(now - timedelta(hours=48)).isoformat())
    make(db, 'Too old', created_at=(now - timedelta(hours=48, seconds=1)).isoformat())
    make(db, 'Yesterday', created_at='2026-10-26T03:59:00+00:00')
    make(db, 'Today', created_at='2026-10-26T04:00:00+00:00')
    make(db, 'Future', created_at=(now + timedelta(seconds=1)).isoformat())
    days = dashboard.load_data(db, now)['DAYS']
    assert [d['day'] for d in days] == ['Mon Oct 26', 'Sun Oct 25', 'Sat Oct 24']
    assert days[0]['events'][0]['time'] == '12:00 AM'
    assert days[1]['events'][0]['time'] == '11:59 PM'
    assert sum(len(d['events']) for d in days) == 3


def test_rules_summary_matches_reference_memberships(db):
    core.playbook_add(db, 'P1', 'Checks', skill='checks')
    core.rule_add(db, 'R10', 'Check ten', title='Ten', playbook_id='P1', actor='Forge')
    core.rule_add(db, 'R2', 'Check two', title='Two', playbook_id='P1', actor='Forge')
    core.rule_add(db, 'R3', 'Loose tested', test='pytest', actor='Forge')
    core.rule_add(db, 'R4', 'Explicit untested', test='manual review', testable=False, actor='Forge')
    summary = dashboard.load_data(db)['RULES']
    assert (summary['total'], summary['tested'], summary['untested']) == (4, 1, 3)
    assert summary['playbooks'] == [
        {'name': 'Checks', 'skill': 'checks', 'rules': 2, 'untested': 2},
        {'name': '(no playbook)', 'skill': '', 'rules': 2, 'untested': 1}]
    assert [r['id'] for r in summary['list']] == ['R2', 'R10', 'R4']


def test_requests_only_read_and_close_fresh_connections(db, client, monkeypatch):
    import sqlite3

    make(db)
    before = list(db.iterdump())
    original = core.connect
    opened = []

    class Tracked:
        def __init__(self):
            self.connection = original()
            self.closed = False
            self.connection.set_authorizer(lambda op, *_: sqlite3.SQLITE_DENY if op in (
                sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) else sqlite3.SQLITE_OK)

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __enter__(self):
            return self.connection.__enter__()

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def close(self):
            self.connection.close()
            self.closed = True
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                self.connection.execute('SELECT 1')

    def connect():
        c = Tracked()
        opened.append(c)
        return c

    monkeypatch.setattr(core, 'connect', connect)
    for path in ('/dashboard', '/dashboard.json'):
        assert client.get(path).status_code == 200
    assert len(opened) == 2 and all(c.closed for c in opened)

    assert list(db.iterdump()) == before


def test_timeline_same_timestamp_uses_newest_log_first(db):
    when = datetime(2026, 10, 26, 12, tzinfo=dashboard.TZ)
    task = make(db, created_at=when.isoformat(), note='First')
    db.execute('INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)',
               (task['id'], when.isoformat(), 'Forge', 'Second'))
    db.execute('INSERT INTO updates(task_id,ts,actor,note) VALUES(?,?,?,?)',
               (task['id'], when.isoformat(), 'Forge', 'status: todo -> done;'))
    events = dashboard.load_data(db, when)['DAYS'][0]['events']
    assert events[0]['text'].endswith('status: todo -> done;')
    assert events[1]['text'].endswith('Second')
    assert events[2]['text'].endswith('First')


def test_rule_load_failure_keeps_dashboard_available(db, client, monkeypatch):
    def fail(*args):
        raise core.TaskError('rules unavailable')

    monkeypatch.setattr(core, 'rule_list', fail)
    result = client.get('/dashboard.json')
    assert result.status_code == 200
    assert result.json()['RULES'] == {'total': 0, 'tested': 0, 'untested': 0, 'playbooks': [], 'list': []}
