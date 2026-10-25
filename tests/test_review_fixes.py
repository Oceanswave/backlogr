import json
import sqlite3
import urllib.error
from contextlib import closing

import pytest
from starlette.testclient import TestClient

import cli
import core
import server


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'DB_PATH', str(tmp_path / 'tasks.db'))
    monkeypatch.delenv('BACKLOGR_URL', raising=False)
    with closing(core.connect()) as c:
        yield c


@pytest.fixture
def api(db, monkeypatch):
    monkeypatch.setattr(server, 'TOKEN', 'test-token')
    app, _ = server.build_app()
    client = TestClient(app, headers={'Authorization': 'Bearer test-token'})
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize('name', ['owner', 'Sean', 'Pat'])
def test_owner_defaults(db, api, monkeypatch, capsys, name):
    monkeypatch.delenv('BACKLOGR_OWNER', raising=False)
    if name != 'owner': monkeypatch.setenv('BACKLOGR_OWNER', name)
    monkeypatch.setenv('BACKLOGR_HUMANS', name)
    with pytest.raises(core.TaskError, match=f'what {name} must decide'):
        core.create(db, 'x', 'bot', 'via_chukar', 'bot', status='waiting_on_sean')
    for action in ('core', 'cli', 'mcp', 'api'):
        t = core.create(db, action, 'bot', 'via_chukar', 'bot', approval_needed=True)
        if action == 'core': core.act(db, t['id'], 'done')
        elif action == 'cli': assert cli.main(['act', t['id'], 'done']) == 0
        elif action == 'mcp': server.task_act(t['id'], 'done')
        else: assert api.post('/api/act', json={'id': t['id'], 'action': 'done'}).status_code == 200
        assert core.get(db, t['id'])['approved_by'] == name
    assert cli.main(['sean']) == 0
    assert f'waiting on {name}' in capsys.readouterr().out


@pytest.mark.parametrize('op', server.API_FIELDS)
def test_api_unknown_fields(api, op):
    for field in ('_attest', 'unexpected'):
        r = api.post('/api/' + op, json={field: True})
        assert r.status_code == 400
        assert r.json() == {'error': f'unknown field: {field}'}
    if op == 'create':
        for field in ('id', 'created_at', 'approved_at'):
            assert api.post('/api/create', json={field: 'x'}).json() == {'error': f'unknown field: {field}'}


def test_connections_closed(db, api, monkeypatch):
    original = core.connect
    opened = []
    class Tracked:
        def __init__(self):
            self.connection = original()
            self.closed = False
        def __getattr__(self, name): return getattr(self.connection, name)
        def close(self):
            self.connection.close()
            self.closed = True
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                self.connection.execute('SELECT 1')
    def tracked():
        c = Tracked(); opened.append(c); return c
    monkeypatch.setattr(core, 'connect', tracked)
    t = core.create(db, 'x', 'bot', 'via_chukar', 'bot')
    calls = [lambda: server.task_create('new', 'bot', 'via_chukar', 'bot'),
             lambda: server.task_update(t['id'], 'bot', note='n'),
             lambda: server.task_add_link(t['id'], 'bot', 'ref'),
             lambda: server.task_get(t['id']), lambda: server.task_list(),
             lambda: server.task_flags(), lambda: server.task_act(t['id'], 'question', reason='why'),
             lambda: server.task_sean(), lambda: server.task_stale(),
             lambda: server.task_import([], 'bot')]
    for call in calls: call()
    with pytest.raises(core.TaskError): server.task_get('missing')
    assert api.post('/api/list', json={}).status_code == 200
    assert api.post('/api/get', json={'id': 'missing'}).status_code == 400
    assert len(opened) == len(calls) + 3
    assert all(c.closed for c in opened)


def test_link_failure_rolls_back(db):
    t = core.create(db, 'x', 'bot', 'via_chukar', 'bot')
    with pytest.raises(sqlite3.IntegrityError): core.add_link(db, t['id'], None, 'ref')
    assert not db.in_transaction
    assert core.get(db, t['id'])['links'] == []
    with pytest.raises(core.TaskError): core.add_link(db, 'missing', 'bot', 'ref')
    assert not db.in_transaction
    core.add_link(db, t['id'], 'bot', 'works')


def test_pagination(db, api, capsys):
    for n in range(5): core.create(db, f'item {n}', 'bot', 'via_chukar', 'bot', due=f'2026-11-0{n+1}')
    rows = core.list_tasks(db)
    assert core.list_tasks(db, limit=2, offset=1) == rows[1:3]
    assert core.list_tasks(db, offset=3) == rows[3:]
    assert core.list_tasks(db, limit=0) == []
    assert core.list_tasks(db, q='item', limit=1, offset=2) == rows[2:3]
    assert server.task_list(limit=2, offset=1) == rows[1:3]
    assert api.post('/api/list', json={'limit': 2, 'offset': 1}).json() == rows[1:3]
    assert cli.main(['--json', 'list', '--limit', '2', '--offset', '1']) == 0
    assert json.loads(capsys.readouterr().out) == rows[1:3]
    for kw in ({'limit': -1}, {'offset': -1}, {'limit': 1.5}):
        with pytest.raises(core.TaskError): core.list_tasks(db, **kw)


