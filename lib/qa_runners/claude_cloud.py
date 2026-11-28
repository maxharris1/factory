"""The Claude Code cloud QA runner (#785).

Implements lib.qa_runners.Runner against the scripted CLI behavior measured in
issue #780 (recorded in PR #1664):
- Launch runs `claude [--environment <ccpool_id>] [--model <model>] --cloud <brief>`
  in a PTY from a deterministic, clean base-branch checkout whose `origin`
  points to the project's remote URL. Claude Code 2.1.323 only accepts self-hosted
  `ccpool_...` pool IDs via `--environment`; logical environment names from
  `factory.md` (`qa.claude_cloud.environment`) are resolved via
  ORCH_QA_CLAUDE_ENVIRONMENTS to either a `ccpool_...` ID or a dedicated
  per-environment CLAUDE_CONFIG_DIR pre-configured with `/remote-env`. Unmapped
  non-`ccpool_` names fail closed with LaunchFailed.
- Pre-accepted folder trust for the checkout path is a one-time host step; an
  interactive trust prompt fails fast with LaunchFailed and is never accepted
  automatically.
- Stop queues a non-interactive stop message with
  `claude -p "stop" --cloud <session-id> --output-format json`.
- The runner passes CLAUDE_CONFIG_DIR under an allowlisted child environment
  (dropping ambient API/OAuth tokens and worker secrets) and never opens, reads,
  or lists files inside that directory. No brief text, upload link, or
  credential is logged or written to disk markers.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import unquote

from lib.harness_env import account_environ
from lib.qa_runners import (
    AgentInfo,
    AgentStatus,
    Lane,
    Launched,
    LaunchFailed,
    RunnerOutage,
    UsageLimit,
    register,
)
from lib.worktrees import WorktreeError, check_remote_url

RUNNER_NAME = "claude-cloud"
CONFIG_DIR_ENV = "ORCH_QA_CLAUDE_CONFIG_DIR"
ENVIRONMENTS_ENV = "ORCH_QA_CLAUDE_ENVIRONMENTS"
WORK_ROOT_ENV = "ORCH_QA_CLAUDE_WORK_ROOT"
REPO_CACHE_ENV = "ORCH_QA_CLAUDE_REPO_CACHE"
BINARY_ENV = "ORCH_QA_CLAUDE_BINARY"
LAUNCH_TIMEOUT_ENV = "ORCH_QA_CLAUDE_TIMEOUT_SECONDS"
STOP_TIMEOUT_ENV = "ORCH_QA_CLAUDE_STOP_TIMEOUT_SECONDS"
REPO_URL_TEMPLATE_ENV = "ORCH_QA_REPO_URL_TEMPLATE"

DEFAULT_WORK_ROOT = Path("/tmp") / "factory-qa-claude"
DEFAULT_REPO_CACHE = Path("/tmp") / "factory-qa-repos"
DEFAULT_BINARY = "claude"
DEFAULT_LAUNCH_TIMEOUT_SECONDS = 180
DEFAULT_STOP_TIMEOUT_SECONDS = 30
DEFAULT_GIT_TIMEOUT_SECONDS = 180
DEFAULT_VERSION_TIMEOUT_SECONDS = 15
DEFAULT_STOP_MESSAGE = "stop"
DEFAULT_REPO_URL_TEMPLATE = "https://github.com/{repo}.git"

_PTY_ROWS = 40
_PTY_COLS = 500
_MAX_OUTPUT_BYTES = 256 * 1024
_KILL_GRACE_SECONDS = 1.0

_FORBIDDEN_HOST_SUBPATHS = (
    Path(".local") / "state" / "factory",
    Path(".config") / "factory",
    Path(".ssh"),
)
_FORBIDDEN_ABSOLUTE_PREFIXES = (
    Path("/etc/factory"),
    Path("/etc/factory-dashboard"),
)

_SAFE_REPO = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
_SIMPLE_SLUG = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SAFE_OPTION_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_CCPOOL_ID = re.compile(r"^ccpool_[A-Za-z0-9._-]{1,120}$")
_SAFE_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_URL_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", re.DOTALL)
_REMOTE_SCHEMES = frozenset({"https", "http", "ssh", "git", "git+ssh", "ssh+git"})

_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|.)", re.DOTALL)
_VIEW_URL = re.compile(r"^[ \t]*View:[ \t]*(https://\S+)", re.MULTILINE)
_RESUME_ID = re.compile(
    r"^[ \t]*Resume with:[ \t]*claude[ \t]+--teleport[ \t]+([A-Za-z0-9][A-Za-z0-9._-]{0,127})", re.MULTILINE
)
_SESSION_FROM_URL = re.compile(r"/code/(session_[A-Za-z0-9._-]+)")
_MACHINE_LINE = re.compile(r"(?:^|\n)Machine:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,63})\b")
_VERSION_TOKEN = re.compile(r"\b(\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9._-]+)?)\b")

_URL_IN_TEXT = re.compile(r"(?:https?|s3)://\S+", re.IGNORECASE)
_LOCATION_IN_TEXT = re.compile(r"'[^'\n]*/[^'\n]*'|[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]+")
_SECRET_IN_TEXT = re.compile(
    r"(?:sk-ant-[A-Za-z0-9_-]+|Bearer\s+\S+|(?:X-Amz-[A-Za-z0-9-]+|sig|token|up|down)=[^&\s]+)",
    re.IGNORECASE,
)
_CLI_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LOGNAME",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMPDIR",
        "TZ",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

_USAGE_LIMIT_PATTERNS = (
    re.compile(r"\b(?:usage limit|limit reached|hit your limit|rate limit|rate_limit_error)\b", re.IGNORECASE),
    re.compile(r"\b(?:too many requests|quota exceeded|spending limit|out of extra usage)\b", re.IGNORECASE),
    re.compile(r"\b(?:credit balance is too low|exceeded (?:your |the )?(?:usage|rate|monthly|weekly|daily) limit)\b", re.IGNORECASE),
    re.compile(r"(?:^|\b)(?:http|status|error:?)\s*429\b|\b429\s+too many requests\b", re.IGNORECASE),
)
_OUTAGE_PATTERNS = (
    re.compile(r"\b(?:overloaded_error|api_error|service unavailable|bad gateway|gateway timeout)\b", re.IGNORECASE),
    re.compile(r"\b(?:internal server error|temporarily unavailable|server error|upstream connect error)\b", re.IGNORECASE),
    re.compile(r"(?:^|\b)(?:http|status|error:?)\s*(?:500|502|503|504|529)\b", re.IGNORECASE),
    re.compile(r"the requested url returned error:\s*(?:500|502|503|504|529)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:econnrefused|econnreset|etimedout|enotfound|eai_again|ehostunreach|enetunreach|epipe)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:connection refused|connection reset|connection timed out|could not resolve host(?:name)?|"
        r"getaddrinfo|fetch failed|network error|socket hang up|tls handshake|tls connection failed|"
        r"ssl handshake|ssl error|certificate has expired|x509:|request timed out|"
        r"unable to connect to (?:the )?api|check your internet connection|"
        r"failed to connect|could not connect|network is unreachable|"
        r"temporary failure in name resolution|name or service not known|"
        r"nodename nor servname provided|no route to host|operation timed out|connection closed by|"
        r"kex_exchange_identification|early eof|remote end hung up unexpectedly|"
        r"empty reply from server|broken pipe|recv failure|send failure|server returned nothing|"
        r"transfer closed with outstanding|unexpected eof|couldn't connect to server|"
        r"failure when receiving data)\b",
        re.IGNORECASE,
    ),
)
_ACCESS_DENIED_PATTERN = re.compile(
    r"\b(?:permission denied|authentication failed|repository not found|access denied|"
    r"could not read (?:username|password)|terminal prompts disabled|host key verification failed)\b",
    re.IGNORECASE,
)
_GIT_TRANSPORT_PATTERN = re.compile(r"unable to access|rpc failed|curl \d+", re.IGNORECASE)
_HTTP_STATUS = re.compile(
    r"(?:returned error:\s*|\bresponse(?:\s+code)?(?:\s+|[:=]\s*)|\bHTTP\s+code(?:\s+|[:=]\s*)|\bHTTP(?:/[\d.]+)?\s+)(\d{3})\b",
    re.IGNORECASE,
)
_RETRYABLE_CLIENT_STATUSES = frozenset({408, 429})
_ALREADY_ENDED_PATTERNS = (
    re.compile(
        r"\b(?:session(?:\s+[A-Za-z0-9._-]+)?\s+(?:has\s+)?(?:already\s+)?(?:ended|stopped|completed|archived|expired|terminated)|"
        r"session(?:\s+[A-Za-z0-9._-]+)?\s+(?:is\s+)?(?:not\s+running|no\s+longer\s+active|already\s+closed)|"
        r"cannot\s+message\s+(?:an?\s+)?(?:ended|archived|completed|stopped)\s+session)\b",
        re.IGNORECASE,
    ),
)
_SESSION_NOT_FOUND_PATTERNS = (
    re.compile(r"\bsession(?:\s+[A-Za-z0-9._-]+)?\s+(?:is\s+)?not\s+found\b", re.IGNORECASE),
)

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.Lock] = {}


def _thread_lock_for(lock_path: Path) -> threading.Lock:
    key = lock_path.resolve()
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


class _TrustPromptError(Exception):
    """Raised internally when the PTY stream hits Claude Code's workspace-trust dialog."""


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RunPtyCommand = Callable[..., subprocess.CompletedProcess[str]]


