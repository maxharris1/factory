from __future__ import annotations

from pathlib import Path

import pytest

from lib import account_doctor
from lib.account_doctor import TOKEN_AUTH, SignIn
from lib.account_signin import Outcome, Prompt, judge, login_script, logout_script, parse_output
from lib.harness_env import ACCOUNT_ISOLATION
from lib.machines_yml import load_machines_yml
from lib.remote import SshTransport
from tests.test_account_doctor import World

CLAUDE_URL = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
              "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
              "&scope=user%3Aprofile+user%3Ainference&code_challenge=Zx1-_abc&code_challenge_method=S256"
              "&state=Qm9vX2Zvbw")
CODEX_URL = "https://auth.openai.com/codex/device"
EMAIL = "max@example.com"
TOKEN = "sk-ant-oat01-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-AbCdEf"
FOREIGN = "https://evil.example.net/cai/oauth/authorize?x=1"
PLANTED = (EMAIL, TOKEN, FOREIGN, "evil.example.net", "sk-ant")


def _hostile(*lines: str) -> bytes:
    noise = [f"Signed in before as {EMAIL}", f"token {TOKEN}", f"or open {FOREIGN}"]
    return ("\r\n".join([*noise, *lines, *noise]) + "\r\n").encode()


@pytest.mark.parametrize(("harness", "output", "expected"), [
    ("claude", _hostile("Browser didn't open? Use the url below to sign in:", CLAUDE_URL), Prompt(CLAUDE_URL, None)),
    ("claude", _hostile(f"\x1b[2m{CLAUDE_URL}\x1b[0m"), Prompt(CLAUDE_URL, None)),
    ("claude", _hostile(f"\x1b]8;;{FOREIGN}\x1b\\{CLAUDE_URL}\x1b]8;;\x1b\\"), Prompt(CLAUDE_URL, None)),
    ("claude", _hostile("Paste code here if prompted >", "ABCD-EFGH"), None),
    ("claude", _hostile(CODEX_URL), None),
    ("codex", _hostile("1. Open this link in your browser", f"   \x1b[94m{CODEX_URL}\x1b[0m",
                       "2. Enter this one-time code", "   \x1b[94mABCD-12EFG\x1b[0m"), Prompt(CODEX_URL, "ABCD-12EFG")),
    ("codex", _hostile(CODEX_URL, "WXYZ-1234"), Prompt(CODEX_URL, "WXYZ-1234")),
    ("codex", _hostile(CODEX_URL), None),
    ("codex", _hostile("ABCD-EFGH"), None),
    ("codex", _hostile(CODEX_URL, "abcd-efgh", "ABC-EFGH", "ABCD-EFGHIJ", f"{TOKEN}-ABCD-EFGH"), None),
    ("codex", _hostile(CLAUDE_URL, "ABCD-EFGH"), None),
    ("claude", _hostile(f"{CLAUDE_URL}@evil.example.net", "https://claude.com.evil.example.net/cai/oauth/authorize",
                        "https://claude.com/cai/oauth/authorize/../../x", "http://claude.com/cai/oauth/authorize",
                        "https://user@claude.com/cai/oauth/authorize", "https://claude.com:8443/cai/oauth/authorize",
                        f"{CLAUDE_URL}#{EMAIL}", f"{CLAUDE_URL}&hint={EMAIL}"), None),
    ("codex", _hostile(f"{CODEX_URL}?next={FOREIGN}", f"{CODEX_URL}/x", "ABCD-EFGH"), None),
    ("claude", b"\xff\xfe" + _hostile(CLAUDE_URL), Prompt(CLAUDE_URL, None)),
    ("claude", b"", None),
])
def test_parse_output_keeps_only_the_allowlisted_url_and_codex_code(
    harness: str, output: bytes, expected: Prompt | None,
) -> None:
    prompt = parse_output(harness, output)

    assert prompt == expected
    kept = "" if prompt is None else f"{prompt.url} {prompt.code}"
    for planted in PLANTED:
        assert planted not in kept, f"{planted} survived the parse"


FP_NEW = "111111111111"
FP_B = "bbbbbbbbbbbb"
FP_C = "cccccccccccc"


def _row(account: str, status: str = "ok", fingerprint: str | None = None, *,
         method: str | None = "claude.ai", detail: str = "") -> SignIn:
    return SignIn(account, "mac", status, method, "max", fingerprint, 0.0, detail)


NEW_OK = _row("claude-c", fingerprint=FP_NEW)
BEFORE = {"claude-b": _row("claude-b", fingerprint=FP_B), "claude-d": _row("claude-d", "signed-out")}


