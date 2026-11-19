import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from bench import study_connector as connector, study_setup


def test_qr_is_local_private_refreshable_and_updates(monkeypatch):
    opened = []
    monkeypatch.setattr(study_setup.subprocess, 'run', lambda args, **kw: opened.append(args[1]))
    monkeypatch.setattr(study_setup.time, 'sleep', lambda _: None)
    with study_setup.qr_window('WhatsApp', 'Scan <here>') as show:
        show('synthetic-qr-one')
        url = opened[0]
        assert url.startswith('http://127.0.0.1:')
        with urlopen(url) as response:
            first = response.read()
            assert response.headers['Cache-Control'] == 'no-store'
            assert b'<svg' in first and b'Scan &lt;here&gt;' in first
            assert b'content="2"' in first
        show('synthetic-qr-two')
        assert len(opened) == 1
        assert urlopen(url).read() != first
        with pytest.raises(HTTPError) as error:
            urlopen(url.rsplit('/', 1)[0] + '/wrong-token')
        assert error.value.code == 404
        with pytest.raises(HTTPError):
            urlopen(Request(url, headers={'Host': 'untrusted.example'}))


def test_whatsapp_setup_uses_browser_callback_without_terminal(monkeypatch):
    from contextlib import contextmanager
    from bench.channels import whatsapp
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(whatsapp, 'BINARY', SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr(connector, 'terminal_command', lambda *a: pytest.fail('Terminal opened'))
    @contextmanager
    def window(*args):
        calls.append(args)
        yield lambda code: calls.append(code)
    monkeypatch.setattr(study_setup, 'qr_window', window)
    monkeypatch.setattr(whatsapp, 'link', lambda show: show('test-qr'))
    connector.setup_channel('whatsapp')
    assert calls[0][0] == 'WhatsApp' and calls[1] == 'test-qr'


def test_missing_telegram_credentials_are_actionable(monkeypatch):
    from bench.channels import telegram
    def missing():
        raise telegram.TelegramError('missing')
    monkeypatch.setattr(telegram, 'api_credentials', missing)
    with pytest.raises(connector.ConnectorError, match='study administrator'):
        connector.setup_channel('telegram')


def test_unconfigured_channel_collects_address_without_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(connector, 'load_agents', lambda: {})
    monkeypatch.setattr(connector, 'configure_agent', lambda *args: calls.append(args))
    monkeypatch.setattr(connector, 'terminal_command', lambda *args: pytest.fail('Terminal opened'))
    monkeypatch.setattr(connector, 'request', lambda *args: {'accepted': True})
    connector.run_job('https://study.example', 'test-credential', {'id': 'test', 'kind': 'setup', 'agent': 'instinct', 'channel': 'whatsapp'})
    assert calls == [('instinct', 'whatsapp')]


def test_whatsapp_setup_reprompts_for_saved_address_every_time(monkeypatch):
    from types import SimpleNamespace
    calls, reports = [], []
    monkeypatch.setattr(connector, 'load_agents', lambda: {
        'instinct-whatsapp': SimpleNamespace(channel='whatsapp', handle='+15550100001')})
    monkeypatch.setattr(connector, 'configure_agent', lambda *args: calls.append(args))
    monkeypatch.setattr(connector, 'setup_channel', lambda *args: pytest.fail('Skipped address entry'))
    def report(origin, action, body, credential):
        reports.append(body)
        return {'accepted': True}
    monkeypatch.setattr(connector, 'request', report)
    for job_id in ('first', 'reconnect'):
        connector.run_job('https://study.example', 'test-credential', {
            'id': job_id, 'kind': 'setup', 'agent': 'instinct-whatsapp', 'channel': 'whatsapp'})
    assert calls == [('instinct-whatsapp', 'whatsapp')] * 2
    assert [r['state'] for r in reports] == ['done', 'done']


def test_imessage_signin_only_opens_messages(monkeypatch):
    from types import SimpleNamespace
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[:2] == ['launchctl', 'print'] else 0)
    monkeypatch.setattr(connector.subprocess, 'run', run)
    monkeypatch.setattr(connector, 'terminal_command', lambda *args: pytest.fail('Terminal opened'))
    connector.setup_channel('imessage')
    assert calls == [['open', '-a', 'Messages']]


def test_imessage_contact_saves_without_opening_other_windows(monkeypatch, tmp_path):
    import yaml
    path = tmp_path / 'agents.local.yaml'
    monkeypatch.setattr(connector, 'LOCAL_AGENTS_FILE', path)
    monkeypatch.setattr(connector, 'address_dialog', lambda *a: '+15550100001')
    monkeypatch.setattr(connector, 'setup_channel', lambda *a: pytest.fail('Opened another window'))
    monkeypatch.setattr(connector.subprocess, 'run', lambda *a, **k: pytest.fail('Started installation'))
    connector.configure_agent('instinct', 'imessage', open_channel=False)
    assert yaml.safe_load(path.read_text())['agents']['instinct']['handle'] == '+15550100001'


def test_imessage_permission_step_installs_silently_once(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(connector.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(connector, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(connector, 'load_agents', lambda: {
        'instinct': SimpleNamespace(name='instinct', channel='imessage', handle='+15550100001')})
    binary = tmp_path / 'Library/Application Support/bench-reader/bench-reader'
    install = [str(connector.ROOT / 'reader/install.sh'), '--no-open']
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args == install:
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b'test-reader')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(connector.subprocess, 'run', run)
    connector.prepare_imessage_reader()
    connector.prepare_imessage_reader()
    assert calls.count(install) == 1
    assert [c for c in calls if c[0] == 'open'] == [['open', connector.SETTINGS]] * 2


@pytest.mark.parametrize('step,expected', [('CHANNEL SETUP', 'contact'), ('IMESSAGE CONTACT', 'contact'),
                                         ('IMESSAGE SIGNIN', 'signin'), ('IMESSAGE PERMISSIONS', 'permissions')])
def test_imessage_job_runs_only_the_requested_step(monkeypatch, step, expected):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(connector, 'load_agents', lambda: {'instinct': SimpleNamespace(channel='imessage')})
    monkeypatch.setattr(connector, 'configure_agent', lambda *a, **k: calls.append(('contact', k)))
    monkeypatch.setattr(connector, 'setup_channel', lambda *a: calls.append(('signin', {})))
    monkeypatch.setattr(connector, 'prepare_imessage_reader', lambda: calls.append(('permissions', {})))
    monkeypatch.setattr(connector, 'request', lambda *a: {'accepted': True})
    connector.run_job('https://study.example', 'test-credential', {
        'id': 'job', 'kind': 'setup', 'agent': 'instinct', 'channel': 'imessage', 'step': step})
    assert calls == [(expected, {'open_channel': False} if expected == 'contact' else {})]


def test_slack_pins_before_browser_login_without_sync_or_send(monkeypatch):
    from bench.channels import slack
    calls = []
    class Channel:
        def __init__(self, agents, **kw):
            assert kw['send_via'] == 'api'
        def __enter__(self):
            calls.append('pin')
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(slack, 'SlackChannel', Channel)
    monkeypatch.setattr(connector, 'load_agents', lambda: {})
    monkeypatch.setattr(slack, 'sign_in', lambda agents: calls.append('login'))
    connector.setup_channel('slack')
    assert calls == ['pin', 'login']
