"""Pinned pstack source (docs/SPEC.md F1-02, docs/PLAN.md section 9).

skills/pstack is the vendored tree, skills/factory holds the factory's
router skills, and skills/cursor-team-kit holds the skills vendored from
cursor-team-kit (deslop). skills/skills.lock records the pin commit, the
version, and the sha256 of each pstack, skills/factory and cursor-team-kit
file. bump_pin writes them together. install-skills symlinks the pstack
skills, the routers and the kit skills into the local harness directories, or
copies them into the project for a cloud run. A local install also links
pstack's agents/ and docs/ into ~/.claude and, given the rules text, writes
~/.claude/rules/pstack-models.md (docs/decisions/178-plugin-dir-and-deslop.md).
doctor prints a canary that asks Claude and Codex
to load factory-builder and quote a token from the pinned poteto-mode. The
owning bot starts the harness and writes the reply. This module does not
start claude or codex.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping, NoReturn

from lib.load import load_yaml

ROOT = Path(__file__).resolve().parent.parent
PSTACK_DIR = Path("skills") / "pstack"
FACTORY_DIR = Path("skills") / "factory"
KIT_DIR = Path("skills") / "cursor-team-kit"
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
CLAUDE_HOME = Path(".claude")
CLAUDE_EXTRAS = ("agents", "docs")
RULES_PATH = CLAUDE_HOME / "rules" / "pstack-models.md"
CANARY_HARNESSES = ("claude", "codex")
OWNERS = {
    "claude": "claude-bot",
    "codex": "codex-bot",
}

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PSTACK_SKILLS = ("skills", "pstack", "skills")
_FACTORY_SKILLS = ("skills", "factory")
_KIT_SKILLS = ("skills", "cursor-team-kit")
_LOCK_KEYS = frozenset({"version", "commit", "token", "files", "factory", "kit"})
_MARKER = ".orch-owned"
_STAGING_PREFIX = ".orch-staging-"
_LEGACY_BACKUP_PREFIX = ".orch-backup-"
_INSTALLER = "orch-install-skills"
Mode = Literal["local", "cloud"]


@dataclass(frozen=True)
class Pin:
    version: str
    commit: str
    token: str
    files: tuple[tuple[str, str], ...]
    factory: tuple[tuple[str, str], ...]
    kit: tuple[tuple[str, str], ...]

    def hashes(self) -> dict[str, str]:
        return dict(self.files)

    def factory_hashes(self) -> dict[str, str]:
        return dict(self.factory)

    def kit_hashes(self) -> dict[str, str]:
        return dict(self.kit)


def lock_file(root: Path) -> Path:
    return root / LOCK_PATH


def pstack_root(root: Path) -> Path:
    return root / PSTACK_DIR


def factory_root(root: Path) -> Path:
    return root / FACTORY_DIR


def kit_root(root: Path) -> Path:
    return root / KIT_DIR


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
    if not isinstance(version, str) or not version:
        raise ValueError("skills.lock version is not a string")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise ValueError("skills.lock commit is not a git sha")
    if not isinstance(token, str) or not token or "\n" in token or token != token.strip():
        raise ValueError("skills.lock token is not one line")
    return Pin(
        version=version,
        commit=commit,
        token=token,
        files=_lock_rows(data["files"], "files"),
        factory=_lock_rows(data["factory"], "factory"),
        kit=_lock_rows(data["kit"], "kit"),
    )


def _lock_rows(section: object, key: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(section, dict) or not section:
        raise ValueError(f"skills.lock {key} is empty")
    rows: list[tuple[str, str]] = []
    for rel, digest in section.items():
        if not isinstance(rel, str) or not _safe_rel(rel):
            raise ValueError(f"skills.lock path is not a relative file: {rel!r}")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError(f"skills.lock hash is not sha256: {rel}")
        rows.append((rel, digest))
    return tuple(sorted(rows))


def verify_pin(root: Path) -> list[str]:
    try:
        pin = load_pin(root)
    except (OSError, ValueError, UnicodeError) as exc:
        return [str(exc)]
    errors: list[str] = []
    found, scan_errors = _scan_tree(root)
    errors.extend(scan_errors)
    factory, factory_errors = _scan_factory(root)
    errors.extend(factory_errors)
    kit, kit_errors = _scan_kit(root)
    errors.extend(kit_errors)
    try:
        if pin.token != _canary_token(root):
            errors.append("skills.lock: token does not match the pinned poteto-mode skill")
    except (OSError, ValueError, UnicodeError) as exc:
        errors.append(str(exc))
    errors.extend(_compare(pin.hashes(), found, ""))
    errors.extend(_compare(pin.factory_hashes(), factory, "factory/"))
    errors.extend(_compare(pin.kit_hashes(), kit, "cursor-team-kit/"))
    versions = _manifest_versions(root)
    if isinstance(versions, str):
        errors.append(versions)
    elif any(version != pin.version for version in versions):
        shown = ", ".join(versions)
        errors.append(f"skills.lock: version {pin.version} does not match plugin.json ({shown})")
    return errors


def _compare(pinned: Mapping[str, str], found: Mapping[str, str], label: str) -> list[str]:
    errors = [f"skills.lock: missing {label}{rel}" for rel in sorted(set(pinned) - set(found))]
    errors.extend(f"skills.lock: extra {label}{rel}" for rel in sorted(set(found) - set(pinned)))
    for rel in sorted(set(pinned) & set(found)):
        if pinned[rel] != found[rel]:
            errors.append(f"skills.lock: {label}{rel} hash does not match the pin")
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
    factory, factory_errors = _scan_factory(root)
    kit, kit_errors = _scan_kit(root)
    if scan_errors or factory_errors or kit_errors:
        raise ValueError((scan_errors + factory_errors + kit_errors)[0])
    token = _canary_token(root)
    files = tuple(sorted(found.items()))
    if not files:
        raise ValueError("pstack tree is empty")
    if not factory:
        raise ValueError("skills/factory is empty")
    if not kit:
        raise ValueError("skills/cursor-team-kit is empty")
    pin = Pin(
        version=version,
        commit=commit,
        token=token,
        files=files,
        factory=tuple(sorted(factory.items())),
        kit=tuple(sorted(kit.items())),
    )
    _write_lock(root, pin)
    return pin


def install_skills(root: Path, anchor: Path, mode: Mode, *, rules: str | None = None) -> list[str]:
    """rules: the ~/.claude/rules/pstack-models.md text, written by a local install only."""
    errors = verify_pin(root)
    if errors:
        return errors
    try:
        skills = _skills(root)
        pin = load_pin(root)
        pinned = _installed_hashes(pin, skills)
    except (OSError, ValueError, UnicodeError) as exc:
        return [str(exc)]
    planned: list[tuple[Path, Path, str]] = []
    dir_errors: dict[Path, str] = {}
    recovered: set[Path] = set()
    for name, src in skills:
        for dest in _destinations(anchor, name, mode):
            skills_dir = dest.parent
            if skills_dir not in dir_errors:
                dir_errors[skills_dir] = _symlink_skills_dir_error(anchor, skills_dir) or ""
            if dir_errors[skills_dir]:
                continue
            if skills_dir not in recovered:
                recovered.add(skills_dir)
                _recover_leftovers(skills_dir, pinned)
            refusal = _unowned_error(dest, name, pinned)
            if refusal:
                errors.append(refusal)
            else:
                planned.append((src, dest, name))
    for message in dir_errors.values():
        if message:
            errors.append(message)
    extras = _claude_extras(root, anchor, pin) if mode == "local" else []
    for src, dest in extras:
        errors.extend(_extra_error(anchor, dest))
    rules_dest = anchor / RULES_PATH
    if mode == "local" and rules is not None:
        errors.extend(_rules_error(anchor, rules_dest, rules, "replace"))
    if errors:
        return errors
    for src, dest, _name in planned:
        try:
            _place_owned(src, dest, mode, pin.commit, pinned)
        except OSError as exc:
            return [f"install interrupted at {dest}: {exc}"]
    try:
        for src, dest in extras:
            _place_atomic(dest, lambda staged, src=src: staged.symlink_to(src.resolve()))
        if mode == "local" and rules is not None:
            _place_atomic(rules_dest, lambda staged: staged.write_text(rules, encoding="utf-8"))
    except OSError as exc:
        return [f"install interrupted at {exc.filename or anchor}: {exc}"]
    return []


def remove_claude_extras(anchor: Path, rules: str) -> list[str]:
    """Undo a local install's agents, docs and rules file; skills stay."""
    claude = anchor / CLAUDE_HOME
    rules_dest = anchor / RULES_PATH
    errors = _rules_error(anchor, rules_dest, rules, "remove")
    links: list[Path] = []
    for top in CLAUDE_EXTRAS:
        base = claude / top
        message = _symlink_skills_dir_error(anchor, base)
        if message:
            errors.append(message)
            continue
        if base.is_dir():
            links.extend(path for path in base.rglob("*") if _owned_extra(claude, path))
    if errors:
        return errors
    for path in links:
        path.unlink()
    if rules_dest.is_file():
        rules_dest.unlink()
    for top in (*CLAUDE_EXTRAS, RULES_PATH.parent.name):
        _prune_empty(claude / top)
    return []


