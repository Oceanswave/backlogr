import json
import os
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


@pytest.mark.parametrize('name', ['owner', 'Operator', 'Pat'])
def test_owner_defaults(db, api, monkeypatch, capsys, name):
    monkeypatch.delenv('BACKLOGR_OWNER', raising=False)
    if name != 'owner': monkeypatch.setenv('BACKLOGR_OWNER', name)
    monkeypatch.setenv('BACKLOGR_HUMANS', name)
    with pytest.raises(core.TaskError, match=f'what {name} must decide'):
        core.create(db, 'x', 'bot', 'via_coordinator', 'bot', status='waiting_on_owner')
    for action in ('core', 'cli', 'mcp', 'api'):
        t = core.create(db, action, 'bot', 'via_coordinator', 'bot', approval_needed=True)
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
    t = core.create(db, 'x', 'bot', 'via_coordinator', 'bot')
    calls = [lambda: server.task_create('new', 'bot', 'via_coordinator', 'bot'),
             lambda: server.task_update(t['id'], 'bot', note='n'),
             lambda: server.task_add_link(t['id'], 'bot', 'ref'),
             lambda: server.task_get(t['id']), lambda: server.task_list(),
             lambda: server.task_flags(), lambda: server.task_act(t['id'], 'question', reason='why'),
             lambda: server.task_waiting(), lambda: server.task_stale(),
             lambda: server.task_import([], 'bot')]
    for call in calls: call()
    with pytest.raises(core.TaskError): server.task_get('missing')
    assert api.post('/api/list', json={}).status_code == 200
    assert api.post('/api/get', json={'id': 'missing'}).status_code == 400
    assert len(opened) == len(calls) + 3
    assert all(c.closed for c in opened)


def test_link_failure_rolls_back(db):
    t = core.create(db, 'x', 'bot', 'via_coordinator', 'bot')
    with pytest.raises(sqlite3.IntegrityError): core.add_link(db, t['id'], None, 'ref')
    assert not db.in_transaction
    assert core.get(db, t['id'])['links'] == []
    with pytest.raises(core.TaskError): core.add_link(db, 'missing', 'bot', 'ref')
    assert not db.in_transaction
    core.add_link(db, t['id'], 'bot', 'works')


def test_pagination(db, api, capsys):
    for n in range(5): core.create(db, f'item {n}', 'bot', 'via_coordinator', 'bot', due=f'2026-11-0{n+1}')
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
    t = core.create(db, 'restore', 'bot', 'via_coordinator', 'bot', key='restore',
                    created_at='2026-01-01T00:00:00+00:00', done_means='tests pass')
    core.add_link(db, t['id'], 'bot', 'ref', 'doc')
    core.update(db, t['id'], 'bot', approved_by='Pat', approval_source='chat', evidence='sha', test_results={'unit': 'pass'})
    core.act(db, t['id'], 'question', by='Pat', reason='why?')
    waiting = core.create(db, 'waiting', 'bot', 'via_coordinator', 'bot', status='waiting_on_owner', ask='approve?')
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
    monkeypatch.setattr(cli._opener, 'open', fail)
    assert cli.main(['list']) == 2
    assert 'error:' in capsys.readouterr().err


