"""Token broker (#171): mint each bot's GitHub App installation token on a
box timer, before it expires, and put it where that bot's harness user
reads it.

The box user runs `refresh` (deploy/app-token-service.sh once). It holds the
App keys; a harness user never can. For every App in orch.yml's github_app
and every installation it lists, refresh keeps one token file,
<token root>/<bot>/<owner>.json, and mints a new token only when the file is
missing or within REFRESH_MARGIN of expiry. The file is readable by the
bot's harness user on the box (group of that user, mode 0640) and by no one
else; the bot's directory is 0750 the same way. A remote device that runs
the bot's harness gets each new token over the bot's own SSH alias, on
stdin, into ~/.config/factory/app-tokens/<owner>.json (mode 0600). Where the
device's ssh_user makes several bots share one login, each bot's tokens go
to ~/.config/factory/app-tokens/<bot>/<owner>.json instead.

The harness user reads it through two helpers, run with the system python
(stdlib only, so the top of this module imports nothing from lib):

* `credential --dir D get`: a git credential helper for https://github.com;
* `gh --dir D --gh /path/to/gh -- ARGS`: runs gh with GH_TOKEN set for that
  one child, so `env -u GH_TOKEN gh` keeps working with no token variable in
  the job's environment.

Both read D/<bot> instead of D when the job's ORCH_JOB_BOT names a bot and
that directory exists.

Nothing here prints, logs or puts a token on a command line.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence, TextIO

if TYPE_CHECKING:
    from lib.orch_yml import GitHubApp

REFRESH_MARGIN = timedelta(minutes=15)
JWT_LIFETIME = timedelta(minutes=9)
JWT_SKEW = timedelta(seconds=60)
API = "https://api.github.com"
DEFAULT_TOKEN_ROOT = "~/.local/state/factory/app-tokens"
REMOTE_DIR = ".config/factory/app-tokens"
PUSH_TIMEOUT_SECONDS = 30.0
MINT_TIMEOUT_SECONDS = 30.0
PUSHED_FILE = ".pushed"
TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_BOT = re.compile(r"^[a-z][a-z0-9-]{0,38}$")
_GITHUB_REMOTE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9-]+)/"
)


class BrokerError(RuntimeError):
    """One token could not be minted or written. The message holds no token."""


@dataclass(frozen=True)
class Stored:
    token: str
    issued_at: datetime
    expires_at: datetime

    def __repr__(self) -> str:
        return f"Stored(issued_at={self.issued_at.isoformat()}, expires_at={self.expires_at.isoformat()})"

    def payload(self) -> bytes:
        return json.dumps(
            {
                "token": self.token,
                "issued_at": _iso(self.issued_at),
                "expires_at": _iso(self.expires_at),
            }
        ).encode("utf-8")


@dataclass(frozen=True)
class Remote:
    """A device that runs the bot's harness, reached as user over alias."""

    device: str
    user: str
    alias: str
    bot: str | None = None


Mint = Callable[["GitHubApp", int, datetime], Stored]
Push = Callable[[Remote, str, bytes], bool]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def plain_owner(owner: str) -> str:
    if not _OWNER.fullmatch(owner):
        raise BrokerError(f"account {owner!r} is not a GitHub login")
    return owner.casefold()


def plain_bot(bot: str) -> str:
    if not _BOT.fullmatch(bot):
        raise BrokerError(f"bot {bot!r} is not a plain bot name")
    return bot


def token_path(directory: Path, owner: str) -> Path:
    return directory / f"{plain_owner(owner)}.json"


def token_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.json") if not p.name.startswith("."))


def read_token(path: Path) -> Stored | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    issued = _parse_time(data.get("issued_at"))
    expires = _parse_time(data.get("expires_at"))
    if not isinstance(token, str) or not token or issued is None or expires is None:
        return None
    return Stored(token, issued, expires)


