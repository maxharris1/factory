"""Suite-wide guard: no test may reach a real host over SSH (F2-02), or
GitHub through gh (#191).

A refusing `ssh` is put first on PATH for the whole run. It exits 255 (an
unreachable device) and touches nothing. Tests that exercise remote start
put tests/fake_ssh.py in front of it instead.

The waiter's skill preflight (lib/skill_preflight.py) is a no-op in this
process unless a test overrides `_skill_preflight`, as
tests/test_skill_preflight.py does. A waiter a test starts as its own
process runs the real preflight, so HOME is a session home holding the
pinned skills, installed as install-skills does.
"""

from __future__ import annotations

import os
import shutil
import signal
import site
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest

LIVE_STATE_ENV_PREFIXES = ("ORCH_", "FACTORY_", "LINEAR_WEBHOOK_", "XDG_")
_live_state_env: dict[str, str] = {}


def pytest_configure(config: pytest.Config) -> None:
    _live_state_env.update({name: os.environ.pop(name) for name in list(os.environ)
                            if name.startswith(LIVE_STATE_ENV_PREFIXES)})


def pytest_unconfigure(config: pytest.Config) -> None:
    os.environ.update(_live_state_env)


def sandbox_env(home: Path, **extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_RUNTIME_DIR": str(home / ".run"),
        "ORCH_STATE_DIR": str(home / ".local/state/factory"),
        **extra,
    }


def reap_home(home: Path) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + 2
        while (pids := _homed(home)) and time.monotonic() < deadline:
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            time.sleep(0.1)


def _homed(home: Path) -> list[int]:
    needle = f"HOME={home}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                environ = (entry / "environ").read_bytes()
            except OSError:
                continue
            if needle in environ.split(b"\0"):
                found.append(int(entry.name))
    return found


@pytest.fixture(autouse=True, scope="session")
def _no_real_ssh(tmp_path_factory: pytest.TempPathFactory):
    guard = tmp_path_factory.mktemp("no-real-ssh")
    script = guard / "ssh"
    script.write_text(
        "#!/bin/sh\necho 'tests: real ssh is disabled' >&2\nexit 255\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    previous = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{guard}{os.pathsep}{previous}"
    yield Path(guard)
    os.environ["PATH"] = previous


@pytest.fixture(autouse=True, scope="session")
def _no_real_gh(_no_real_ssh: Path, tmp_path_factory: pytest.TempPathFactory):
    """No test reaches GitHub through `gh` (#191: `orch job done` reads its
    handoff comment). tests/fake_gh.py answers from $FAKE_GH_STORE and
    refuses without one; tests/handoff_store.py writes a store. The basetemp
    may be on a noexec /dev/shm, where PATH would skip the script; then it
    goes in its own directory under /tmp."""
    guard = tmp_path_factory.mktemp("no-real-gh")
    script = _write_gh(guard)
    fallback = None
    if not os.access(script, os.X_OK):
        fallback = Path(tempfile.mkdtemp(prefix="pytest-no-real-gh-", dir="/tmp"))
        guard, script = fallback, _write_gh(fallback)
    previous = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{guard}{os.pathsep}{previous}"
    yield script
    os.environ["PATH"] = previous
    if fallback is not None:
        shutil.rmtree(fallback, ignore_errors=True)


def _write_gh(directory: Path) -> Path:
    script = directory / "gh"
    script.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).with_name("fake_gh.py")}" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture(autouse=True)