def _strip_ansi(text: str) -> str:
    return _ESCAPE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def _looks_like_trust_prompt(text: str) -> bool:
    low = _strip_ansi(text).lower()
    if "quick safety check" in low and ("trust this folder" in low or "project you created" in low):
        return True
    return "yes, i trust this folder" in low and "no, exit" in low


def _sanitize_error_text(text: str, *, max_len: int = 160) -> str:
    cleaned = _strip_ansi(text)
    cleaned = _URL_IN_TEXT.sub("<url>", cleaned)
    cleaned = _SECRET_IN_TEXT.sub("<redacted>", cleaned)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    summary = " ".join(lines)
    summary = re.sub(r"\s+", " ", summary).strip()
    return summary[:max_len]


def _message_text(text: str) -> str:
    return _LOCATION_IN_TEXT.sub("<location>", _strip_ansi(text))


def _is_usage_limit(text: str) -> bool:
    plain = _message_text(text)
    return any(pattern.search(plain) for pattern in _USAGE_LIMIT_PATTERNS)


def _is_outage(text: str) -> bool:
    plain = _message_text(text)
    return any(pattern.search(plain) for pattern in _OUTAGE_PATTERNS)


def _is_client_error(status: int) -> bool:
    return 400 <= status < 500 and status not in _RETRYABLE_CLIENT_STATUSES


