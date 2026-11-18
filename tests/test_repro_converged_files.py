from lib import live_settings
from tests.test_repro_separate_checkout import separate_checkouts
from tests.test_repro_settings_adoption import page
from tests.test_settings_apply import loaded


def converged_but_closed(tmp_path):
    queue, audit, waiter, waiter_file, poller_file, monitor = separate_checkouts(tmp_path)
    assert waiter.claim("claude") is None
    waiter_file.write_bytes(poller_file.read_bytes())
    assert loaded(waiter_file).device("pc").enabled
    assert monitor.config.device("pc").enabled
    claims = []
    for _ in range(3):
        monitor.reload()
        claims.append(waiter.claim("claude"))
    with queue.connect() as connection:
        live = live_settings.peek(connection)
    result = page(queue, audit)
    return live, result, claims


def test_live_enable_takes_effect_after_all_files_converge(tmp_path):
    _, _, claims = converged_but_closed(tmp_path)
    assert any(claim is not None for claim in claims)


def test_pending_until_claims_use_enable(tmp_path):
    live, result, _ = converged_but_closed(tmp_path)
    assert live.switches["pc"].enabled or result == "pending"
