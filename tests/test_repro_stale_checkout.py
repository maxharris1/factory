from lib import live_settings, settings_apply
from lib.device_monitor import DeviceMonitor
from lib.device_pool import DevicePool
from lib.queue import SQLiteJobQueue
from tests import test_remote_start as remote
from tests.test_repro_settings_adoption import page
from tests.test_settings_apply import SignalTransport, loaded
import pytest


@pytest.mark.parametrize('field,old,new', [('slot_max', 6, 1), ('enabled', False, True)])
def test_stale_checkout_restart(tmp_path, field, old, new):
    body = ('devices:\n  - name: box\n    local: true\n'
            f'    enabled: {str(old if field == "enabled" else True).lower()}\n'
            '    slot_max: 6\n    harnesses: {claude: claude-bot}\n')
    settings_file, service_file = [tmp_path / name / 'machines.yml'
                                   for name in ('settings', 'service')]
    for path in (settings_file, service_file):
        path.parent.mkdir()
        path.write_text(body)
    queue = SQLiteJobQueue(tmp_path / 'orch.db', clock=lambda: 100.0)
    config = loaded(service_file)

    def monitor():
        return DeviceMonitor(DevicePool(queue, config, machines_path=service_file),
                             config, SignalTransport(), machines_path=service_file)

    monitor().reload()
    waiter = DevicePool(queue, config, machines_path=service_file)
    entry = settings_apply.Applier(
        settings_file, tmp_path, tmp_path / 'deploy', 'maxharris1', {}, (),
        ('poller', 'waiter-claude-bot'), settings_apply.claim_lock(queue.path),
        run=lambda *_: 0,
    ).apply(settings_apply.SettingsRequest(
        'settings-request-123456', 'maxharris1', '2026-11-17T10:00:00Z',
        'box', field, old, new))
    assert entry.result == 'pending'
    audit = tmp_path / 'settings-audit.jsonl'
    audit.write_text(entry.line() + '\n')
    assert page(queue, audit) == 'pending'
    restarted = monitor()
    restarted.reload()
    for n in range(6):
        remote.enqueue(queue, 'claude', str(n), timeout=5400)
    claimed = sum(waiter.claim('claude') is not None for _ in range(6))
    with queue.connect() as connection:
        live = live_settings.peek(connection)
    result = page(queue, audit)
    disk = getattr(loaded(settings_file).device('box'), field)
    held = getattr(restarted.config.device('box'), field)
    print(dict(field=field, settings_disk=disk, monitor=held, claimed=claimed,
               version=entry.version, adopted=live.adopted, seen=dict(live.seen), page=result))
    if field == 'slot_max':
        assert claimed <= disk, 'placement exceeded the Settings disk limit'
    else:
        assert result == 'pending', 'services have never used the requested enable'


@pytest.mark.parametrize('field,old,new', [('drain', False, True), ('slot_max', 6, 1)])
def test_a_rolled_back_change_restores_placement(tmp_path, field, old, new):
    body = ('devices:\n  - name: box\n    local: true\n'
            '    drain: false\n    slot_max: 6\n    harnesses: {claude: claude-bot}\n')
    settings_file, service_file = [tmp_path / name / 'machines.yml'
                                   for name in ('settings', 'service')]
    for path in (settings_file, service_file):
        path.parent.mkdir()
        path.write_text(body)
    queue = SQLiteJobQueue(tmp_path / 'orch.db', clock=lambda: 100.0)
    config = loaded(service_file)

    def reload_poller():
        DeviceMonitor(DevicePool(queue, config, machines_path=service_file),
                      config, SignalTransport(), machines_path=service_file).reload()

    reload_poller()
    codes = iter([0, 1, 0, 0])

    def restart(argv, env, log):
        if 'poller-service.sh' in argv[0]:
            reload_poller()
        return next(codes)

    entry = settings_apply.Applier(
        settings_file, tmp_path, tmp_path / 'deploy', 'maxharris1', {},
        ('poller', 'waiter:claude-bot'), (), settings_apply.claim_lock(queue.path),
        run=restart,
    ).apply(settings_apply.SettingsRequest(
        'rollback-request-123456', 'maxharris1', '2026-11-17T10:00:00Z',
        'box', field, old, new))
    assert entry.result == 'failed'
    assert settings_file.read_text() == body
    waiter = DevicePool(queue, config, machines_path=service_file)
    for n in range(6):
        remote.enqueue(queue, 'claude', str(n), timeout=5400)
    claimed = sum(waiter.claim('claude') is not None for _ in range(6))
    with queue.connect() as connection:
        shared = getattr(live_settings.peek(connection).switches['box'], field)
    assert (shared, claimed) == (old, 6), 'the rollback left the failed value in placement'
