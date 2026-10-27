import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from backlogr import cli
from backlogr import core
from backlogr import server


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'DB_PATH', str(tmp_path / 'rules.db'))
    monkeypatch.delenv('BACKLOGR_URL', raising=False)
    with closing(core.connect()) as c:
        yield c


def seed():
    return json.loads((Path(__file__).parent / 'fixtures/rules-playbooks.json').read_text())


@pytest.mark.parametrize('test,expected', [('', False), ('  ', False), ('untested: review', False), ('UNTESTED', False), ('pytest', True)])
def test_derivation(db, test, expected):
    assert core.rule_add(db, 'R', rule='Check it', test=test)['testable'] is expected
    assert bool(core.rule_untested(db)) is (not expected)
    if not test.strip():
        with pytest.raises(core.TaskError):
            core.rule_add(db, 'X', rule='Check', test=test, testable=True)
    else:
        assert core.rule_add(db, 'X', rule='Check', test=test, testable=True)['testable'] is expected


def test_rules_and_links(db):
    r = core.rule_add(db, 'R', rule='Check', title='Checks', actor='Agent', test='pytest', category='quality')
    with pytest.raises(core.TaskError): core.rule_add(db, 'R', rule='Duplicate')
    with pytest.raises(core.TaskError): core.rule_add(db, 'empty', rule=' ')
    with pytest.raises(core.TaskError): core.rule_get(db, 'missing')
    with pytest.raises(core.TaskError): core.playbook_add(db, 'P', 'Checks', rule_ids=['missing'])
    assert core.playbook_list(db) == []
    core.playbook_add(db, 'P', 'Checks', rule_ids=['R', 'R'])
    core.playbook_add(db, 'Q', 'Other', rule_ids=['R'])
    assert core.playbook_get(db, 'P')['rules'] == [r]
    assert core.rule_list(db, playbook='Q', testable=True, category='quality') == [r]
    assert core.rule_list(db, testable=False) == []
    replaced = core.rule_add(db, 'R', rule='Updated', replace=True, actor='Other', playbook_id='P')
    assert replaced['created_by'] == 'Agent' and replaced['created_at'] == r['created_at']
    assert len(core.playbook_get(db, 'P')['rules']) == 1
    with pytest.raises(core.TaskError): core.rule_add(db, 'bad', rule='Check', playbook_id='missing')


def test_import(db):
    data = seed()
    assert core.rules_import(db, data, 'Agent') == {'created': 3, 'updated': 0, 'unchanged': 0}
    before = core.playbook_get(db, 'P-check')
    assert core.rules_import(db, data, 'Other') == {'created': 0, 'updated': 0, 'unchanged': 3}
    assert core.playbook_get(db, 'P-check') == before
    data['rules'][0]['rule'] = 'Updated text'
    assert core.rules_import(db, data, 'Agent') == {'created': 0, 'updated': 1, 'unchanged': 2}
    bad = {'rules': [{'id': 'new', 'rule': 'Check'}], 'playbooks': [{'id': 'bad', 'name': 'Bad', 'rule_ids': ['unknown']}]}
    with pytest.raises(core.TaskError): core.rules_import(db, bad, 'Agent')
    assert {r['id'] for r in core.rule_list(db)} == {'M1', 'M2'}
    assert not db.in_transaction


def test_migration(tmp_path):
    path = str(tmp_path / 'old.db')
    with closing(sqlite3.connect(path)) as old:
        old.executescript('''CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT NOT NULL,
        done_means TEXT NOT NULL DEFAULT '', owner_bot TEXT NOT NULL, due TEXT,
        status TEXT NOT NULL DEFAULT 'todo', source TEXT NOT NULL,
        approval_needed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO tasks VALUES('T-old','Existing','','Agent',NULL,'todo','direct_to_bot',0,'then','then');
        CREATE TABLE updates(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
        ts TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL);
        INSERT INTO updates(task_id,ts,actor,note) VALUES('T-old','then','Agent','created');
        CREATE TABLE links(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
        ts TEXT NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'other');
        INSERT INTO links(task_id,ts,ref,kind) VALUES('T-old','then','example evidence','other');
        PRAGMA user_version=1;''')
    for _ in range(2):
        with closing(core.connect(path)) as c:
            existing = core.get(c, 'T-old')
            assert existing['title'] == 'Existing'
            assert existing['updates'][0]['note'] == 'created'
            assert existing['links'][0]['ref'] == 'example evidence'
            assert c.execute('PRAGMA user_version').fetchone()[0] == core.SCHEMA_VERSION
            assert core.rules_import(c, seed(), 'Agent')['created'] in (0, 3)
            assert len(core.rule_list(c)) == 2