def _claude_extras(root: Path, anchor: Path, pin: Pin) -> list[tuple[Path, Path]]:
    return [
        (pstack_root(root) / rel, anchor / CLAUDE_HOME / rel)
        for rel, _digest in pin.files
        if rel.split("/", 1)[0] in CLAUDE_EXTRAS
    ]


def _extra_error(anchor: Path, dest: Path) -> list[str]:
    message = _symlink_skills_dir_error(anchor, dest.parent)
    if message:
        return [message]
    if (dest.exists() or dest.is_symlink()) and not _owned_extra(anchor / CLAUDE_HOME, dest):
        return [f"refusing to replace unowned path {dest}"]
    return []


def _owned_extra(claude: Path, path: Path) -> bool:
    if not path.is_symlink():
        return False
    rel = path.relative_to(claude).parts
    target = Path(os.readlink(path)).parts
    return target[-len(rel) - 2 :] == ("skills", "pstack", *rel)


def _rules_error(anchor: Path, dest: Path, rules: str, verb: str) -> list[str]:
    message = _symlink_skills_dir_error(anchor, dest.parent)
    if message:
        return [message]
    if not dest.exists() and not dest.is_symlink():
        return []
    if dest.is_symlink() or not dest.is_file() or _first_line(dest) != rules.partition("\n")[0]:
        return [f"refusing to {verb} unowned path {dest}"]
    return []