def _is_access_denied(text: str) -> bool:
    plain = _message_text(text)
    if _ACCESS_DENIED_PATTERN.search(plain):
        return True
    return any(_is_client_error(int(code)) for code in _HTTP_STATUS.findall(plain))


def _is_git_transport_failure(text: str) -> bool:
    plain = _message_text(text)
    return _GIT_TRANSPORT_PATTERN.search(plain) is not None


def _is_already_ended(text: str, *, under_launch_account: bool) -> bool:
    plain = _message_text(text)
    if _is_usage_limit(plain) or _is_outage(plain):
        return False
    if any(pattern.search(plain) for pattern in _ALREADY_ENDED_PATTERNS):
        return True
    return under_launch_account and any(pattern.search(plain) for pattern in _SESSION_NOT_FOUND_PATTERNS)


def _classify_cli_failure(prefix: str, text: str, returncode: int) -> LaunchFailed | RunnerOutage | UsageLimit:
    plain = _message_text(text)
    detail = _sanitize_error_text(text) or f"exit {returncode}"
    if _is_usage_limit(plain):
        return UsageLimit(f"{prefix}: {detail}")
    if _is_access_denied(plain):
        return LaunchFailed(f"{prefix}: {detail}")
    if _is_outage(plain):
        return RunnerOutage(f"{prefix}: {detail}")
    return LaunchFailed(f"{prefix}: {detail}")


def _assert_safe_host_path(path: Path, *, home: Path) -> Path:
    resolved = path.resolve()
    home_resolved = home.resolve()
    protected = (
        *((home_resolved / sub).resolve() for sub in _FORBIDDEN_HOST_SUBPATHS),
        *(prefix.resolve() for prefix in _FORBIDDEN_ABSOLUTE_PREFIXES),
    )
    for item in protected:
        if resolved == item or item in resolved.parents or resolved in item.parents:
            raise LaunchFailed(f"refusing host path that overlaps a protected directory: {item}")
    return resolved


def _assert_safe_source(url: str, *, home: Path) -> str:
    try:
        check_remote_url(url)
    except WorktreeError as exc:
        raise LaunchFailed(str(exc)) from None
    match = _URL_SCHEME.match(url)
    if match and match.group(1).lower() in _REMOTE_SCHEMES:
        return url
    if match and match.group(1) == "file":
        decoded = unquote(match.group(2))
        if "/" not in decoded:
            raise LaunchFailed(f"unsupported repository source: {url!r}")
        _assert_safe_host_path(Path(decoded[decoded.index("/"):]), home=home)
        return url
    first = url.split("/", 1)[0]
    if match or "::" in first or first.lower().startswith("file:"):
        raise LaunchFailed(f"unsupported repository source: {url!r}")
    _assert_safe_host_path(Path(url), home=home)
    return url


def _branch_slug(base_ref: str) -> str:
    if _SIMPLE_SLUG.fullmatch(base_ref) and base_ref not in (".", ".."):
        return base_ref
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "__", base_ref).strip("._-") or "ref"
    digest = hashlib.sha256(base_ref.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:48]}-{digest}"


def _lane_marker_stem(lane_id: str) -> str:
    return hashlib.sha256(lane_id.encode("utf-8")).hexdigest()[:24]


def _lane_marker_name(lane_id: str) -> str:
    return f"{_lane_marker_stem(lane_id)}.json"