def test_export_import_roundtrip(db, tmp_path):
    t = core.create(db, 'restore', 'bot', 'via_chukar', 'bot', key='restore',
                    created_at='2026-01-01T00:00:00+00:00', done_means='tests pass')
    core.add_link(db, t['id'], 'bot', 'ref', 'doc')
    core.update(db, t['id'], 'bot', approved_by='Pat', approval_source='chat', evidence='sha', test_results={'unit': 'pass'})
    core.act(db, t['id'], 'question', by='Pat', reason='why?')
    waiting = core.create(db, 'waiting', 'bot', 'via_chukar', 'bot', status='waiting_on_sean', ask='approve?')
    core.create(db, 'finished', 'bot', 'bot_initiated', 'bot', status='done', evidence='complete')
    path = str(tmp_path / 'export.json')
    core.export(db, path)
    data = json.loads(open(path).read())
    with closing(core.connect(str(tmp_path / 'restored.db'))) as restored:
        result = core.bulk_import(restored, data, 'restore', restore=True)
        assert result == {'created': [r['id'] for r in data['tasks']], 'skipped': [], 'errors': []}
        core.export(restored, path)
        assert json.loads(open(path).read())['tasks'] == data['tasks']
        assert core.bulk_import(restored, data['tasks'], 'restore')['skipped'] == [r['id'] for r in data['tasks']]
        assert core.get(restored, t['id']) == next(r for r in data['tasks'] if r['id'] == t['id'])
        assert core.get(restored, waiting['id'])['waiting_since'] == waiting['waiting_since']


def test_api_body_cap(api):
    assert api.post('/api/list', content=b'{}' + b' ' * (server.MAX_BODY - 2)).status_code == 200
    assert api.post('/api/list', content=b' ' * (server.MAX_BODY + 1)).status_code == 413
    def chunks():
        yield b' ' * server.MAX_BODY
        yield b' '
    assert api.post('/api/list', content=chunks()).status_code == 413