def test_cli(db, capsys, tmp_path):
    def run(args):
        assert cli.main(args) == 0
        return json.loads(capsys.readouterr().out)
    fixture = str(Path(__file__).parent / 'fixtures/rules-playbooks.json')
    assert run(['--json', 'rules', 'import', fixture, '--by', 'Agent'])['created'] == 3
    assert run(['rules', 'list', '--json', '--playbook', 'P-check', '--testable', 'yes'])[0]['id'] == 'M1'
    assert run(['rules', '--json', 'list', '--untested'])[0]['id'] == 'M2'
    assert run(['rules', 'untested', '--json'])[0]['id'] == 'M2'
    assert run(['rules', 'get', 'M1', '--json'])['rule']
    assert run(['playbooks', 'list', '--json'])[0]['id'] == 'P-check'
    assert len(run(['playbooks', 'get', 'P-check', '--json'])['rules']) == 2
    run(['rules', 'add', 'R', '--rule', 'Check', '--test', 'pytest', '--evidence', 'output', '--source', 'guide', '--playbook', 'P-check', '--json'])
    assert cli.main(['rules', 'add', 'R', '--rule', 'Check']) == 2
    capsys.readouterr()
    run(['rules', 'add', 'R', '--rule', 'Updated', '--replace', '--json'])
    assert run(['playbooks', 'add', 'Q', '--name', 'Checks', '--rules', 'R,M1', '--test', 'pytest', '--json'])['rule_ids'] == ['M1', 'R']


def test_mcp_and_api(db, monkeypatch):
    monkeypatch.setattr(server, 'TOKEN', 'fixture-token')
    assert server.rules_import(seed(), 'Agent')['created'] == 3
    assert server.rule_get('M1')['testable'] is True
    assert server.rule_list(category='quality')[0]['id'] == 'M1'
    assert server.rule_untested()[0]['id'] == 'M2'
    server.rule_add('R', rule='Check')
    server.playbook_add('Q', 'Other', rule_ids=['R'])
    assert len(server.playbook_list()) == 2
    assert server.playbook_get('Q')['rules'][0]['id'] == 'R'
    app, _ = server.build_app()
    calls = {'rules_import': {'items': seed(), 'actor': 'Agent'}, 'rule_get': {'id': 'M1'},
             'rule_list': {'playbook': 'P-check'}, 'rule_untested': {},
             'rule_add': {'id': 'API', 'rule': 'Check'}, 'playbook_list': {},
             'playbook_get': {'id': 'Q'}, 'playbook_add': {'id': 'API-P', 'name': 'API', 'rule_ids': ['API']}}
    with TestClient(app) as client:
        for op, args in calls.items():
            assert client.post('/api/' + op, json=args).status_code == 401
            response = client.post('/api/' + op, json=args, headers={'Authorization': 'Bearer fixture-token'})
            assert response.status_code == 200, response.text
        assert client.post('/api/rule_add', json={'id': 'API', 'rule': 'duplicate'}, headers={'Authorization': 'Bearer fixture-token'}).status_code == 400


def test_remote(db, monkeypatch):
    calls = []
    remote = cli.Remote('https://example.invalid', 'fixture-token')
    monkeypatch.setattr(remote, '_call', lambda op, **args: calls.append((op, args)))
    for op, args in [('rule_list', {}), ('rule_get', {'id': 'R'}), ('rule_add', {'id': 'R', 'rule': 'Check'}),
                     ('rule_untested', {}), ('playbook_list', {}), ('playbook_get', {'id': 'P'}),
                     ('playbook_add', {'id': 'P', 'name': 'Checks'}), ('rules_import', {'items': seed(), 'actor': 'Agent'})]:
        getattr(remote, op)(None, **args)
        assert calls[-1] == (op, args)


def test_import_portable_fields(db):
    data = {'rules': [{'id': 'R', 'rule': 'Check', 'playbook': 'P', 'applies_to': ['docs', 'code']}],
            'playbooks': [{'id': 'P', 'name': 'Checks', 'rule_ids': ['R']}]}
    assert core.rules_import(db, data, 'Agent')['created'] == 2
    assert core.rule_get(db, 'R')['playbook_id'] == 'P'
    assert json.loads(core.rule_get(db, 'R')['applies_to']) == ['docs', 'code']
    assert core.rules_import(db, data, 'Agent')['unchanged'] == 2


@pytest.mark.parametrize('items', [[], {'rules': 'bad'}, {'rules': [{'id': 'R'}]},
    {'rules': [{'id': 'R', 'rule': 'Check', 'unknown': True}]},
    {'rules': [{'id': 'R', 'rule': 'Check'}, {'id': 'R', 'rule': 'Again'}]},
    {'playbooks': [{'id': 'P', 'name': 'Checks', 'rule_ids': 'R'}]}])
def test_invalid_import_rolls_back(db, items):
    with pytest.raises(core.TaskError): core.rules_import(db, items, 'Agent')
    assert core.rule_list(db) == [] and core.playbook_list(db) == []
    assert not db.in_transaction