def needs_refresh(stored: Stored | None, now: datetime) -> bool:
    return stored is None or now >= stored.expires_at - REFRESH_MARGIN


def _write_private(path: Path, data: bytes, *, mode: int, gid: int | None) -> None:
    """Write data to path atomically, the file at mode (group gid) from the
    start, so no reader ever sees a wider or half-written file."""
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(handle, 0o600)
        if gid is not None:
            os.fchown(handle, -1, gid)
        os.fchmod(handle, mode)
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


def prepare_dir(directory: Path, gid: int | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if gid is not None:
        os.chown(directory, -1, gid)
        os.chmod(directory, 0o750)
    else:
        os.chmod(directory, 0o700)


def write_token(path: Path, stored: Stored, gid: int | None) -> None:
    _write_private(path, stored.payload(), mode=0o640 if gid is not None else 0o600, gid=gid)


def _has_access(path: Path, gid: int | None) -> bool:
    info = path.stat()
    if gid is None:
        return stat.S_IMODE(info.st_mode) == 0o600
    return stat.S_IMODE(info.st_mode) == 0o640 and info.st_gid == gid


def ensure(
    app: "GitHubApp",
    owner: str,
    directory: Path,
    *,
    now: datetime,
    mint: Mint,
    gid: int | None,
) -> tuple[Stored, bool]:
    """The bot's token for owner: the stored one while it is fresh, else a
    new one, written before it is returned. True when it was minted."""
    path = token_path(directory, owner)
    stored = read_token(path)
    if not needs_refresh(stored, now):
        assert stored is not None
        if not _has_access(path, gid):
            write_token(path, stored, gid)
        return stored, False
    installation = _installation(app, owner)
    fresh = mint(app, installation, now)
    write_token(path, fresh, gid)
    return fresh, True


def _installation(app: "GitHubApp", owner: str) -> int:
    for account, number in app.installations.items():
        if account.casefold() == owner.casefold():
            return number
    raise BrokerError(f"{app.bot} has no installation for {owner}")


# --- minting -----------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def openssl_sign(key_file: Path, data: bytes) -> bytes:
    """RS256 over data with the App key, by openssl, which reads the key."""
    try:
        done = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key_file)],
            input=data,
            capture_output=True,
            timeout=MINT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrokerError(f"openssl could not sign: {type(exc).__name__}") from None
    if done.returncode != 0 or not done.stdout:
        raise BrokerError(f"openssl could not sign with {key_file} (exit {done.returncode})")
    return done.stdout


def app_jwt(
    app_id: int,
    key_file: Path,
    now: datetime,
    sign: Callable[[Path, bytes], bytes] = openssl_sign,
) -> str:
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = {
        "iat": int((now - JWT_SKEW).timestamp()),
        "exp": int((now + JWT_LIFETIME).timestamp()),
        "iss": str(app_id),
    }
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode("ascii")
    return f"{header}.{body}.{_b64(sign(key_file, signing_input))}"


def github_mint(
    app: "GitHubApp",
    installation: int,
    now: datetime,
    *,
    urlopen: Callable[..., object] | None = None,
    sign: Callable[[Path, bytes], bytes] = openssl_sign,
) -> Stored:
    """POST /app/installations/<id>/access_tokens as the App."""
    import urllib.error
    import urllib.request

    opener = urlopen or urllib.request.urlopen
    request = urllib.request.Request(
        f"{API}/app/installations/{installation}/access_tokens",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {app_jwt(app.app_id, app.private_key_file, now, sign)}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "factory-token-broker",
        },
    )
    try:
        with opener(request, timeout=MINT_TIMEOUT_SECONDS) as response:  # type: ignore[attr-defined]
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise BrokerError(f"GitHub refused the token for {app.bot} (HTTP {exc.code})") from None
    except (OSError, ValueError) as exc:
        raise BrokerError(f"no token for {app.bot}: {type(exc).__name__}") from None
    token = data.get("token") if isinstance(data, dict) else None
    expires = _parse_time(data.get("expires_at")) if isinstance(data, dict) else None
    if not isinstance(token, str) or not token or expires is None:
        raise BrokerError(f"GitHub sent no token for {app.bot}")
    return Stored(token, now, expires)