def _first_line(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readline().rstrip("\n")
    except (OSError, UnicodeError):
        return None


def _place_atomic(dest: Path, write: Callable[[Path], object]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = dest.with_name(f"{_STAGING_PREFIX}{os.getpid()}-{dest.name}.tmp")
    if staged.exists() or staged.is_symlink():
        staged.unlink()
    write(staged)
    os.replace(staged, dest)


def _prune_empty(top: Path) -> None:
    if top.is_symlink() or not top.is_dir():
        return
    for path in sorted(top.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()
    if not any(top.iterdir()):
        top.rmdir()


def unstable_checkout(root: Path) -> str | None:
    if (root / ".git").is_file():
        return f"{root} is a linked git worktree; install from a stable checkout"
    temp = Path(tempfile.gettempdir()).resolve()
    if root.resolve().is_relative_to(temp):
        return f"{root} is under the temp dir; install from a stable checkout"
    return None


def canary_prompt() -> str:
    return (
        "Read the factory-builder skill from your harness's skills folder: "
        ".claude/skills/factory-builder/SKILL.md in Claude Code, "
        ".agents/skills/factory-builder/SKILL.md in Codex.\n"
        "Name the skill you loaded: factory-builder.\n"
        "It routes to poteto-mode, installed beside it. "
        "Quote the whole first bold passage of the pinned poteto-mode playbook "
        "playbooks/orchestrate.md, exactly as written but without the ** markers, "
        "on one line.\n"
        "The owning bot starts this harness.\n"
        "Claude Bot writes started-by: claude-bot.\n"
        "Codex Bot writes started-by: codex-bot.\n"
        "Reply with these three lines:\n"
        "started-by: <bot>\n"
        "skill: factory-builder\n"
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
            for harness in CANARY_HARNESSES
        ]
        return 1, prompt, errors
    errors = _score_responses(responses, token)
    if errors:
        return 1, prompt, errors
    return 0, prompt, []


def _check_installed(root: Path, anchor: Path, mode: Mode) -> list[str]:
    skills = _skills(root)
    pin = _installed_hashes(load_pin(root), skills)
    errors: list[str] = []
    for name, src in skills:
        prefix = f"skills/{name}/"
        expected = {rel: digest for rel, digest in pin.items() if rel.startswith(prefix)}
        for dest in _destinations(anchor, name, mode):
            harness = _harness_for(dest, anchor, mode)
            errors.extend(_check_dest(anchor, src, dest, expected, prefix, harness, mode))
    if mode == "local":
        for src, dest in _claude_extras(root, anchor, load_pin(root)):
            if not dest.is_symlink() or dest.resolve() != src.resolve():
                errors.append(f"claude: did not load the pinned version ({dest} is not the pinned file)")
    return errors


def _check_dest(
    anchor: Path,
    src: Path,
    dest: Path,
    expected: Mapping[str, str],
    prefix: str,
    harness: str,
    mode: Mode,
) -> list[str]:
    if mode == "cloud":
        if _symlinked_component(anchor, dest):
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
    errors: list[str] = []
    for harness in CANARY_HARNESSES:
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
        owner = OWNERS[harness]
        checks = (
            ("started-by", owner, f"not started by {owner}"),
            ("skill", "factory-builder", "did not name factory-builder"),
            ("token", token, "token does not match the pin"),
        )
        for key, expected, wrong in checks:
            found = [line for line in lines if line.startswith(f"{key}:")]
            if len(found) > 1:
                errors.append(f"{harness}: did not load the pinned version (more than one {key}: line)")
                break
            if found != [f"{key}: {expected}"]:
                errors.append(f"{harness}: did not load the pinned version ({wrong})")
                break
    return errors


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
    names = {name for name, _path in found}
    extra = _skill_folders(factory_root(root), "skills/factory") + _skill_folders(
        kit_root(root), "skills/cursor-team-kit"
    )
    for name, path in extra:
        if name in names:
            raise ValueError(f"{path.parent.name}/{name} has the same name as another skill")
        names.add(name)
    return tuple(found) + extra


def _skill_folders(base: Path, label: str) -> tuple[tuple[str, Path], ...]:
    if base.is_symlink() or not base.is_dir():
        raise ValueError(f"{label} is missing")
    found: list[tuple[str, Path]] = []
    for path in sorted(base.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            raise ValueError(f"{label}: symlink is not allowed: {path.name}")
        if path.is_dir() and (path / "SKILL.md").is_file():
            found.append((path.name, path))
    if not found:
        raise ValueError(f"{label} has no skills")
    return tuple(found)


def _installed_hashes(pin: Pin, skills: tuple[tuple[str, Path], ...]) -> dict[str, str]:
    names = {name for name, _path in skills}
    hashes = {rel: digest for rel, digest in pin.files if rel.startswith("skills/")}
    for rel, digest in (*pin.factory, *pin.kit):
        if rel.split("/", 1)[0] in names:
            hashes[f"skills/{rel}"] = digest
    return hashes


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


def _place_owned(src: Path, dest: Path, mode: Mode, commit: str, pinned: Mapping[str, str]) -> None:
    skills_dir = dest.parent
    if skills_dir.is_symlink():
        raise OSError(f"refusing to follow symlinked skills dir {skills_dir}")
    skills_dir.mkdir(parents=True, exist_ok=True)
    if skills_dir.is_symlink() or not skills_dir.is_dir():
        raise OSError(f"refusing to follow symlinked skills dir {skills_dir}")
    stage_root = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=skills_dir))
    staged = stage_root / dest.name
    try:
        if mode == "local":
            staged.symlink_to(src.resolve(), target_is_directory=True)
        elif mode == "cloud":
            _copy_tree(src, staged)
            _write_marker(staged, commit)
        else:
            _exhaustive(mode)
        _swap_in(staged, dest, stage_root / "backup" / dest.name, pinned)
    finally:
        _discard(stage_root)


def _swap_in(staged: Path, dest: Path, backup: Path, pinned: Mapping[str, str]) -> None:
    if dest.exists() or dest.is_symlink():
        if not _installer_owns(dest, dest.name, pinned):
            raise OSError(f"refusing to replace unowned path {dest}")
    if not dest.exists() and not dest.is_symlink():
        os.rename(staged, dest)
        return
    if _is_non_dir(staged) and _is_non_dir(dest):
        os.replace(staged, dest)
        return
    backup.parent.mkdir()
    os.rename(dest, backup)
    try:
        os.rename(staged, dest)
    except BaseException:
        os.rename(backup, dest)
        raise


def _is_non_dir(path: Path) -> bool:
    return path.is_symlink() or not path.is_dir()


def _recover_leftovers(skills_dir: Path, pinned: Mapping[str, str]) -> None:
    if skills_dir.is_symlink() or not skills_dir.is_dir():
        return
    for entry in sorted(skills_dir.iterdir()):
        if entry.name.startswith(_STAGING_PREFIX) and not entry.is_symlink() and entry.is_dir():
            backups = entry / "backup"
            if not backups.is_symlink() and backups.is_dir():
                for backup in sorted(backups.iterdir()):
                    _restore_backup(backup, skills_dir / backup.name, pinned)
            if _owned_leftover(entry, pinned):
                _discard(entry)
        elif entry.name.startswith(_LEGACY_BACKUP_PREFIX):
            name = entry.name.removeprefix(_LEGACY_BACKUP_PREFIX).rsplit("-", 1)[0]
            dest = skills_dir / name
            if name and _installer_owns(entry, name, pinned):
                if dest.exists() or dest.is_symlink():
                    _discard(entry)
                else:
                    os.rename(entry, dest)


def _restore_backup(backup: Path, dest: Path, pinned: Mapping[str, str]) -> None:
    if dest.exists() or dest.is_symlink():
        return
    if _installer_owns(backup, backup.name, pinned):
        os.rename(backup, dest)


def _owned_leftover(stage_root: Path, pinned: Mapping[str, str]) -> bool:
    for entry in stage_root.iterdir():
        if entry.name == "backup" and not entry.is_symlink() and entry.is_dir():
            if not all(_installer_owns(item, item.name, pinned) for item in entry.iterdir()):
                return False
        elif not _installer_owns(entry, entry.name, pinned):
            return False
    return True


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
    return _scan(pstack_root(root), "skills/pstack")


def _scan_factory(root: Path) -> tuple[dict[str, str], list[str]]:
    return _scan(factory_root(root), "skills/factory")


def _scan_kit(root: Path) -> tuple[dict[str, str], list[str]]:
    return _scan(kit_root(root), "skills/cursor-team-kit")


def _scan(base: Path, label: str) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    if base.is_symlink():
        return {}, [f"{label}: symlink is not allowed"]
    if not base.is_dir():
        return {}, [f"{label} is missing"]
    hashes: dict[str, str] = {}
    for path in base.rglob("*"):
        rel = path.relative_to(base).as_posix()
        if path.is_symlink():
            errors.append(f"{label}: symlink is not allowed: {rel}")
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            errors.append(f"{label}: not a regular file: {rel}")
            continue
        hashes[rel] = _sha256(path)
    return hashes, errors


def _canary_token(root: Path) -> str:
    path = root / SKILL_ROOT / "poteto-mode" / "playbooks" / "orchestrate.md"
    if path.is_symlink() or not path.is_file():
        raise ValueError("pinned poteto-mode playbook is missing")
    text = path.read_text(encoding="utf-8")
    start = text.find("**")
    if start < 0:
        raise ValueError("pinned poteto-mode has no version token")
    end = text.find("**", start + 2)
    if end < 0 or "\n" in text[start + 2 : end]:
        raise ValueError("pinned poteto-mode first bold passage is not on one line")
    token = text[start + 2 : end].strip()
    if not token:
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


def _symlinked_component(anchor: Path, dest: Path) -> bool:
    current = anchor
    for part in dest.relative_to(anchor).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _unowned_error(dest: Path, name: str, pinned: Mapping[str, str]) -> str | None:
    if not dest.exists() and not dest.is_symlink():
        return None
    if _installer_owns(dest, name, pinned):
        return None
    return f"refusing to replace unowned path {dest}"


def _installer_owns(dest: Path, name: str, pinned: Mapping[str, str]) -> bool:
    if dest.is_symlink():
        return _symlink_into_source(dest, name)
    if dest.is_dir():
        return _marker_matches_contents(dest, name, pinned)
    return False


def _symlink_into_source(dest: Path, name: str) -> bool:
    try:
        resolved = dest.resolve(strict=True)
    except OSError:
        target = Path(os.readlink(dest))
        return target.name == name and _source_parent(target.parts)
    return resolved.name == name and resolved.is_dir() and _source_parent(resolved.parts)


def _source_parent(parts: tuple[str, ...]) -> bool:
    return parts[-4:-1] == _PSTACK_SKILLS or parts[-3:-1] in (_FACTORY_SKILLS, _KIT_SKILLS)


def _marker_matches_contents(dest: Path, name: str, pinned: Mapping[str, str]) -> bool:
    listed = _marker_files(dest / _MARKER)
    if listed is None:
        return False
    if not listed:
        prefix = f"skills/{name}/"
        listed = {rel.removeprefix(prefix): digest for rel, digest in pinned.items() if rel.startswith(prefix)}
    return bool(listed) and _content_hashes(dest) == listed


def _marker_files(path: Path) -> dict[str, str] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError):
        return None
    if len(lines) < 2 or lines[0] != f"installer: {_INSTALLER}":
        return None
    if not lines[1].startswith("commit: "):
        return None
    if _COMMIT.fullmatch(lines[1].removeprefix("commit: ")) is None:
        return None
    files: dict[str, str] = {}
    for line in lines[2:]:
        if not line.startswith("file: "):
            return None
        digest, _, rel = line.removeprefix("file: ").partition(" ")
        if _SHA256.fullmatch(digest) is None or not _safe_rel(rel):
            return None
        files[rel] = digest
    return files


def _content_hashes(dest: Path) -> dict[str, str] | None:
    hashes: dict[str, str] = {}
    for path in dest.rglob("*"):
        rel = path.relative_to(dest).as_posix()
        if path.is_symlink():
            return None
        if path.is_dir():
            continue
        if not path.is_file():
            return None
        if rel != _MARKER:
            hashes[rel] = _sha256(path)
    return hashes


def _write_marker(dest: Path, commit: str) -> None:
    hashes = _content_hashes(dest)
    if not hashes:
        raise OSError(f"staged copy has no regular files: {dest}")
    lines = [f"installer: {_INSTALLER}", f"commit: {commit}"]
    lines.extend(f"file: {digest} {rel}" for rel, digest in sorted(hashes.items()))
    (dest / _MARKER).write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        "# A pin bump updates version, commit, files, factory, and kit together.",
        f"version: {json.dumps(pin.version)}",
        f"commit: {pin.commit}",
        f"token: {json.dumps(pin.token)}",
        "files:",
    ]
    for rel, digest in pin.files:
        lines.append(f"  {rel}: {digest}")
    lines.append("factory:")
    for rel, digest in pin.factory:
        lines.append(f"  {rel}: {digest}")
    lines.append("kit:")
    for rel, digest in pin.kit:
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