@pytest.mark.parametrize('creation', ['add', 'import'])
@pytest.mark.parametrize('query', ['core', 'cli_untested', 'cli_list', 'mcp', 'api'])
def test_untested_returns_all_non_testable_rules(db, capsys, monkeypatch, creation, query):
    rules = [
        {'id': 'R-empty', 'rule': 'Check empty test'},
        {'id': 'R-placeholder', 'rule': 'Check placeholder', 'test': 'UNTESTED: review'},
        {'id': 'R-manual', 'rule': 'Check manually', 'test': 'manual check', 'testable': False},
        {'id': 'R-testable', 'rule': 'Check automatically', 'test': 'pytest', 'testable': True},
    ]
    if creation == 'import':
        core.rules_import(db, {'rules': rules}, 'Agent')
    else:
        for rule in rules:
            core.rule_add(db, **rule, actor='Agent')

    if query == 'core':
        result = core.rule_untested(db)
    elif query in ('cli_untested', 'cli_list'):
        args = ['untested'] if query == 'cli_untested' else ['list', '--untested']
        assert cli.main(['rules', *args, '--json']) == 0
        result = json.loads(capsys.readouterr().out)
    elif query == 'mcp':
        result = server.rule_untested()
    else:
        monkeypatch.setattr(server, 'TOKEN', 'fixture-token')
        app, _ = server.build_app()
        client = TestClient(app)
        try:
            response = client.post('/api/rule_untested', json={},
                                   headers={'Authorization': 'Bearer fixture-token'})
        finally:
            client.close()
        assert response.status_code == 200, response.text
        result = response.json()

    assert [r['id'] for r in result] == ['R-empty', 'R-manual', 'R-placeholder']
    assert all(r['testable'] is False for r in result)


def test_mcp_registration():
    import asyncio
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {'rule_list', 'rule_get', 'rule_add', 'rule_untested', 'playbook_list',
            'playbook_get', 'playbook_add', 'rules_import'} <= names


def test_membership_changes_update_playbook_timestamp(db, monkeypatch):
    core.playbook_add(db, 'P', 'Checks')
    monkeypatch.setattr(core, 'now', lambda: '2030-01-01T00:00:00+00:00')
    core.rule_add(db, 'R', rule='Check', playbook_id='P')
    assert core.playbook_get(db, 'P')['updated_at'] == core.now()
    monkeypatch.setattr(core, 'now', lambda: '2030-01-02T00:00:00+00:00')
    core.rule_add(db, 'R', rule='Check', replace=True)
    assert core.playbook_get(db, 'P')['updated_at'] == core.now()
    assert core.playbook_get(db, 'P')['rules'] == []


def test_doctor_rules_schema(db, capsys):
    db.execute('DROP TABLE playbook_rules')
    # A healthy connection recreates the table; schema inspection also covers it.
    assert cli.main(['doctor']) == 0
    assert 'columns: OK' in capsys.readouterr().out


def test_export_round_trips_rules_and_playbooks(db, tmp_path):
    assert core.rules_import(db, seed(), 'Agent')['created'] > 0
    core.rule_add(db, 'manual', rule='Look', test='manual review', testable=False, actor='Agent')
    core.create(db, 'task', 'Agent', 'bot_initiated', 'Agent')
    before = core.rules_snapshot(db)
    out = core.export(db, str(tmp_path / 'e.json'))
    data = json.loads(Path(out['path']).read_text())
    assert data['rules'] == before['rules'] and data['playbooks'] == before['playbooks']
    for restore in (False, True):
        with closing(core.connect(str(tmp_path / f'fresh-{restore}.db'))) as fresh:
            result = core.bulk_import(fresh, data, 'Agent', restore=restore)
            assert result['errors'] == [] and len(result['created']) == 1
            assert result['rules']['created'] == len(before['rules']) + len(before['playbooks'])
            assert core.rules_snapshot(fresh) == before
    # rules-only objects import too, and repeating is a no-op
    again = core.bulk_import(db, {'rules': before['rules'], 'playbooks': before['playbooks']}, 'Agent')
    assert again['created'] == [] and again['rules']['created'] == again['rules']['updated'] == 0


def test_remove_rules_and_playbooks(db, capsys):
    core.playbook_add(db, 'P1', 'Checks')
    core.playbook_add(db, 'P2', 'Other')
    core.rule_add(db, 'R1', 'one', playbook_id='P1')
    core.rule_add(db, 'R2', 'two', playbook_id='P1')
    core.playbook_add(db, 'P2', 'Other', rule_ids=['R1'])
    assert core.rule_remove(db, 'R1') == {'removed': 'R1', 'playbooks': ['P1', 'P2']}
    assert core.playbook_get(db, 'P1')['rule_ids'] == ['R2'] and core.playbook_get(db, 'P2')['rule_ids'] == []
    with pytest.raises(core.TaskError, match='unknown rule'): core.rule_remove(db, 'R1')
    assert core.playbook_remove(db, 'P1') == {'removed': 'P1', 'detached_rules': ['R2']}
    assert core.rule_get(db, 'R2')['playbook_id'] is None
    with pytest.raises(core.TaskError, match='unknown playbook'): core.playbook_remove(db, 'P1')
    assert server.playbook_remove('P2') == {'removed': 'P2', 'detached_rules': []}
    assert cli.main(['rules', 'remove', 'R2']) == 0 and '"removed": "R2"' in capsys.readouterr().out
    assert core.rule_list(db) == [] and core.playbook_list(db) == []
