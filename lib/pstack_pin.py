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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, NoReturn

from lib.load import load_yaml, split_front_matter
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
_LOCK_KEYS = frozenset({"version", "commit", "files"})
Mode = Literal["local", "cloud"]


@dataclass(frozen=True)
class Pin:
    version: str
    commit: str
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
    files = data["files"]
    if not isinstance(version, str) or not version:
        raise ValueError("skills.lock version is not a string")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise ValueError("skills.lock commit is not a git sha")
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
    return Pin(version=version, commit=commit, files=tuple(rows))


def verify_pin(root: Path) -> list[str]:
    try:
        pin = load_pin(root)
    except (OSError, ValueError, UnicodeError) as exc:
        return [str(exc)]
    errors: list[str] = []
    found = _tree_hashes(root)
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
    files = tuple(sorted(_tree_hashes(root).items()))
    if not files:
        raise ValueError("pstack tree is empty")
    pin = Pin(version=version, commit=commit, files=files)
    _write_lock(root, pin)
    return pin


def install_skills(root: Path, anchor: Path, mode: Mode) -> list[str]:
    errors = verify_pin(root)
    if errors:
        return errors
    for name, _src in _skills(root):
        for dest in _destinations(anchor, name, mode):
            _place(_skill_dir(root, name), dest, mode)
    return []


def canary_prompt() -> str:
    return (
        "Name the skill you loaded: poteto-mode.\n"
        "Quote the description token from the pinned poteto-mode skill, exactly, on one line.\n"
        "The owning bot starts this harness.\n"
        "Claude Bot writes started-by: claude-bot.\n"
        "Codex Bot writes started-by: codex-bot.\n"
        "Cursor Bot writes started-by: cursor-bot.\n"
        "Reply with these three lines:\n"
        "started-by: <bot>\n"
        "skill: poteto-mode\n"
        "token: <description>\n"
    )


def pinned_token(root: Path) -> str:
    text = (root / POTETO_SKILL).read_text(encoding="utf-8")
    raw, _body = split_front_matter(text)
    data = load_yaml(raw)
    if not isinstance(data, dict):
        raise ValueError("pinned poteto-mode skill has no front matter")
    description = data.get("description")
    if not isinstance(description, str) or not description or description != description.strip():
        raise ValueError("pinned poteto-mode skill has no description token")
    if "\n" in description:
        raise ValueError("pinned poteto-mode description is not one line")
    return description


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
            errors.extend(_check_dest(src, dest, expected, prefix, harness))
    return errors


def _check_dest(
    src: Path,
    dest: Path,
    expected: Mapping[str, str],
    prefix: str,
    harness: str,
) -> list[str]:
    if dest.is_symlink():
        if dest.resolve() != src.resolve():
            return [f"{harness}: did not load the pinned version ({dest} is not the pinned skill)"]
        return []
    if not dest.is_dir():
        return [f"{harness}: did not load the pinned version ({dest} is missing)"]
    found: dict[str, Path] = {}
    for path in dest.rglob("*"):
        if path.is_file() and not path.is_symlink():
            rel = prefix + path.relative_to(dest).as_posix()
            found[rel] = path
    errors: list[str] = []
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
    found = tuple(sorted((path.name, path) for path in base.iterdir() if path.is_dir()))
    if not found:
        raise ValueError("pinned pstack has no skills")
    return found


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


def _place(src: Path, dest: Path, mode: Mode) -> None:
    if dest.is_symlink() or dest.exists():
        if dest.is_dir() and not dest.is_symlink():
            _remove_tree(dest)
        else:
            dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "local":
        dest.symlink_to(src.resolve(), target_is_directory=True)
        return
    if mode == "cloud":
        _copy_tree(src, dest)
        return
    _exhaustive(mode)


def _copy_tree(src: Path, dest: Path) -> None:
    dest.mkdir()
    for path in src.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"pinned skill contains a symlink: {path}")
        target = dest / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            target.chmod(path.stat().st_mode & 0o777)


def _remove_tree(dest: Path) -> None:
    for path in sorted(dest.rglob("*"), reverse=True):
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink()
    dest.rmdir()


def _tree_hashes(root: Path) -> dict[str, str]:
    base = pstack_root(root)
    hashes: dict[str, str] = {}
    for path in base.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        hashes[path.relative_to(base).as_posix()] = _sha256(path)
    return hashes


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