def _read_json(path: Path) -> dict:
    try:
        if path.is_symlink():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LaunchFailed(f"refusing to write symlink marker: {path.name}")
    body = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_json_if_absent(path: Path, data: Mapping[str, object]) -> bool:
    """Atomically write JSON only if `path` does not already exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        try:
            os.link(tmp_name, path)
            return True
        except FileExistsError:
            return False
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _lane_record(session_record: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in session_record.items() if key != "state"}


def _parse_int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return default


def _parse_environments_env(raw: str) -> Mapping[str, str]:
    text = raw.strip()
    if not text:
        return MappingProxyType({})
    parsed: dict[str, str] = {}
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            return MappingProxyType({})
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
                    parsed[key.strip()] = value.strip()
        return MappingProxyType(parsed)
    for entry in text.split(","):
        if "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        if key.strip() and value.strip():
            parsed[key.strip()] = value.strip()
    return MappingProxyType(parsed)


def _default_run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_STOP_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        proc.terminate()
    deadline = time.monotonic() + _KILL_GRACE_SECONDS
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            proc.kill()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _default_run_pty(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_LAUNCH_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    master_fd, slave_fd = pty.openpty()
    proc: subprocess.Popen[bytes] | None = None
    try:
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", _PTY_ROWS, _PTY_COLS, 0, 0))
        except OSError:
            pass

        def _attach_ctty() -> None:
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen(
            list(argv),
            cwd=None if cwd is None else str(cwd),
            env=None if env is None else dict(env),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=_attach_ctty,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise

    os.close(slave_fd)
    buf = bytearray()
    deadline = time.monotonic() + timeout
    trust_detected = False
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            poll_wait = min(0.1, remaining) if proc.poll() is None else 0.05
            ready, _, _ = select.select([master_fd], [], [], poll_wait)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                if len(buf) < _MAX_OUTPUT_BYTES:
                    buf.extend(chunk[: _MAX_OUTPUT_BYTES - len(buf)])
                if _looks_like_trust_prompt(buf.decode("utf-8", errors="replace")):
                    trust_detected = True
                    break
            elif proc.poll() is not None:
                break

        if trust_detected or timed_out:
            _terminate_process_group(proc)
        else:
            remaining = max(0.5, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(proc)
    finally:
        os.close(master_fd)

    output = buf.decode("utf-8", errors="replace")
    if trust_detected:
        raise _TrustPromptError(output)
    if timed_out:
        raise subprocess.TimeoutExpired(list(argv), timeout, output=output)
    return subprocess.CompletedProcess(list(argv), proc.returncode, output, "")


def parse_launch_output(output: str) -> tuple[Launched, str]:
    """Parse session ID, URL, and optional machine from `claude --cloud` output."""
    plain = _strip_ansi(output).strip()
    machine = "unknown"
    if plain.startswith("{"):
        try:
            data = json.loads(plain)
        except ValueError:
            data = None
        if isinstance(data, dict):
            sid = data.get("session_id")
            url = data.get("url")
            raw_machine = data.get("machine")
            if isinstance(raw_machine, str) and raw_machine.strip():
                machine = raw_machine.strip()
            if isinstance(sid, str) and isinstance(url, str):
                return _validated_launched(sid, url), machine

    view_match = _VIEW_URL.search(plain)
    resume_match = _RESUME_ID.search(plain)
    url = view_match.group(1).rstrip(".,;)") if view_match else None
    agent_id = resume_match.group(1) if resume_match else None
    if agent_id is None and url is not None:
        from_url = _SESSION_FROM_URL.search(url)
        if from_url:
            agent_id = from_url.group(1)
    machine_match = _MACHINE_LINE.search(plain)
    if machine_match:
        machine = machine_match.group(1)
    if not agent_id or not url:
        raise LaunchFailed("claude --cloud did not print a session id and view URL")
    return _validated_launched(agent_id, url), machine


def _validated_launched(agent_id: str, url: str) -> Launched:
    if not _SAFE_AGENT_ID.fullmatch(agent_id) or ".." in agent_id:
        raise LaunchFailed(f"claude --cloud returned an unsafe session id: {agent_id!r}")
    if not url.startswith("https://") or any(ch.isspace() for ch in url):
        raise LaunchFailed("claude --cloud returned an invalid session URL")
    from_url = _SESSION_FROM_URL.search(url)
    if from_url and from_url.group(1) != agent_id:
        raise LaunchFailed("claude --cloud session link and session id disagree")
    return Launched(agent_id=agent_id, url=url)


@dataclass(frozen=True)
class ClaudeCloudConfig:
    config_dir: Path | None
    work_root: Path = DEFAULT_WORK_ROOT
    repo_cache_root: Path = DEFAULT_REPO_CACHE
    claude_binary: str = DEFAULT_BINARY
    git_binary: str = "git"
    launch_timeout_seconds: int = DEFAULT_LAUNCH_TIMEOUT_SECONDS
    stop_timeout_seconds: int = DEFAULT_STOP_TIMEOUT_SECONDS
    repo_url_template: str = DEFAULT_REPO_URL_TEMPLATE
    stop_message: str = DEFAULT_STOP_MESSAGE
    environments: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ClaudeCloudConfig:
        env = os.environ if environ is None else environ
        raw_config = env.get(CONFIG_DIR_ENV, "").strip()
        config_dir = Path(raw_config) if raw_config else None
        work_root = Path(env.get(WORK_ROOT_ENV) or DEFAULT_WORK_ROOT)
        repo_cache = Path(env.get(REPO_CACHE_ENV) or DEFAULT_REPO_CACHE)
        binary = env.get(BINARY_ENV) or DEFAULT_BINARY
        launch_timeout = _parse_int_env(env, LAUNCH_TIMEOUT_ENV, DEFAULT_LAUNCH_TIMEOUT_SECONDS)
        stop_timeout = _parse_int_env(env, STOP_TIMEOUT_ENV, DEFAULT_STOP_TIMEOUT_SECONDS)
        url_template = env.get(REPO_URL_TEMPLATE_ENV) or DEFAULT_REPO_URL_TEMPLATE
        environments = _parse_environments_env(env.get(ENVIRONMENTS_ENV, ""))
        return cls(
            config_dir=config_dir,
            work_root=work_root,
            repo_cache_root=repo_cache,
            claude_binary=binary,
            launch_timeout_seconds=launch_timeout,
            stop_timeout_seconds=stop_timeout,
            repo_url_template=url_template,
            environments=environments,
        )


class ClaudeCloudRunner:
    def __init__(
        self,
        config: ClaudeCloudConfig | None = None,
        *,
        run: RunCommand = _default_run,
        run_pty: RunPtyCommand = _default_run_pty,
        home: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config or ClaudeCloudConfig.from_env(environ)
        self._run = run
        self._run_pty = run_pty
        self._home = Path.home() if home is None else Path(home)
        self._environ = os.environ if environ is None else environ
        self._by_lane: dict[str, Launched] = {}
        self._version: str | None = None

    def _work_root(self) -> Path:
        return _assert_safe_host_path(self.config.work_root, home=self._home)

    def _repo_cache_root(self) -> Path:
        return _assert_safe_host_path(self.config.repo_cache_root, home=self._home)

    def _lane_marker(self, lane_id: str) -> Path:
        return _assert_safe_host_path(
            self._work_root() / "lanes" / _lane_marker_name(lane_id),
            home=self._home,
        )

    def _lane_lock(self, lane_id: str) -> Path:
        return _assert_safe_host_path(
            self._work_root() / "lanes" / f"{_lane_marker_stem(lane_id)}.lock",
            home=self._home,
        )

    def _sessions_dir(self) -> Path:
        return _assert_safe_host_path(self._work_root() / "sessions", home=self._home)

    def _session_marker(self, agent_id: str) -> Path:
        if not _SAFE_AGENT_ID.fullmatch(agent_id) or ".." in agent_id:
            raise LaunchFailed(f"unsafe agent_id: {agent_id!r}")
        return _assert_safe_host_path(
            self._sessions_dir() / f"{agent_id}.json",
            home=self._home,
        )

    def _session_lock(self, agent_id: str) -> Path:
        if not _SAFE_AGENT_ID.fullmatch(agent_id) or ".." in agent_id:
            raise LaunchFailed(f"unsafe agent_id: {agent_id!r}")
        return _assert_safe_host_path(
            self._sessions_dir() / f"{agent_id}.lock",
            home=self._home,
        )

    def _validate_config_dir(self, value: str, label: str) -> str:
        raw = value.strip()
        if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
            raise LaunchFailed(f"{label} is invalid")
        return raw

    def _config_dir(self) -> str:
        if self.config.config_dir is None:
            raise LaunchFailed(f"{CONFIG_DIR_ENV} is not set")
        return self._validate_config_dir(str(self.config.config_dir), CONFIG_DIR_ENV)

    def _cli_env(self, config_dir: str | None = None) -> dict[str, str]:
        resolved = self._config_dir() if config_dir is None else self._validate_config_dir(config_dir, CONFIG_DIR_ENV)
        base = {
            name: value
            for name, value in self._environ.items()
            if name in _CLI_ENV_ALLOWLIST or name.startswith("LC_")
        }
        return account_environ(base, "claude", resolved)

    def _resolve_environment(self, lane: Lane) -> tuple[str | None, str]:
        """Resolve lane.environment_name to (cli --environment ccpool_id | None, CLAUDE_CONFIG_DIR)."""
        name = (lane.environment_name or "").strip()
        if not name:
            raise LaunchFailed("qa.claude_cloud.environment is empty")
        if not _SAFE_OPTION_VALUE.fullmatch(name):
            raise LaunchFailed(f"unsafe environment name: {lane.environment_name!r}")
        mapped = self.config.environments.get(name)
        if mapped is not None:
            target = mapped.strip()
            if _CCPOOL_ID.fullmatch(target):
                return target, self._config_dir()
            if Path(target).is_absolute():
                return None, self._validate_config_dir(target, f"{ENVIRONMENTS_ENV}[{name}]")
            raise LaunchFailed(
                f"invalid {ENVIRONMENTS_ENV} target for {name!r}: expected a ccpool_... id or absolute config directory path"
            )
        if _CCPOOL_ID.fullmatch(name):
            return name, self._config_dir()
        raise LaunchFailed(
            f"Claude Code --environment expects a self-hosted environment id (ccpool_...), got {name!r}; "
            f"map {name!r} in {ENVIRONMENTS_ENV} to a ccpool_... id or a dedicated /remote-env config directory"
        )

    def _validate_lane(self, lane: Lane) -> str:
        if not _SAFE_REPO.fullmatch(lane.repo) or ".." in lane.repo:
            raise LaunchFailed(f"unsafe repository name: {lane.repo!r}")
        if not _SAFE_REF.fullmatch(lane.base_ref) or ".." in lane.base_ref or lane.base_ref.startswith("-"):
            raise LaunchFailed(f"unsafe base_ref: {lane.base_ref!r}")
        if lane.model is not None and not _SAFE_OPTION_VALUE.fullmatch(lane.model):
            raise LaunchFailed(f"unsafe model name: {lane.model!r}")
        for sha in lane.commits:
            if not _SHA.fullmatch(sha):
                raise LaunchFailed(f"unsafe commit: {sha!r}")
        return _assert_safe_source(self.config.repo_url_template.format(repo=lane.repo), home=self._home)

    def checkout_path_for(self, repo: str, base_ref: str) -> Path:
        if not _SAFE_REPO.fullmatch(repo) or ".." in repo:
            raise LaunchFailed(f"unsafe repository name: {repo!r}")
        if not _SAFE_REF.fullmatch(base_ref) or ".." in base_ref or base_ref.startswith("-"):
            raise LaunchFailed(f"unsafe base_ref: {base_ref!r}")
        owner, name = repo.split("/", 1)
        return _assert_safe_host_path(
            self._work_root() / "checkouts" / f"{owner}__{name}" / _branch_slug(base_ref),
            home=self._home,
        )

    @contextmanager
    def _lock(self, lock_path: Path) -> Iterator[None]:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        thread_lock = _thread_lock_for(lock_path)
        with thread_lock:
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        timeout: int = DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._run([self.config.git_binary, *args], cwd=cwd, env=self._environ, timeout=timeout)
        except FileNotFoundError:
            raise LaunchFailed("git binary is not installed") from None
        except subprocess.TimeoutExpired:
            raise RunnerOutage("git command timed out") from None
        except OSError as exc:
            raise LaunchFailed(f"git invocation failed: {exc}") from None

    def _raise_git_failure(self, subcommand: str, proc: subprocess.CompletedProcess[str]) -> None:
        combined = "\n".join(part for part in (proc.stderr, proc.stdout) if part)
        err = _sanitize_error_text(combined) or f"exit {proc.returncode}"
        if not _is_access_denied(combined) and (_is_outage(combined) or _is_git_transport_failure(combined)):
            raise RunnerOutage(f"git {subcommand} failed: {err}")
        raise LaunchFailed(f"git {subcommand} failed: {err}")

    def _git_ok(self, *args: str, cwd: Path | None = None, timeout: int = DEFAULT_GIT_TIMEOUT_SECONDS) -> str:
        proc = self._git(*args, cwd=cwd, timeout=timeout)
        if proc.returncode != 0:
            self._raise_git_failure(args[0], proc)
        return (proc.stdout or "").strip()

    def _prepare_checkout(self, lane: Lane, remote_url: str) -> Path:
        owner, name = lane.repo.split("/", 1)
        cache_root = self._repo_cache_root()
        repo_cache = _assert_safe_host_path(cache_root / f"{owner}__{name}.git", home=self._home)
        cache_lock = cache_root / f"{owner}__{name}.lock"
        checkout = self.checkout_path_for(lane.repo, lane.base_ref)
        temp_ref = f"refs/factory-qa/{_lane_marker_stem(lane.lane_id)}/base"

        with self._lock(cache_lock):
            if not repo_cache.exists():
                repo_cache.parent.mkdir(parents=True, exist_ok=True)
                self._git_ok("clone", "--bare", "--", remote_url, str(repo_cache))
            else:
                self._git_ok("remote", "set-url", "origin", remote_url, cwd=repo_cache)

            if _SHA.fullmatch(lane.base_ref):
                branch_proc = self._git(
                    "fetch", "origin", f"+refs/heads/{lane.base_ref}:{temp_ref}", cwd=repo_cache
                )
                if branch_proc.returncode == 0:
                    is_branch = True
                else:
                    combined = "\n".join(part for part in (branch_proc.stderr, branch_proc.stdout) if part)
                    if _is_access_denied(combined) or _is_outage(combined) or _is_git_transport_failure(combined):
                        self._raise_git_failure("fetch", branch_proc)
                    self._git_ok("fetch", "origin", f"+{lane.base_ref}:{temp_ref}", cwd=repo_cache)
                    is_branch = False
            else:
                self._git_ok(
                    "fetch", "origin", f"+refs/heads/{lane.base_ref}:{temp_ref}", cwd=repo_cache
                )
                is_branch = True
            try:
                base_sha = self._git_ok("rev-parse", "--verify", f"{temp_ref}^{{commit}}", cwd=repo_cache)
                if not (checkout / ".git").exists():
                    if checkout.exists() or checkout.is_symlink():
                        if checkout.is_symlink() or checkout.is_file():
                            checkout.unlink()
                        else:
                            shutil.rmtree(checkout)
                    checkout.parent.mkdir(parents=True, exist_ok=True)
                    staging = _assert_safe_host_path(checkout.parent / f".{checkout.name}.init", home=self._home)
                    if staging.exists() or staging.is_symlink():
                        if staging.is_symlink() or staging.is_file():
                            staging.unlink()
                        else:
                            shutil.rmtree(staging)
                    self._git_ok("init", "--quiet", str(staging))
                    staging.rename(checkout)
                self._git_ok("config", "remote.origin.url", remote_url, cwd=checkout)
                self._git_ok("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=checkout)
                self._git_ok(
                    "fetch", "--force", str(repo_cache), f"+{temp_ref}:refs/remotes/origin/{lane.base_ref}",
                    cwd=checkout,
                )
            finally:
                self._git("update-ref", "-d", temp_ref, cwd=repo_cache)

        # Clean any local drift (including nested git repos) before checkout so advancing base_ref stays clean.
        if self._git("rev-parse", "--verify", "HEAD", cwd=checkout).returncode == 0:
            self._git_ok("reset", "--hard", "--quiet", "HEAD", cwd=checkout)
        self._git_ok("clean", "-ffdx", "-q", cwd=checkout)
        if is_branch:
            self._git_ok("checkout", "--quiet", "-B", lane.base_ref, base_sha, cwd=checkout)
            self._git_ok("config", f"branch.{lane.base_ref}.remote", "origin", cwd=checkout)
            self._git_ok("config", f"branch.{lane.base_ref}.merge", f"refs/heads/{lane.base_ref}", cwd=checkout)
        else:
            self._git_ok("checkout", "--quiet", "--detach", base_sha, cwd=checkout)
        self._git_ok("reset", "--hard", "--quiet", base_sha, cwd=checkout)
        self._git_ok("clean", "-ffdx", "-q", cwd=checkout)
        return checkout

    def build_launch_argv(self, lane: Lane, *, cli_environment: str | None = None) -> list[str]:
        argv = [self.config.claude_binary]
        if cli_environment:
            argv.extend(["--environment", cli_environment])
        if lane.model:
            argv.extend(["--model", lane.model])
        argv.extend(["--cloud", lane.brief])
        return argv

    def build_stop_argv(self, agent_id: str) -> list[str]:
        return [
            self.config.claude_binary,
            "-p",
            self.config.stop_message,
            "--cloud",
            agent_id,
            "--output-format",
            "json",
        ]

    def _saved_lane(self, lane_id: str) -> Launched | None:
        saved = _read_json(self._lane_marker(lane_id))
        if saved.get("lane_id") == lane_id:
            agent_id = saved.get("agent_id")
            url = saved.get("url")
            if isinstance(agent_id, str) and isinstance(url, str) and agent_id and url:
                session_marker = self._session_marker(agent_id)
                config_dir = saved.get("config_dir")
                if isinstance(config_dir, str) and config_dir:
                    with self._lock(self._session_lock(agent_id)):
                        if not session_marker.exists():
                            _write_json_if_absent(session_marker, {**saved, "state": "running"})
                return Launched(agent_id=agent_id, url=url)
        sessions_dir = self._sessions_dir()
        if sessions_dir.is_dir():
            for marker in sorted(sessions_dir.glob("*.json")):
                record = _read_json(marker)
                if record.get("lane_id") != lane_id:
                    continue
                agent_id = record.get("agent_id")
                url = record.get("url")
                if isinstance(agent_id, str) and isinstance(url, str) and agent_id and url:
                    adopted = Launched(agent_id=agent_id, url=url)
                    _write_json_atomic(self._lane_marker(lane_id), _lane_record(record))
                    return adopted
        return None

    def _persist_markers(
        self,
        lane: Lane,
        launched: Launched,
        *,
        checkout: Path,
        machine: str,
        config_dir: str,
    ) -> None:
        record = {
            "agent_id": launched.agent_id,
            "checkout": str(checkout),
            "config_dir": config_dir,
            "lane_id": lane.lane_id,
            "machine": machine,
            "state": "running",
            "url": launched.url,
        }
        _write_json_atomic(self._session_marker(launched.agent_id), record)
        _write_json_atomic(self._lane_marker(lane.lane_id), _lane_record(record))

    def launch(self, lane: Lane) -> Launched:
        if lane.lane_id in self._by_lane:
            return self._by_lane[lane.lane_id]

        with self._lock(self._lane_lock(lane.lane_id)):
            if lane.lane_id in self._by_lane:
                return self._by_lane[lane.lane_id]
            saved = self._saved_lane(lane.lane_id)
            if saved is not None:
                self._by_lane[lane.lane_id] = saved
                return saved

            remote_url = self._validate_lane(lane)
            cli_environment, resolved_config_dir = self._resolve_environment(lane)
            cli_env = self._cli_env(resolved_config_dir)
            checkout = self.checkout_path_for(lane.repo, lane.base_ref)
            checkout_lock = checkout.parent / f"{checkout.name}.lock"

            with self._lock(checkout_lock):
                self._prepare_checkout(lane, remote_url)
                argv = self.build_launch_argv(lane, cli_environment=cli_environment)
                try:
                    proc = self._run_pty(
                        argv,
                        cwd=checkout,
                        env=cli_env,
                        timeout=self.config.launch_timeout_seconds,
                    )
                except _TrustPromptError:
                    raise LaunchFailed(
                        f"claude workspace trust is not accepted for {checkout}; "
                        "pre-trust this folder once with the QA worker's Claude config directory"
                    ) from None
                except FileNotFoundError:
                    raise RunnerOutage("claude binary is not installed") from None
                except subprocess.TimeoutExpired:
                    raise RunnerOutage("claude --cloud launch timed out") from None
                except OSError as exc:
                    raise RunnerOutage(f"claude invocation failed: {exc}") from None

            combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
            if _looks_like_trust_prompt(combined):
                raise LaunchFailed(
                    f"claude workspace trust is not accepted for {checkout}; "
                    "pre-trust this folder once with the QA worker's Claude config directory"
                )
            if proc.returncode != 0:
                raise _classify_cli_failure("claude --cloud failed", combined, proc.returncode)
            stripped = _strip_ansi(combined).strip()
            if stripped.startswith("{"):
                try:
                    payload = json.loads(stripped)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and payload.get("ok") is False:
                    err_text = str(payload.get("error") or payload.get("message") or combined)
                    raise _classify_cli_failure("claude --cloud failed", err_text, proc.returncode)

            launched, machine = parse_launch_output(combined)
            self._persist_markers(
                lane,
                launched,
                checkout=checkout,
                machine=machine,
                config_dir=resolved_config_dir,
            )
            self._by_lane[lane.lane_id] = launched
            return launched

    def status(self, agent_id: str) -> AgentStatus:
        """Return the locally recorded session state.

        Claude Code 2.1.322 exposes no non-interactive CLI command or flag to
        query the live status of a ``--cloud`` session (#780; ``claude cloud``
        and ``claude sessions`` do not exist, and ``attach``/``logs``/``stop``/
        ``rm``/``respawn`` apply to background sessions only). ``status()``
        therefore reflects the session marker written by ``launch()`` and
        flipped to ``"ended"`` once ``stop()`` succeeds or observes an
        already-ended session. Silent remote exits that never upload a result
        are reclaimed by the worker's ``qa.timeout_minutes`` deadline, which
        invokes ``stop()`` and records ``timed-out``.
        """
        saved = _read_json(self._session_marker(agent_id))
        if saved.get("state") == "ended":
            return "ended"
        return "running"

    def _mark_ended(self, agent_id: str, saved: Mapping[str, object]) -> None:
        updated = dict(saved)
        updated["agent_id"] = agent_id
        updated["state"] = "ended"
        _write_json_atomic(self._session_marker(agent_id), updated)

    def stop(self, agent_id: str) -> None:
        marker = self._session_marker(agent_id)
        with self._lock(self._session_lock(agent_id)):
            saved = _read_json(marker)
            if saved.get("state") == "ended":
                return

            saved_config = saved.get("config_dir")
            under_launch_account = isinstance(saved_config, str) and bool(saved_config)
            cli_env = self._cli_env(saved_config if under_launch_account else None)
            checkout_raw = saved.get("checkout")
            cwd: Path | None = None
            if isinstance(checkout_raw, str) and checkout_raw:
                candidate = Path(checkout_raw)
                if candidate.is_dir():
                    cwd = candidate

            try:
                proc = self._run(
                    self.build_stop_argv(agent_id),
                    cwd=cwd,
                    env=cli_env,
                    timeout=self.config.stop_timeout_seconds,
                )
            except FileNotFoundError:
                raise RunnerOutage("claude binary is not installed") from None
            except subprocess.TimeoutExpired:
                raise RunnerOutage("claude stop command timed out") from None
            except OSError as exc:
                raise RunnerOutage(f"claude stop invocation failed: {exc}") from None

            combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
            if proc.returncode == 0:
                raw_out = (proc.stdout or "").strip()
                if raw_out.startswith("{"):
                    try:
                        data = json.loads(raw_out)
                    except ValueError:
                        data = None
                    if isinstance(data, dict) and data.get("ok") is False:
                        err_text = str(data.get("error") or data.get("message") or combined)
                        if _is_already_ended(err_text, under_launch_account=under_launch_account):
                            self._mark_ended(agent_id, saved)
                            return
                        raise _classify_cli_failure("claude stop failed", err_text, proc.returncode)
                self._mark_ended(agent_id, saved)
                return

            if _is_already_ended(combined, under_launch_account=under_launch_account):
                self._mark_ended(agent_id, saved)
                return
            raise _classify_cli_failure("claude stop failed", combined, proc.returncode)

    def _cli_version(self) -> str:
        try:
            proc = self._run(
                [self.config.claude_binary, "--version"],
                env=self._cli_env(),
                timeout=DEFAULT_VERSION_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            raise RunnerOutage("claude binary is not installed") from None
        except subprocess.TimeoutExpired:
            raise RunnerOutage("claude --version timed out") from None
        except OSError as exc:
            raise RunnerOutage(f"claude --version failed: {exc}") from None
        if proc.returncode != 0:
            combined = "\n".join(part for part in (proc.stderr, proc.stdout) if part)
            raise RunnerOutage(
                f"claude --version failed: {_sanitize_error_text(combined) or f'exit {proc.returncode}'}"
            )
        out = _strip_ansi(proc.stdout or "").strip()
        match = _VERSION_TOKEN.search(out)
        version = match.group(1) if match else (out.splitlines()[0].strip() if out else "unknown")
        self._version = version
        return version

    def describe(self, agent_id: str) -> AgentInfo:
        saved = _read_json(self._session_marker(agent_id))
        raw_machine = saved.get("machine")
        machine = raw_machine.strip() if isinstance(raw_machine, str) and raw_machine.strip() else "unknown"
        version = self._version
        if version is None:
            try:
                version = self._cli_version()
            except (LaunchFailed, RunnerOutage, UsageLimit):
                version = "unknown"
        agent_version = version if version.startswith("claude-") else f"claude-{version}"
        return AgentInfo(machine=machine, version=agent_version)

    def ping(self) -> str:
        self._config_dir()
        return self._cli_version()


register(RUNNER_NAME, ClaudeCloudRunner)