def test_doctor_and_version(db, tmp_path, monkeypatch, capsys):
    token = tmp_path / 'token'; token.write_text('never-print-this'); token.chmod(0o600)
    monkeypatch.setenv('BACKLOGR_TOKEN_FILE', str(token))
    assert cli.main(['doctor']) == 0
    out = capsys.readouterr().out
    assert 'never-print-this' not in out and 'mode 600' in out and f'schema version {core.SCHEMA_VERSION}' in out
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
    monkeypatch.setattr(cli._opener, 'open', urlopen)
    assert cli.main(['doctor']) == 0
    assert 'authenticated list: OK' in capsys.readouterr().out
    def fail(*a, **kw): raise urllib.error.URLError('unreachable')
    monkeypatch.setattr(cli.urllib.request, 'urlopen', fail)
    monkeypatch.setattr(cli._opener, 'open', fail)
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
            'title': 'restore', 'owner_bot': 'bot', 'source': 'via_coordinator'}
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
    item = dict(id='T-snapshot', title='snapshot', owner_bot='bot', source='via_coordinator',
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
    task = core.create(db, 'snapshot', 'bot', 'via_coordinator', 'bot', approval_needed=True)
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
    task = core.create(db, 'original', 'bot', 'via_coordinator', 'bot')
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


def test_legacy_names_are_accepted_and_migrated(db, api, tmp_path, capsys):
    t = core.create(db, 'legacy', 'bot', 'via_chukar', 'bot', status='waiting_on_sean', ask='ok?')
    assert (t['status'], t['source']) == ('waiting_on_owner', 'via_coordinator')
    assert core.update(db, t['id'], 'bot', status='todo')['status'] == 'todo'
    assert core.update(db, t['id'], 'bot', status='waiting_on_sean', ask='ok?')['status'] == 'waiting_on_owner'
    assert [x['id'] for x in core.list_tasks(db, status='waiting_on_sean', source='via_chukar')] == [t['id']]
    assert [x['id'] for x in api.post('/api/sean', json={}).json()] == [t['id']]
    assert cli.main(['sean']) == 0 and t['id'] in capsys.readouterr().out
    assert cli.main(['new', 'x', '--owner', 'bot', '--source', 'via_chukar', '--by', 'bot']) == 0
    # a pre-2.0 database with the old stored names is rewritten on connect
    path = str(tmp_path / 'v2.db')
    with closing(core.connect(path)) as c:
        c.execute("INSERT INTO tasks(id,title,owner_bot,status,source,created_at,updated_at) "
                  "VALUES('T-v2','old','bot','waiting_on_sean','via_chukar','2026-01-01','2026-01-01')")
        c.execute('PRAGMA user_version=2')
    with closing(core.connect(path)) as c:
        row = core.get(c, 'T-v2', with_log=False)
        assert (row['status'], row['source']) == ('waiting_on_owner', 'via_coordinator')
        assert c.execute('PRAGMA user_version').fetchone()[0] == core.SCHEMA_VERSION


def test_people_who_act_can_still_approve(db, monkeypatch):
    monkeypatch.setenv('BACKLOGR_OWNER', 'Pat')
    monkeypatch.delenv('BACKLOGR_HUMANS', raising=False)
    t = core.create(db, 'gated', 'bot', 'direct_to_bot', 'bot', approval_needed=True)
    core.act(db, t['id'], 'later', reason='after lunch')                  # owner acts: stays a human
    core.act(db, t['id'], 'question', by='Alex', reason='which one?')    # another person acts
    assert core.update(db, t['id'], 'bot', approved_by='Pat', approval_source='Pat in chat')['approved_by'] == 'Pat'
    assert core.update(db, t['id'], 'bot', status='in_progress')['status'] == 'in_progress'
    with pytest.raises(core.TaskError, match='is a bot'):
        core.update(db, t['id'], 'bot', approved_by='bot', approval_source='self')


def test_created_at_is_validated(db):
    with pytest.raises(core.TaskError, match='created_at'):
        core.create(db, 'x', 'bot', 'direct_to_bot', 'bot', created_at='garbage')
    result = core.bulk_import(db, [dict(title='y', owner_bot='bot', source='direct_to_bot', created_at='nope')], 'bot')
    assert result['created'] == [] and 'created_at' in result['errors'][0]['error']
    assert core.flags(db)['created_recently'] == []


def test_exports_and_backups_are_private(db, tmp_path, monkeypatch):
    core.create(db, 'x', 'bot', 'direct_to_bot', 'bot')
    out = core.export(db, str(tmp_path / 'exports' / 'e.json'))
    assert oct(os.stat(out['path']).st_mode & 0o777) == '0o600'
    assert json.load(open(out['path']))['tasks'][0]['title'] == 'x'
    assert not list((tmp_path / 'exports').glob('*.tmp'))
    monkeypatch.setattr(server, 'BACKUP_DIR', str(tmp_path / 'backups'))
    monkeypatch.setattr(server, 'BACKUP_KEEP', 1)
    path = server.backup()
    assert oct(os.stat(path).st_mode & 0o777) == '0o600'


def test_due_can_be_cleared(db, capsys, tmp_path):
    t = core.create(db, 'dated', 'bot', 'direct_to_bot', 'bot', due='2026-11-01')
    assert core.create(db, 'blank', 'bot', 'direct_to_bot', 'bot', due='  ')['due'] is None
    cleared = core.update(db, t['id'], 'bot', due='')
    assert cleared['due'] is None and cleared['updates'][-1]['note'] == 'due: 2026-11-01 -> none'
    with pytest.raises(core.TaskError, match='nothing to update'):
        core.update(db, t['id'], 'bot', due='')
    assert cli.main(['update', t['id'], '--by', 'bot', '--due', '2026-12-01']) == 0
    assert cli.main(['update', t['id'], '--by', 'bot', '--due', 'none']) == 0
    assert core.get(db, t['id'])['due'] is None
    assert server.task_update(t['id'], 'bot', due='2026-12-02')['due'] == '2026-12-02'
    assert server.task_update(t['id'], 'bot', due='')['due'] is None
    # a pre-2.1 database that stored a cleared due as '' is normalized on connect
    path = str(tmp_path / 'v3.db')
    with closing(core.connect(path)) as c:
        c.execute("INSERT INTO tasks(id,title,owner_bot,status,source,due,created_at,updated_at) "
                  "VALUES('T-v3','old','bot','todo','direct_to_bot','','2026-01-01','2026-01-01')")
        c.execute('PRAGMA user_version=3')
    with closing(core.connect(path)) as c:
        assert core.get(c, 'T-v3', with_log=False)['due'] is None
