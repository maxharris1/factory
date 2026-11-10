"""Pinned pstack source (docs/SPEC.md F1-02, docs/PLAN.md section 9).

skills/pstack is the vendored tree. skills/skills.lock records the pin
commit, the version, and the sha256 of each file. bump_pin writes those
three together. install-skills symlinks that tree into the local harness
directories, or copies it into the project for a cloud run. doctor prints
a canary that asks for poteto-mode and a token from the pinned skill. The
owning bot starts the harness and writes the reply. This module does not
start claude, codex, or cursor-agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, NoReturn

from lib.load import load_yaml
from lib.schema import HARNESSES

ROOT = Path(__file__).resolve().parent.parent
PSTACK_DIR = Path("skills") / "pstack"
LOCK_PATH = Path("skills") / "skills.lock"
SKILL_ROOT = PSTACK_DIR / "skills"
POTETO_SKILL = SKILL_ROOT / "poteto-mode" / "SKILL.md"
PLUGIN_MANIFESTS = (
    PSTACK_DIR / ".cursor-plugin" / "plugin.json",
    PSTACK_DIR / "plugin.json",
)

# Same relative directories for local (under the bot home) and cloud (under the project).
SKILL_DIRS = (
    ".claude/skills",
    ".agents/skills",
    ".cursor/skills",
)
DIR_HARNESS = {
    ".claude/skills": "claude",
    ".agents/skills": "codex",
    ".cursor/skills": "cursor",
}
OWNERS = {
    "claude": "claude-bot",
    "codex": "codex-bot",
    "cursor": "cursor-bot",
}

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOCK_KEYS = frozenset({"version", "commit", "token", "files"})
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_MARKER = ".orch-owned"
_INSTALLER = "orch-install-skills"
Mode = Literal["local", "cloud"]


@dataclass(frozen=True)
class Pin:
    version: str
    commit: str
    token: str
    files: tuple[tuple[str, str], ...]

    def hashes(self) -> dict[str, str]:
        return dict(self.files)


def lock_file(root: Path) -> Path:
    return root / LOCK_PATH


def pstack_root(root: Path) -> Path:
    return root / PSTACK_DIR


def load_pin(root: Path) -> Pin:
    path = lock_file(root)
    data = load_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("skills.lock is not a mapping")
    unknown = set(data) - _LOCK_KEYS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ValueError(f"skills.lock has unknown keys: {names}")
    missing = _LOCK_KEYS - set(data)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"skills.lock is missing {names}")
    version = data["version"]
    commit = data["commit"]
    token = data["token"]
    files = data["files"]
    if not isinstance(version, str) or not version:
        raise ValueError("skills.lock version is not a string")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise ValueError("skills.lock commit is not a git sha")
    if not isinstance(token, str) or not token or "\n" in token or token != token.strip():
        raise ValueError("skills.lock token is not one line")
    if not isinstance(files, dict) or not files:
        raise ValueError("skills.lock files is empty")
    rows: list[tuple[str, str]] = []
    for rel, digest in files.items():
        if not isinstance(rel, str) or not _safe_rel(rel):
            raise ValueError(f"skills.lock path is not a relative file: {rel!r}")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError(f"skills.lock hash is not sha256: {rel}")
        rows.append((rel, digest))
    rows.sort()
    return Pin(version=version, commit=commit, token=token, files=tuple(rows))


def verify_pin(root: Path) -> list[str]:
    try:
        pin = load_pin(root)
    except (OSError, ValueError, UnicodeError) as exc:
        return [str(exc)]
    errors: list[str] = []
    found, scan_errors = _scan_tree(root)
    errors.extend(scan_errors)
    try:
        if pin.token != _canary_token(root):
            errors.append("skills.lock: token does not match the pinned poteto-mode skill")
    except (OSError, ValueError, UnicodeError) as exc:
        errors.append(str(exc))
    pinned = pin.hashes()
    for rel in sorted(set(pinned) - set(found)):
        errors.append(f"skills.lock: missing {rel}")
    for rel in sorted(set(found) - set(pinned)):
        errors.append(f"skills.lock: extra {rel}")
    for rel in sorted(set(pinned) & set(found)):
        if pinned[rel] != found[rel]:
            errors.append(f"skills.lock: {rel} hash does not match the pin")
    versions = _manifest_versions(root)
    if isinstance(versions, str):
        errors.append(versions)
    elif any(version != pin.version for version in versions):
        shown = ", ".join(versions)
        errors.append(f"skills.lock: version {pin.version} does not match plugin.json ({shown})")
    return errors


def bump_pin(root: Path, commit: str, version: str) -> Pin:
    """Write the pin commit, the version, and every file hash in one lock file."""
    if _COMMIT.fullmatch(commit) is None:
        raise ValueError("commit is not a git sha")
    if not version:
        raise ValueError("version is empty")
    versions = _manifest_versions(root)
    if isinstance(versions, str):
        raise ValueError(versions)
    if any(found != version for found in versions):
        shown = ", ".join(versions)
        raise ValueError(f"plugin.json version is {shown}, not {version}")
    found, scan_errors = _scan_tree(root)
    if scan_errors:
        raise ValueError(scan_errors[0])
    token = _canary_token(root)
    files = tuple(sorted(found.items()))
    if not files:
        raise ValueError("pstack tree is empty")
    pin = Pin(version=version, commit=commit, token=token, files=files)
    _write_lock(root, pin)
    return pin


def install_skills(root: Path, anchor: Path, mode: Mode) -> list[str]:
    errors = verify_pin(root)
    if errors:
        return errors
    try:
        skills = _skills(root)
        commit = load_pin(root).commit
    except (OSError, ValueError, UnicodeError) as exc:
        return [str(exc)]
    planned: list[tuple[Path, Path, str]] = []
    dir_errors: dict[Path, str] = {}
    for name, src in skills:
        for dest in _destinations(anchor, name, mode):
            skills_dir = dest.parent
            if skills_dir not in dir_errors:
                dir_errors[skills_dir] = _symlink_skills_dir_error(anchor, skills_dir) or ""
            if dir_errors[skills_dir]:
                continue
            refusal = _unowned_error(dest, name)
            if refusal:
                errors.append(refusal)
            else:
                planned.append((src, dest, name))
    for message in dir_errors.values():
        if message:
            errors.append(message)
    if errors:
        return errors
    for src, dest, _name in planned:
        try:
            _place_owned(src, dest, mode, commit)
        except OSError as exc:
            return [f"install interrupted at {dest}: {exc}"]
    return []


def canary_prompt() -> str:
    return (
        "Name the skill you loaded: poteto-mode.\n"
        "Quote the whole first bold passage of the pinned poteto-mode playbook "
        "playbooks/orchestrate.md, exactly as written, on one line.\n"
        "The owning bot starts this harness.\n"
        "Claude Bot writes started-by: claude-bot.\n"
        "Codex Bot writes started-by: codex-bot.\n"
        "Cursor Bot writes started-by: cursor-bot.\n"
        "Reply with these three lines:\n"
        "started-by: <bot>\n"
        "skill: poteto-mode\n"
        "token: <that passage>\n"
    )


def pinned_token(root: Path) -> str:
    return _canary_token(root)


def doctor(
    root: Path,
    anchor: Path,
    mode: Mode,
    responses: Path | None,
    *,
    print_canary: bool,
) -> tuple[int, str, list[str]]:
    errors = verify_pin(root)
    if errors:
        return 1, "", errors
    try:
        token = pinned_token(root)
    except (OSError, ValueError, UnicodeError) as exc:
        return 1, "", [str(exc)]
    prompt = canary_prompt()
    if print_canary:
        return 0, prompt, []
    errors = _check_installed(root, anchor, mode)
    if errors:
        return 1, prompt, errors
    if responses is None:
        errors = [
            f"{harness}: did not load the pinned version (no canary response)"
            for harness in HARNESSES
        ]
        return 1, prompt, errors
    errors = _score_responses(responses, token)
    if errors:
        return 1, prompt, errors
    return 0, prompt, []


def _check_installed(root: Path, anchor: Path, mode: Mode) -> list[str]:
    pin = load_pin(root).hashes()
    errors: list[str] = []
    for name, src in _skills(root):
        prefix = f"skills/{name}/"
        expected = {rel: digest for rel, digest in pin.items() if rel.startswith(prefix)}
        for dest in _destinations(anchor, name, mode):
            harness = _harness_for(dest, anchor, mode)
            errors.extend(_check_dest(src, dest, expected, prefix, harness, mode))
    return errors


def _check_dest(
    src: Path,
    dest: Path,
    expected: Mapping[str, str],
    prefix: str,
    harness: str,
    mode: Mode,
) -> list[str]:
    if mode == "cloud":
        if dest.is_symlink() or _skills_parent_is_symlink(dest):
            return [f"{harness}: did not load the pinned version ({dest} must be a real copy)"]
    elif mode == "local" and dest.is_symlink():
        try:
            linked = dest.resolve(strict=True)
        except OSError:
            linked = None
        if linked != src.resolve():
            return [f"{harness}: did not load the pinned version ({dest} is not the pinned skill)"]
        return []
    elif mode != "local":
        _exhaustive(mode)
    if dest.is_symlink() or not dest.is_dir():
        return [f"{harness}: did not load the pinned version ({dest} is missing)"]
    found: dict[str, Path] = {}
    errors: list[str] = []
    for path in dest.rglob("*"):
        rel_in_skill = path.relative_to(dest).as_posix()
        if path.is_symlink():
            if mode == "cloud":
                errors.append(f"{harness}: did not load the pinned version ({dest} must be a real copy)")
            else:
                errors.append(f"{harness}: did not load the pinned version ({rel_in_skill} is a symlink)")
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            errors.append(f"{harness}: did not load the pinned version ({rel_in_skill} is not a regular file)")
            continue
        if rel_in_skill == _MARKER:
            continue
        found[prefix + rel_in_skill] = path
    for rel in sorted(set(expected) - set(found)):
        errors.append(f"{harness}: did not load the pinned version (missing {rel})")
    for rel in sorted(set(found) - set(expected)):
        errors.append(f"{harness}: did not load the pinned version (extra {rel})")
    for rel in sorted(set(expected) & set(found)):
        if _sha256(found[rel]) != expected[rel]:
            errors.append(f"{harness}: did not load the pinned version ({rel} hash differs)")
    return errors


def _score_responses(responses: Path, token: str) -> list[str]:
    owners = _owners()
    errors: list[str] = []
    for harness in HARNESSES:
        path = responses / harness
        if not path.is_file():
            errors.append(f"{harness}: did not load the pinned version (no canary response)")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(f"{harness}: did not load the pinned version (reply is not utf-8)")
            continue
        lines = [line.strip() for line in text.splitlines()]
        owner = owners[harness]
        if f"started-by: {owner}" not in lines:
            errors.append(f"{harness}: did not load the pinned version (not started by {owner})")
            continue
        if "skill: poteto-mode" not in lines:
            errors.append(f"{harness}: did not load the pinned version (did not name poteto-mode)")
            continue
        if f"token: {token}" not in lines:
            errors.append(f"{harness}: did not load the pinned version (token does not match the pin)")
    return errors


def _owners() -> dict[str, str]:
    missing = [name for name in HARNESSES if name not in OWNERS]
    if missing:
        raise ValueError("no owning bot for " + ", ".join(missing))
    return {name: OWNERS[name] for name in HARNESSES}


def _skills(root: Path) -> tuple[tuple[str, Path], ...]:
    base = root / SKILL_ROOT
    if base.is_symlink() or not base.is_dir():
        raise ValueError("skills/pstack: symlink is not allowed: skills")
    found: list[tuple[str, Path]] = []
    for path in sorted(base.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise ValueError(f"skills/pstack: symlink is not allowed: skills/{path.name}")
        if path.is_dir():
            found.append((path.name, path))
    if not found:
        raise ValueError("pinned pstack has no skills")
    return tuple(found)


def _skill_dir(root: Path, name: str) -> Path:
    return root / SKILL_ROOT / name


def _destinations(anchor: Path, name: str, mode: Mode) -> tuple[Path, ...]:
    if mode != "local" and mode != "cloud":
        _exhaustive(mode)
    return tuple(anchor / rel / name for rel in SKILL_DIRS)


def _harness_for(dest: Path, anchor: Path, mode: Mode) -> str:
    if mode == "local" or mode == "cloud":
        relative = dest.relative_to(anchor).as_posix()
        for rel, harness in DIR_HARNESS.items():
            if relative == f"{rel}/{dest.name}" or relative.startswith(rel + "/"):
                return harness
        raise ValueError(f"no harness for {dest}")
    _exhaustive(mode)


def _place_owned(src: Path, dest: Path, mode: Mode, commit: str) -> None:
    skills_dir = dest.parent
    if skills_dir.is_symlink():
        raise OSError(f"refusing to follow symlinked skills dir {skills_dir}")
    skills_dir.mkdir(parents=True, exist_ok=True)
    if skills_dir.is_symlink() or not skills_dir.is_dir():
        raise OSError(f"refusing to follow symlinked skills dir {skills_dir}")
    stage_root = Path(tempfile.mkdtemp(prefix=".orch-staging-", dir=skills_dir))
    staged = stage_root / dest.name
    try:
        if mode == "local":
            staged.symlink_to(src.resolve(), target_is_directory=True)
        elif mode == "cloud":
            _copy_tree(src, staged)
            _write_marker(staged, commit)
        else:
            _exhaustive(mode)
        _swap_in(staged, dest)
    finally:
        _discard(stage_root)


def _swap_in(staged: Path, dest: Path) -> None:
    if dest.exists() or dest.is_symlink():
        if not _installer_owns(dest, dest.name):
            raise OSError(f"refusing to replace unowned path {dest}")
    if not dest.exists() and not dest.is_symlink():
        os.rename(staged, dest)
        return
    if dest.is_symlink() or dest.is_file():
        os.replace(staged, dest)
        return
    backup_name = tempfile.mkdtemp(prefix=f".orch-backup-{dest.name}-", dir=dest.parent)
    os.rmdir(backup_name)
    backup = Path(backup_name)
    os.rename(dest, backup)
    try:
        os.rename(staged, dest)
    except OSError:
        os.rename(backup, dest)
        raise
    _discard(backup)


def _copy_tree(src: Path, dest: Path) -> None:
    dest.mkdir()
    for path in src.rglob("*"):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise OSError(f"pinned skill contains a non-regular file: {path}")
        target = dest / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            target.chmod(path.stat().st_mode & 0o777)


def _discard(path: Path) -> None:
    if not path.is_symlink() and not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            child.rmdir()
    path.rmdir()


def _scan_tree(root: Path) -> tuple[dict[str, str], list[str]]:
    base = pstack_root(root)
    errors: list[str] = []
    if base.is_symlink():
        return {}, ["skills/pstack: symlink is not allowed"]
    if not base.is_dir():
        return {}, ["skills/pstack is missing"]
    hashes: dict[str, str] = {}
    for path in base.rglob("*"):
        rel = path.relative_to(base).as_posix()
        if path.is_symlink():
            errors.append(f"skills/pstack: symlink is not allowed: {rel}")
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            errors.append(f"skills/pstack: not a regular file: {rel}")
            continue
        hashes[rel] = _sha256(path)
    return hashes, errors


def _canary_token(root: Path) -> str:
    path = root / SKILL_ROOT / "poteto-mode" / "playbooks" / "orchestrate.md"
    if path.is_symlink() or not path.is_file():
        raise ValueError("pinned poteto-mode playbook is missing")
    match = _BOLD.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError("pinned poteto-mode has no version token")
    token = match.group(1).strip()
    if not token or "\n" in token:
        raise ValueError("pinned poteto-mode has no version token")
    return token


def _symlink_skills_dir_error(anchor: Path, skills_dir: Path) -> str | None:
    try:
        relative = skills_dir.relative_to(anchor)
    except ValueError:
        return f"refusing skills dir outside the target: {skills_dir}"
    current = anchor
    for part in relative.parts:
        current = current / part
        if not current.is_symlink():
            if current.exists() and not current.is_dir():
                return f"refusing skills dir {current}: not a directory"
            continue
        try:
            resolved = current.resolve(strict=True)
        except OSError:
            return f"refusing to follow symlinked skills dir {current}"
        try:
            resolved.relative_to(anchor.resolve())
        except ValueError:
            return f"refusing symlinked skills dir {current}: resolves outside the target"
        return f"refusing to follow symlinked skills dir {current}"
    return None


def _skills_parent_is_symlink(dest: Path) -> bool:
    return dest.parent.is_symlink()


def _unowned_error(dest: Path, name: str) -> str | None:
    if not dest.exists() and not dest.is_symlink():
        return None
    if _installer_owns(dest, name):
        return None
    return f"refusing to replace unowned path {dest}"


def _installer_owns(dest: Path, name: str) -> bool:
    if dest.is_symlink():
        return _symlink_into_pstack(dest, name)
    if dest.is_dir():
        return _has_orch_marker(dest)
    return False


def _symlink_into_pstack(dest: Path, name: str) -> bool:
    try:
        resolved = dest.resolve(strict=True)
    except OSError:
        return False
    parts = resolved.parts
    return (
        resolved.name == name
        and resolved.is_dir()
        and len(parts) >= 4
        and parts[-4:-1] == ("skills", "pstack", "skills")
    )


def _has_orch_marker(dest: Path) -> bool:
    path = dest / _MARKER
    if path.is_symlink() or not path.is_file():
        return False
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError):
        return False
    if len(lines) < 2 or lines[0] != f"installer: {_INSTALLER}":
        return False
    if not lines[1].startswith("commit: "):
        return False
    return _COMMIT.fullmatch(lines[1].removeprefix("commit: ")) is not None


def _write_marker(dest: Path, commit: str) -> None:
    (dest / _MARKER).write_text(
        f"installer: {_INSTALLER}\ncommit: {commit}\n",
        encoding="utf-8",
    )


def _manifest_versions(root: Path) -> tuple[str, ...] | str:
    versions: list[str] = []
    for rel in PLUGIN_MANIFESTS:
        path = root / rel
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return f"{rel.as_posix()}: {exc}"
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(version, str) or not version:
            return f"{rel.as_posix()}: version is missing"
        versions.append(version)
    if len(set(versions)) != 1:
        return "plugin.json copies disagree: " + ", ".join(versions)
    return tuple(versions)


def _write_lock(root: Path, pin: Pin) -> None:
    lines = [
        "# A pin bump updates version, commit, and files together.",
        f"version: {json.dumps(pin.version)}",
        f"commit: {pin.commit}",
        f"token: {json.dumps(pin.token)}",
        "files:",
    ]
    for rel, digest in pin.files:
        lines.append(f"  {rel}: {digest}")
    lines.append("")
    path = lock_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _safe_rel(rel: str) -> bool:
    if not rel or rel.startswith("/") or "\\" in rel:
        return False
    parts = Path(rel).parts
    return ".." not in parts and parts == tuple(rel.split("/"))


def _exhaustive(mode: Mode) -> NoReturn:
    raise ValueError(mode)
