import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / 'examples' / 'serein-codex-hook' / 'scripts' / 'serein_hook.py'


@pytest.fixture
def hook(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('serein_codex_hook_example', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('PLUGIN_DATA', str(tmp_path))
    monkeypatch.setenv('SEREIN_BASE_URL', 'https://serein.example')
    monkeypatch.setenv('SEREIN_GATEWAY_KEY', 'synthetic-key')
    return module


def test_opt_in_recall_then_archive_completed_turn(hook, monkeypatch):
    calls = []

    def request(path, body):
        calls.append((path, body))
        if path == '/api/hook/recall':
            return {'ok':True, 'recalled_ids':['scene:synthetic'],
                    'additional_context':'Synthetic recalled memory'}
        return {'status':'recorded'}

    monkeypatch.setattr(hook, 'request', request)
    with hook.connect() as connection:
        enabled = hook.on_user(connection, {'session_id':'session-1', 'turn_id':'control',
            'prompt':'serein:on'})
        assert enabled['decision'] == 'block'
        recalled = hook.on_user(connection, {'session_id':'session-1', 'turn_id':'turn-1',
            'prompt':'What did we discuss?'})
        assert 'Synthetic recalled memory' in recalled['hookSpecificOutput']['additionalContext']
        assert calls[0][1]['delivered_ids'] == []
        assert hook.on_stop(connection, {'turn_id':'turn-1',
            'last_assistant_message':'A completed synthetic answer.'}) == {}
        assert connection.execute('SELECT count(*) FROM pending_turns').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM successful_turns').fetchone()[0] == 1
    archive = calls[1][1]
    assert archive['user_message'] == 'What did we discuss?'
    assert archive['assistant_message'] == 'A completed synthetic answer.'
    assert archive['delivered_ids'] == ['scene:synthetic']
    assert archive['client'] == 'codex_local_hook'


def test_disabled_and_interrupted_turns_are_not_archived(hook, monkeypatch):
    calls = []
    monkeypatch.setattr(hook, 'request', lambda path, body: calls.append((path, body)) or {
        'ok':True, 'recalled_ids':[], 'additional_context':''})
    with hook.connect() as connection:
        assert hook.on_user(connection, {'session_id':'session-1', 'turn_id':'disabled',
            'prompt':'No memory here'}) == {}
        hook.on_user(connection, {'session_id':'session-1', 'turn_id':'control', 'prompt':'serein:on'})
        hook.on_user(connection, {'session_id':'session-1', 'turn_id':'turn-1', 'prompt':'Started'})
        hook.on_interrupt(connection, {'turn_id':'turn-1'})
        hook.on_stop(connection, {'turn_id':'turn-1', 'last_assistant_message':'Partial answer'})
        assert connection.execute('SELECT count(*) FROM pending_turns').fetchone()[0] == 0
    assert [path for path, _ in calls] == ['/api/hook/recall']


def test_failed_archive_is_retried_before_next_prompt(hook, monkeypatch):
    attempts = {'archive':0}

    def request(path, body):
        if path == '/api/hook/recall':
            return {'ok':True, 'recalled_ids':[], 'additional_context':''}
        attempts['archive'] += 1
        if attempts['archive'] == 1:
            raise OSError('synthetic outage')
        return {'status':'recorded'}

    monkeypatch.setattr(hook, 'request', request)
    with hook.connect() as connection:
        hook.on_user(connection, {'session_id':'session-1', 'turn_id':'control', 'prompt':'serein:on'})
        hook.on_user(connection, {'session_id':'session-1', 'turn_id':'turn-1', 'prompt':'First'})
        warning = hook.on_stop(connection, {'turn_id':'turn-1', 'last_assistant_message':'Answer'})
        assert 'retry' in warning['systemMessage']
        hook.on_user(connection, {'session_id':'session-1', 'turn_id':'turn-2', 'prompt':'Second'})
        assert connection.execute('SELECT count(*) FROM pending_turns WHERE turn_id=?', ('turn-1',)).fetchone()[0] == 0
    assert attempts['archive'] == 2