def _registration_gate(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if request.node.get_closest_marker("job_bound") is None:
        from lib.dispatch.core import DispatcherBase
        from lib.dispatch.types import Registration

        monkeypatch.setattr(
            DispatcherBase,
            "_registration",
            lambda self, ticket, comment, kind: Registration.REGISTERED,
        )


@pytest.fixture(autouse=True)
def _claim_proof(monkeypatch: pytest.MonkeyPatch):
    """A bot holds its claim's proof (#127): pool_claim's claim."""
    from lib.job_token import PROOF_ENV
    from tests.pool_helpers import CLAIM_PROOF

    monkeypatch.setenv(PROOF_ENV, CLAIM_PROOF)


@pytest.fixture(autouse=True)
def _end_fake_runners():
    yield
    from tests import fake_runner

    fake_runner.end_all()


@pytest.fixture(autouse=True)
def _fake_gh_store(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Each test's fake gh answers from its own store, empty until a test
    serves a comment (tests/handoff_store.py); a fake harness posts one with
    $FAKE_POST_HANDOFF."""
    store = tmp_path_factory.mktemp("fake-gh") / "fake-gh.json"
    store.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("FAKE_GH_STORE", str(store))
    monkeypatch.setenv("FAKE_POST_HANDOFF", str(Path(__file__).with_name("post_handoff.py")))
    monkeypatch.setenv("FAKE_GH_HANDOFFS", str(Path(__file__).with_name("fake_gh.py")))
    yield store


@pytest.fixture(autouse=True, scope="session")
def _no_local_overlay():
    """No host's orch.local.yml or machines.local.yml leaks into a test (#107).
    Overlay tests clear ORCH_NO_LOCAL_OVERLAY with monkeypatch.delenv."""
    previous = os.environ.get("ORCH_NO_LOCAL_OVERLAY")
    os.environ["ORCH_NO_LOCAL_OVERLAY"] = "1"
    yield
    if previous is None:
        os.environ.pop("ORCH_NO_LOCAL_OVERLAY", None)
    else:
        os.environ["ORCH_NO_LOCAL_OVERLAY"] = previous


@pytest.fixture(autouse=True, scope="session")
def _no_real_db_backup(tmp_path_factory: pytest.TempPathFactory):
    """#66: `orch poll` backs up after every cycle; never to the real
    ~/.local/share/factory/backup. Tests that need a path set their own."""
    backup = tmp_path_factory.mktemp("db-backup") / "orch.db"
    previous = os.environ.get("ORCH_DB_BACKUP")
    os.environ["ORCH_DB_BACKUP"] = str(backup)
    yield backup
    if previous is None:
        os.environ.pop("ORCH_DB_BACKUP", None)
    else:
        os.environ["ORCH_DB_BACKUP"] = previous


@pytest.fixture(autouse=True)
def _0_no_leaked_processes(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    """#358. pytest orders a module's autouse fixtures by name, so `_0_` is set
    up first and torn down last, after the test's own cleanup."""
    from tests import child_reaper

    before = set(child_reaper._table())
    yield
    root = tmp_path_factory.getbasetemp()
    tag = request.node.stash.get(child_reaper._tag, "")
    _fail_on_survivors("test", before, lambda: child_reaper.leaked(before, tag, root))


def _fail_on_survivors(owner: str, before: set[int], survivors) -> None:
    if described := _stop_survivors(before, survivors):
        pytest.fail(f"{owner} left processes running:\n" + "\n".join(described), pytrace=False)


def _stop_survivors(before: set[int], survivors) -> list[str]:
    from tests import child_reaper, subreaper

    deadline = time.monotonic() + 3
    while (leftovers := survivors()) and time.monotonic() < deadline:
        time.sleep(0.1)
    if not leftovers:
        return []
    described = [subreaper._describe(pid) for pid in sorted(leftovers)]
    child_reaper.kill(leftovers, before | {os.getpid()}, child_reaper._table())
    return described


_collection_leaks = pytest.StashKey[list[str]]()
_registered_by = pytest.StashKey[dict[object, Path]]()


def _collection_leak(config: pytest.Config, before: set[int], owner: str) -> str:
    from tests import child_reaper

    root = config._tmp_path_factory.getbasetemp()
    tag = os.environ.get(child_reaper.TAG, "")
    if not (described := _stop_survivors(before, lambda: child_reaper.leaked(before, tag, root))):
        return ""
    leak = f"{owner} left processes running:\n" + "\n".join(described)
    config.stash.setdefault(_collection_leaks, []).append(leak)
    return leak


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    from tests import child_reaper

    before = set(child_reaper._table())
    plugins = set(collector.config.pluginmanager.get_plugins())
    try:
        report = yield
    except BaseException as interrupted:
        if leak := _collection_leak(collector.config, before, collector.nodeid):
            interrupted.add_note(leak)
        raise
    finally:
        if module := collector.getparent(pytest.Module):
            registered = collector.config.stash.setdefault(_registered_by, {})
            registered.update(dict.fromkeys(collector.config.pluginmanager.get_plugins() - plugins, module.path))
    if leak := _collection_leak(collector.config, before, collector.nodeid):
        report.longrepr = f"{report.longrepr}\n\n{leak}" if report.failed else leak
        report.outcome = "failed"
    return report


@pytest.hookimpl(wrapper=True)
def pytest_collection(session: pytest.Session):
    from tests import child_reaper

    before = set(child_reaper._table())
    plugins = set(session.config.pluginmanager.get_plugins())
    try:
        return (yield)
    finally:
        registered = session.config.stash.get(_registered_by, {})
        added = session.config.pluginmanager.get_plugins() - plugins
        sources = {getattr(plugin, "__file__", None) or registered.get(plugin)
                   for plugin in added | registered.keys()}
        loaded = sorted({os.path.relpath(source, session.config.rootpath) for source in sources if source})
        owner = f"collection hooks in {', '.join(loaded)}" if loaded else "collection"
        if leak := _collection_leak(session.config, before, owner):
            session.ihook.pytest_collectreport(report=pytest.CollectReport("", "failed", leak, []))


_summarized = pytest.StashKey[bool]()


def pytest_terminal_summary(config: pytest.Config) -> None:
    config.stash[_summarized] = True


@pytest.hookimpl(wrapper=True)
def pytest_cmdline_main(config: pytest.Config):
    try:
        status = yield
    except BaseException as exc:
        if not _report_collection_leaks(config, exc.code if isinstance(exc, SystemExit) else exc):
            raise
        return pytest.ExitCode.TESTS_FAILED
    return pytest.ExitCode.TESTS_FAILED if _report_collection_leaks(config, status) else status


def _report_collection_leaks(config: pytest.Config, code: object) -> bool:
    if not (leaks := config.stash.get(_collection_leaks, [])):
        return False
    if not config.stash.get(_summarized, False):
        print("\n\n".join(leaks))
    return code in (pytest.ExitCode.OK, None)


_broad_fixtures: dict[pytest.FixtureDef, tuple[set[int], dict[int, str | None]]] = {}


def _started_since(before: set[int], request: pytest.FixtureRequest) -> set[int]:
    from lib.worktrees import process_start
    from tests import child_reaper
    from tests.procs import alive

    root = request.config._tmp_path_factory.getbasetemp()
    claimed = {pid for _, started in _broad_fixtures.values() for pid, start in started.items()
               if alive(pid) and process_start(pid) == start}
    return child_reaper.leaked(before, os.environ.get(child_reaper.TAG, ""), root) - claimed


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef, request: pytest.FixtureRequest):
    if fixturedef.scope == "function":
        return (yield)
    from lib.worktrees import process_start
    from tests import child_reaper

    before = set(child_reaper._table())
    try:
        return (yield)
    finally:
        started = _started_since(before, request)
        _broad_fixtures[fixturedef] = (before, {pid: process_start(pid) for pid in started})


def pytest_fixture_post_finalizer(fixturedef: pytest.FixtureDef, request: pytest.FixtureRequest) -> None:
    if fixturedef not in _broad_fixtures:
        return
    before, _ = _broad_fixtures.pop(fixturedef)
    _fail_on_survivors(f"{fixturedef.scope}-scoped fixture {fixturedef.argname!r}", before,
                       lambda: _started_since(before, request))


@pytest.fixture(autouse=True)
def _a_ssh_control_dir():
    """#222: each test gets its own short ControlMaster socket directory, so
    no fake master outlives its test and a socket path stays bindable. It
    takes no monkeypatch, so a test's own patches are undone before this
    teardown runs. pytest orders same-scope autouse fixtures by name: the
    `_a_` prefix keeps this one ahead of any autouse fixture that requests
    monkeypatch (such as `_skill_preflight`), so monkeypatch is always set up
    after it and undone before its teardown."""
    directory = tempfile.mkdtemp(prefix="orch-ssh-", dir="/tmp")
    previous = os.environ.get("ORCH_SSH_CONTROL_DIR")
    os.environ["ORCH_SSH_CONTROL_DIR"] = directory
    yield directory
    if previous is None:
        os.environ.pop("ORCH_SSH_CONTROL_DIR", None)
    else:
        os.environ["ORCH_SSH_CONTROL_DIR"] = previous
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def _skill_preflight(monkeypatch: pytest.MonkeyPatch):
    from lib import skill_preflight

    monkeypatch.setattr(skill_preflight, "preflight", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True, scope="session")
def _skills_home(tmp_path_factory: pytest.TempPathFactory):
    from lib.pstack_pin import install_skills

    home = tmp_path_factory.mktemp("skills-home")
    errors = install_skills(Path(__file__).resolve().parents[1], home, "local")
    assert errors == [], errors
    saved = {name: os.environ.get(name) for name in ("HOME", "PYTHONUSERBASE")}
    os.environ["PYTHONUSERBASE"] = site.getuserbase()  # keep user-site packages found
    os.environ["HOME"] = str(home)
    yield home
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