def test_cli_errors(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('BACKLOGR_URL', 'http://example.invalid')
    monkeypatch.setenv('BACKLOGR_TOKEN_FILE', str(tmp_path / 'missing'))
    assert cli.main(['list']) == 2
    assert capsys.readouterr().err.startswith('error: ')
    monkeypatch.setenv('BACKLOGR_TOKEN_FILE', str(tmp_path))
    assert cli.main(['list']) == 2
    assert capsys.readouterr().err.startswith('error: ')
    monkeypatch.delenv('BACKLOGR_URL')
    bad = tmp_path / 'bad.json'; bad.write_text('{bad')
    assert cli.main(['import', str(bad), '--by', 'bot']) == 2
    assert capsys.readouterr().err.startswith('error: ')
    monkeypatch.setenv('BACKLOGR_URL', 'http://example.invalid')
    token = tmp_path / 'token'; token.write_text('secret'); token.chmod(0o600)
    monkeypatch.setenv('BACKLOGR_TOKEN_FILE', str(token))
    def fail(*a, **kw): raise urllib.error.URLError('unreachable')
    monkeypatch.setattr(cli.urllib.request, 'urlopen', fail)
    assert cli.main(['list']) == 2
    assert 'error:' in capsys.readouterr().err


def test_doctor_and_version(db, tmp_path, monkeypatch, capsys):
    token = tmp_path / 'token'; token.write_text('never-print-this'); token.chmod(0o600)
    monkeypatch.setenv('BACKLOGR_TOKEN_FILE', str(token))
    assert cli.main(['doctor']) == 0
    out = capsys.readouterr().out
    assert 'never-print-this' not in out and 'mode 600' in out and 'schema version 1' in out
    monkeypatch.setattr(core, 'DB_PATH', str(tmp_path))
    assert cli.main(['doctor']) == 1
    capsys.readouterr()
    with pytest.raises(SystemExit) as e: cli.main(['--version'])
    assert e.value.code == 0 and capsys.readouterr().out.strip() == core.__version__


def test_remote_doctor(db, api, tmp_path, monkeypatch, capsys):
    token = tmp_path / 'token'; token.write_text('test-token'); token.chmod(0o600)
    monkeypatch.setenv('BACKLOGR_TOKEN_FILE', str(token))
    monkeypatch.setenv('BACKLOGR_URL', 'http://testserver')
    def urlopen(req, **kw):
        if isinstance(req, str): response = api.get('/healthz')
        else: response = api.post('/api/list', content=req.data)
        import io
        response_file = io.BytesIO(response.content); response_file.status = response.status_code
        return response_file
    monkeypatch.setattr(cli.urllib.request, 'urlopen', urlopen)
    assert cli.main(['doctor']) == 0
    assert 'authenticated list: OK' in capsys.readouterr().out
    def fail(*a, **kw): raise urllib.error.URLError('unreachable')
    monkeypatch.setattr(cli.urllib.request, 'urlopen', fail)
    assert cli.main(['doctor']) == 1


def test_doctor_schema_and_permissions(db, tmp_path, monkeypatch, capsys):
    db.execute("PRAGMA user_version=999")
    assert cli.main(['doctor']) == 1
    assert 'schema version mismatch' in capsys.readouterr().err
    db.execute("PRAGMA user_version=1")
    path = tmp_path / 'tasks.db'
    path.chmod(0o400)
    try:
        assert cli.main(['doctor']) == 1
        assert 'not writable' in capsys.readouterr().err
    finally:
        path.chmod(0o600)


def test_import_transport_formats(db, api):
    item = {'id': 'T-restore', 'created_at': '2026-01-01T00:00:00+00:00',
            'title': 'restore', 'owner_bot': 'bot', 'source': 'via_chukar'}
    assert api.post('/api/import', json={'items': {'tasks': [item]}, 'actor': 'bot'}).json()['created'] == ['T-restore']
    snapshot = core.get(db, 'T-restore')
    assert server.task_import({'tasks': [snapshot]}, 'bot')['skipped'] == ['T-restore']


@pytest.mark.parametrize('transport', ['api', 'mcp', 'core'])
@pytest.mark.parametrize('fields,error', [
    ({'approval_needed': True, 'evidence': 'result'}, 'approval'),
    ({}, 'evidence'),
    ({'done_means': 'tests pass', 'evidence': 'result'}, 'test'),
])
def test_snapshot_import_enforces_gates(db, api, transport, fields, error):
    item = dict(id='T-snapshot', title='snapshot', owner_bot='bot', source='via_chukar',
                status='done', updates=[], updated_at='2026-01-01T00:00:00+00:00', **fields)
    if transport == 'api':
        response = api.post('/api/import', json={'items': [item], 'actor': 'bot'})
        assert response.status_code == 200
        result = response.json()
    elif transport == 'mcp':
        result = server.task_import([item], 'bot')
    else:
        result = core.bulk_import(db, [item], 'bot')
    assert result['created'] == []
    assert error in result['errors'][0]['error'].lower()
    assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_local_cli_restore_roundtrip(db, tmp_path, monkeypatch, capsys):
    task = core.create(db, 'snapshot', 'bot', 'via_chukar', 'bot', approval_needed=True)
    core.add_link(db, task['id'], 'bot', 'result', 'doc')
    path = str(tmp_path / 'snapshot.json')
    core.export(db, path)
    original = json.loads(open(path).read())['tasks']
    monkeypatch.setattr(core, 'DB_PATH', str(tmp_path / 'restored.db'))
    assert cli.main(['import', path, '--by', 'bot', '--restore']) == 0
    capsys.readouterr()
    with closing(core.connect()) as restored:
        core.export(restored, path)
    assert json.loads(open(path).read())['tasks'] == original


def test_remote_restore_rejected_before_connect(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('BACKLOGR_URL', 'http://example.invalid')
    def unexpected():
        pytest.fail('remote restore must not connect')
    monkeypatch.setattr(cli, 'backend', unexpected)
    assert cli.main(['import', str(tmp_path / 'unused'), '--by', 'bot', '--restore']) == 2
    assert '--restore is only available in local mode' in capsys.readouterr().err


def test_api_cannot_enable_restore(api):
    response = api.post('/api/import', json={'items': [], 'actor': 'bot', 'restore': True})
    assert response.status_code == 400
    assert response.json() == {'error': 'unknown field: restore'}


@pytest.mark.parametrize('transport', ['api', 'mcp'])
def test_snapshot_import_uses_create(db, api, transport):
    task = core.create(db, 'original', 'bot', 'via_chukar', 'bot')
    snapshot = core.get(db, task['id'])
    snapshot['id'] = 'T-new-snapshot'
    snapshot['updates'] = [{'ts': snapshot['created_at'], 'actor': 'forged', 'note': 'forged history'}]
    if transport == 'api':
        result = api.post('/api/import', json={'items': [snapshot], 'actor': 'importer'}).json()
    else:
        result = server.task_import([snapshot], 'importer')
    assert result == {'created': ['T-new-snapshot'], 'skipped': [], 'errors': []}
    imported = core.get(db, 'T-new-snapshot')
    assert imported['updates'][0]['actor'] == 'importer'
    assert imported['updates'][0]['note'] == 'created (todo)'