# --- remote devices ----------------------------------------------------------


def push_script(owner: str, bot: str | None = None) -> str:
    """The device side of a push: the token arrives on stdin, never in argv."""
    name = f"{plain_owner(owner)}.json"
    directory = REMOTE_DIR if bot is None else f"{REMOTE_DIR}/{plain_bot(bot)}"
    return (
        "exec sh -c 'umask 077 && d=\"$HOME/" + directory + "\" && mkdir -p \"$d\" && "
        "chmod 700 \"$d\" && t=\"$d/." + name + ".$$\" && cat >\"$t\" && "
        "mv -f \"$t\" \"$d/" + name + "\"'"
    )


def ssh_push(remote: Remote, owner: str, payload: bytes) -> bool:
    from lib.remote import SSH_BINARY, SSH_OPTIONS, SshTransport

    script = push_script(owner, remote.bot)
    argv = [SSH_BINARY, *SSH_OPTIONS, "-l", remote.user, "--", remote.alias, script]
    result = SshTransport().exchange(argv, timeout=PUSH_TIMEOUT_SECONDS, input=payload)
    return result.code == 0


def _read_pushed(directory: Path) -> dict[str, str]:
    try:
        data = json.loads((directory / PUSHED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


# --- refresh -----------------------------------------------------------------


@dataclass(frozen=True)
class Holder:
    """Who reads a bot's tokens on the box: gid None means only the box user
    (the bot's harness runs as the box user, or has no user here)."""

    user: str | None
    gid: int | None


def refresh(
    apps: Iterable["GitHubApp"],
    token_root: Path,
    *,
    now: Callable[[], datetime],
    mint: Mint,
    holders: Mapping[str, Holder],
    remotes: Mapping[str, Sequence[Remote]],
    push: Push = ssh_push,
    out: TextIO | None = None,
) -> int:
    """One timer pass. Exit 0 when every token is fresh and every device has
    the latest one, else 1. Each line names a bot, an account and a time."""
    out = out or sys.stdout
    status = 0
    for app in apps:
        holder = holders.get(app.bot, Holder(None, None))
        directory = token_root / app.bot
        try:
            prepare_dir(directory, holder.gid)
        except OSError as exc:
            print(f"app-token: {app.bot}: cannot prepare {directory}: {exc.strerror}", file=out)
            status = 1
            continue
        pushed = _read_pushed(directory)
        before = dict(pushed)
        for owner in sorted(app.installations, key=str.casefold):
            try:
                stored, minted = ensure(
                    app, owner, directory, now=now(), mint=mint, gid=holder.gid
                )
            except (BrokerError, OSError) as exc:
                message = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
                print(f"app-token: {app.bot} {owner}: {message}", file=out)
                status = 1
                continue
            action = "minted" if minted else "fresh"
            print(
                f"app-token: {app.bot} {owner}: {action}, expires {_iso(stored.expires_at)}",
                file=out,
            )
            for remote in remotes.get(app.bot, ()):
                key = f"{remote.device}/{remote.user}/{plain_owner(owner)}"
                stamp = _iso(stored.issued_at)
                if pushed.get(key) == stamp:
                    continue
                if push(remote, owner, stored.payload()):
                    pushed[key] = stamp
                    print(f"app-token: {app.bot} {owner}: sent to {remote.device}", file=out)
                else:
                    print(
                        f"app-token: {app.bot} {owner}: {remote.device} unreachable; retry next pass",
                        file=out,
                    )
                    status = 1
        if pushed != before:
            _write_private(
                directory / PUSHED_FILE, json.dumps(pushed).encode(), mode=0o600, gid=None
            )
    return status


def readable_by(path: Path, uid: int, gids: Iterable[int]) -> bool:
    """Whether uid (in gids) may read path by its mode bits: search on each
    directory above it, read on the file. Root reads everything."""
    if uid == 0:
        return True
    groups = set(gids)
    path = Path(os.path.abspath(path))
    chain = [*reversed(path.parents), path]
    for index, part in enumerate(chain):
        try:
            info = part.stat()
        except OSError:
            return False
        need = 0o4 if index == len(chain) - 1 else 0o1
        if info.st_uid == uid:
            bits = (info.st_mode >> 6) & 0o7
        elif info.st_gid in groups:
            bits = (info.st_mode >> 3) & 0o7
        else:
            bits = info.st_mode & 0o7
        if not bits & need:
            return False
    return True


def box_holders(bots: Iterable[str], users: Mapping[str, str]) -> dict[str, Holder]:
    """Each bot's box reader: the group of its harness user when that user
    exists and is not the box user; else the box user alone."""
    import pwd

    result: dict[str, Holder] = {}
    for bot in bots:
        user = users.get(bot)
        if user is None:
            result[bot] = Holder(None, None)
            continue
        try:
            entry = pwd.getpwnam(user)
        except KeyError:
            result[bot] = Holder(user, None)
            continue
        gid = None if entry.pw_uid == os.getuid() else entry.pw_gid
        result[bot] = Holder(user, gid)
    return result


def _warn_unreadable(token_root: Path, holders: Mapping[str, Holder], out: TextIO) -> None:
    import pwd

    for bot, holder in holders.items():
        if holder.gid is None or holder.user is None:
            continue
        entry = pwd.getpwnam(holder.user)
        gids = os.getgrouplist(holder.user, entry.pw_gid)
        directory = token_root / bot
        for path in token_files(directory):
            if not readable_by(path, entry.pw_uid, gids):
                print(f"app-token: warning: {holder.user} cannot read {path}", file=out)


def _command_refresh(args: argparse.Namespace) -> int:
    from datetime import datetime as _dt

    from lib.machines_yml import load_machines_yml
    from lib.orch_yml import load_orch_yml
    from lib.wait import OWNING_BOTS

    config, findings = load_orch_yml(Path(args.orch))
    if config is None:
        for finding in findings:
            print(f"app-token: {finding.format(args.orch)}", file=sys.stderr)
        return 1
    if config.github_app is None:
        return 0
    machines, machine_findings = load_machines_yml(Path(args.machines))
    devices = machines.devices if machines is not None else ()
    if machines is None:
        for finding in machine_findings:
            print(f"app-token: {finding.format(args.machines)}; no remote device gets a token")
    users: dict[str, str] = {}
    remotes: dict[str, list[Remote]] = {}
    for bot, harness in OWNING_BOTS.items():
        for device in devices:
            if device.local:
                if harness in device.harnesses:
                    users[bot] = device.harnesses[harness]
                continue
            alias = device.ssh_alias_for(harness)
            if device.eligible(harness) and alias is not None:
                login = device.login_for(harness)
                shared = bot if login != device.harnesses[harness] else None
                remotes.setdefault(bot, []).append(Remote(device.name, login, alias, shared))
    apps = list(config.github_app.apps.values())
    holders = box_holders((app.bot for app in apps), users)
    token_root = Path(args.token_root).expanduser()
    token_root.mkdir(parents=True, exist_ok=True)
    os.chmod(token_root, 0o711)
    status = refresh(
        apps,
        token_root,
        now=lambda: _dt.now(timezone.utc),
        mint=github_mint,
        holders=holders,
        remotes=remotes,
        push=ssh_push,
    )
    _warn_unreadable(token_root, holders, sys.stdout)
    return 1 if machines is None else status


# --- harness-side helpers (stdlib only) --------------------------------------


def bot_directory(directory: Path, environ: Mapping[str, str]) -> Path:
    """directory/<ORCH_JOB_BOT> when that is a bot's own token directory."""
    bot = environ.get("ORCH_JOB_BOT", "")
    if _BOT.fullmatch(bot) and (directory / bot).is_dir():
        return directory / bot
    return directory


def select_token(directory: Path, owner: str | None, now: datetime) -> str | None:
    """The unexpired token for owner, or the only one when owner is unknown."""
    if owner is not None:
        try:
            paths = [token_path(directory, owner)]
        except BrokerError:
            return None
    else:
        paths = token_files(directory)
        if len(paths) != 1:
            return None
    stored = read_token(paths[0])
    if stored is None or now >= stored.expires_at:
        return None
    return stored.token


def credential(directory: Path, operation: str, stdin: TextIO, stdout: TextIO, now: datetime) -> int:
    """git-credential `get` for https://github.com; store and erase are no-ops."""
    fields: dict[str, str] = {}
    for line in stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition("=")
        fields[key] = value
    if operation != "get" or fields.get("protocol") != "https" or fields.get("host") != "github.com":
        return 0
    path = fields.get("path", "")
    owner = path.split("/", 1)[0] if "/" in path else None
    token = select_token(directory, owner, now)
    if token is not None:
        stdout.write(f"username=x-access-token\npassword={token}\n")
    return 0


_REPO_FLAGS = ("-R", "--repo")
_FIELD_FLAGS = ("-f", "-F", "--field", "--raw-field")
_VALUE_FLAGS = frozenset(
    {
        "-b", "--body", "-t", "--title", "-F", "--body-file", "-n", "--notes", "--notes-file",
        "--subject", "--message", "-q", "--jq", "--template", "-S", "--search", "--json",
        "-B", "--base", "-H", "--head", "--header", "-X", "--method", "--input", "--preview",
        "--cache", "--hostname", "-l", "--label", "--add-label", "--remove-label", "--assignee",
        "--reviewer", "--milestone", "--project", "--author", "--commit", "--match-head-commit",
    }
)
_GITHUB_TARGET = re.compile(
    r"^(?:https?://)?(?:www\.|api\.)?github\.com/(?:repos/|orgs/|users/)?([^/\s]+)/"
)
_API_PATH = re.compile(r"^/?(?:repos|orgs|users)/([^/?\s]+)")
_REPO_SLUG = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)/[A-Za-z0-9._-]+$")


def _owner_of_target(value: str, slug: bool, api_path: bool = True) -> str | None:
    match = _GITHUB_TARGET.match(value) or (_API_PATH.match(value) if api_path else None)
    if match is None and slug:
        match = _REPO_SLUG.match(value)
    return match.group(1) if match else None


def _field_value(args: Sequence[str], index: int) -> str | None:
    arg = args[index]
    if arg in _FIELD_FLAGS:
        return args[index + 1] if index + 1 < len(args) else None
    for flag in _FIELD_FLAGS:
        if flag.startswith("--") and arg.startswith(flag + "="):
            return arg[len(flag) + 1 :]
        if not flag.startswith("--") and arg.startswith(flag) and len(arg) > len(flag):
            return arg[len(flag) :]
    return None


def owner_from_args(args: Sequence[str]) -> str | None:
    """The owner of the repo or org a gh command targets, in this order:
    -R/--repo; for `gh api`, an `owner=` field, then the endpoint; else a
    github.com URL positional or the OWNER/REPO positional of `gh repo
    <command>`. No other option's value ever names the target."""
    api = args[:1] == ["api"]
    explicit: str | None = None
    field_owner: str | None = None
    positionals: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _REPO_FLAGS:
            if index + 1 < len(args) and explicit is None:
                explicit = _owner_of_target(args[index + 1], slug=True) or ""
            index += 2
            continue
        if arg.startswith("--repo=") or (arg.startswith("-R") and len(arg) > 2):
            if explicit is None:
                value = arg.split("=", 1)[1] if arg.startswith("--") else arg[2:].removeprefix("=")
                explicit = _owner_of_target(value, slug=True) or ""
            index += 1
            continue
        if api and (field := _field_value(args, index)) is not None:
            if field.startswith("owner=") and field_owner is None:
                field_owner = field.split("=", 1)[1] or None
            index += 2 if arg in _FIELD_FLAGS else 1
            continue
        if arg in _VALUE_FLAGS:
            index += 2
            continue
        if not arg.startswith("-"):
            positionals.append(arg)
        index += 1
    if explicit is not None:
        return explicit or None
    if field_owner:
        return field_owner
    if api:
        return _owner_of_target(positionals[1], slug=False) if len(positionals) > 1 else None
    slug_from = 2 if args[:1] == ["repo"] else len(positionals)
    for position, value in enumerate(positionals):
        owner = _owner_of_target(value, slug=position >= slug_from, api_path=False)
        if owner:
            return owner
    return None


def owner_from_origin(cwd: str | None = None) -> str | None:
    try:
        done = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _GITHUB_REMOTE.match(done.stdout.strip()) if done.returncode == 0 else None
    return match.group(1) if match else None


def gh_env(
    directory: Path, args: Sequence[str], environ: Mapping[str, str], now: datetime
) -> dict[str, str]:
    """gh's environment: no inherited token variable; GH_TOKEN set to the
    bot's App token for the target's owner when there is one. The target
    comes from the args, then GH_REPO, then the checkout's origin."""
    env = {key: value for key, value in environ.items() if key not in TOKEN_VARIABLES}
    gh_repo = environ.get("GH_REPO", "")
    owner = (
        owner_from_args(args)
        or _owner_of_target(gh_repo, slug=True, api_path=False)
        or owner_from_origin()
    )
    token = select_token(directory, owner, now)
    if token is not None:
        env["GH_TOKEN"] = token
    return env


def reads_prefix(env_prefix: Sequence[str], bot: str, environ: Mapping[str, str]) -> tuple[str, ...]:
    """The argv head that runs gh through the `gh` helper with bot's box token
    directory, in place of a plain `env_prefix gh`."""
    gh = shutil.which("gh", path=environ.get("PATH"))
    if gh is None:
        raise BrokerError("gh is not on PATH")
    root = Path(environ.get("ORCH_APP_TOKEN_ROOT") or DEFAULT_TOKEN_ROOT).expanduser()
    return (
        *env_prefix,
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "gh",
        "--dir",
        str(root / plain_bot(bot)),
        "--gh",
        str(Path(gh).absolute()),
        "--",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="github_app_token", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    refresh_cmd = sub.add_parser("refresh", help="mint tokens due for renewal (box user)")
    refresh_cmd.add_argument("--orch", required=True)
    refresh_cmd.add_argument("--machines", required=True)
    refresh_cmd.add_argument("--token-root", default=DEFAULT_TOKEN_ROOT)
    cred = sub.add_parser("credential", help="git credential helper (harness user)")
    cred.add_argument("--dir", required=True)
    cred.add_argument("operation")
    gh = sub.add_parser("gh", help="run gh with the bot's token (harness user)")
    gh.add_argument("--dir", required=True)
    gh.add_argument("--gh", required=True)
    gh.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.command == "refresh":
        return _command_refresh(args)
    directory = bot_directory(Path(args.dir).expanduser(), os.environ)
    if args.command == "credential":
        return credential(directory, args.operation, sys.stdin, sys.stdout, now)
    rest = list(args.args)
    if rest[:1] == ["--"]:
        rest = rest[1:]
    os.execve(args.gh, [args.gh, *rest], gh_env(directory, rest, os.environ, now))
    return 127


if __name__ == "__main__":
    sys.exit(main())