@pytest.mark.parametrize(("new", "before", "after", "expected"), [
    (NEW_OK, BEFORE, BEFORE, Outcome("ok", None, "ok")),
    (NEW_OK, {}, {}, Outcome("ok", None, "ok")),
    (NEW_OK, BEFORE, {**BEFORE, "claude-d": _row("claude-d", fingerprint=FP_C)}, Outcome("ok", None, "ok")),
    (_row("claude-c", "signed-out", method="oauth_token", detail=TOKEN_AUTH), BEFORE, BEFORE,
     Outcome("failed", "token-auth", "signed-out")),
    (_row("claude-c", "ok", FP_NEW, method="oauth_token"), BEFORE, BEFORE,
     Outcome("failed", "token-auth", "ok")),
    (_row("claude-c", "signed-out", detail=account_doctor.NOT_LOGGED_IN), BEFORE, BEFORE,
     Outcome("failed", "signed-out", "signed-out")),
    (_row("claude-c", "unreachable", method=None, detail=account_doctor.SSH_DOWN), BEFORE, BEFORE,
     Outcome("failed", "unreachable", "unreachable")),
    (NEW_OK, BEFORE, {**BEFORE, "claude-b": _row("claude-b", "signed-out")},
     Outcome("mismatch", "disturbed-account", "ok")),
    (NEW_OK, BEFORE, {**BEFORE, "claude-b": _row("claude-b", fingerprint=FP_C)},
     Outcome("mismatch", "disturbed-account", "ok")),
    (NEW_OK, BEFORE, {"claude-d": BEFORE["claude-d"]}, Outcome("mismatch", "disturbed-account", "ok")),
    (NEW_OK, {**BEFORE, "claude-b": _row("claude-b", fingerprint=FP_NEW)},
     {**BEFORE, "claude-b": _row("claude-b", fingerprint=FP_NEW)}, Outcome("mismatch", "shared-identity", "ok")),
    (NEW_OK, BEFORE, {**BEFORE, "claude-d": _row("claude-d", fingerprint=FP_NEW)},
     Outcome("mismatch", "shared-identity", "ok")),
    (_row("claude-c", "mismatch", FP_NEW, detail=account_doctor.SHARED_IDENTITY.format("claude-b")), BEFORE, BEFORE,
     Outcome("mismatch", "shared-identity", "mismatch")),
    (_row("claude-c", "signed-out", detail=account_doctor.NOT_LOGGED_IN), BEFORE,
     {**BEFORE, "claude-b": _row("claude-b", "signed-out")}, Outcome("mismatch", "disturbed-account", "signed-out")),
], ids=["ok", "ok-alone", "ok-other-signed-in-meanwhile", "profile-token-login", "token-method-never-ok",
        "still-signed-out", "unreachable", "other-signed-out", "other-fingerprint-changed", "other-missing",
        "other-shares-new-fingerprint", "previously-out-other-shares-new-fingerprint", "doctor-mismatch",
        "failed-login-that-disturbs-another-is-mismatch"])
def test_judge_table(new: SignIn, before: dict[str, SignIn], after: dict[str, SignIn], expected: Outcome) -> None:
    assert judge(new, before, after) == expected


SECURE = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
TOKENS = tuple(name for isolation in ACCOUNT_ISOLATION.values() for name in isolation.tokens)
FAKE_CLI = """\
#!/bin/sh
{{ printf 'argv=%s\\n' "$*"; env; }} > {dump}
echo "{name} ran"
"""
ARGV = {
    ("claude", "login"): "auth login --claudeai",
    ("codex", "login"): "login --device-auth",
    ("claude", "logout"): "auth logout",
    ("codex", "logout"): "logout",
}
SCRIPTS = {"login": login_script, "logout": logout_script}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    world = World(tmp_path, monkeypatch)
    world.profile(f"export {SECURE}=/planted/secure\n")
    return world


@pytest.mark.parametrize(("harness", "action"), list(ARGV))
def test_the_cli_sees_the_account_home_and_none_of_the_profile_tokens(
    world: World, harness: str, action: str,
) -> None:
    profile = (world.device_home / ".bash_profile").read_text(encoding="utf-8")
    assert all(f"export {name}=" in profile for name in TOKENS) and f"{SECURE}=/planted/secure" in profile
    assert len(TOKENS) == 5
    dump = world.tmp / f"{harness}-{action}.env"
    fake = world.bin / harness
    fake.write_text(FAKE_CLI.format(dump=dump, name=harness), encoding="utf-8")
    config, _ = load_machines_yml(world.machines)
    device = config.device("pc")
    home = config.account_home("pc", harness, f"{harness}-b")

    exchanged = SshTransport().exchange(
        account_doctor.ssh_argv(device, harness, SCRIPTS[action](harness, home)), timeout=30)

    assert exchanged.code == 0, exchanged
    seen = dump.read_text(encoding="utf-8")
    variables = dict(line.split("=", 1) for line in seen.splitlines() if "=" in line)
    assert variables["argv"] == ARGV[harness, action]
    assert variables[ACCOUNT_ISOLATION[harness].home] == home
    (call,) = world.ssh.calls()
    assert (call["user"], call["alias"]) == (device.login_for(harness), f"pc-{harness}-bot")
    assert not set(variables) & {*TOKENS, SECURE}, sorted(set(variables) & {*TOKENS, SECURE})
    assert "sk-planted" not in seen and "/planted/secure" not in seen
