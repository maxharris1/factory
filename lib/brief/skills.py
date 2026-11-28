"""The skill files a job reads: the repo's own, and the pinned router by absolute path.

A plan, build, fix or review job reads the factory router and the pstack files
it names, at absolute paths under the placed device's factory_root (the
deployed factory checkout, never the job's worktree). Each path must be listed
in skills/skills.lock. When a path is not pinned, the root is unusable, or the
harness has no adapter, resolve returns the reason and the brief fails closed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

from lib.machines_yml import BOT_TOKEN, workdir_problem
from lib.pstack_models import brief_lines
from lib.pstack_pin import FACTORY_DIR, KIT_DIR, PSTACK_DIR, ROOT, load_pin
from lib.ticket_type import TICKET_TYPES

_SKILL_PATH = re.compile(r"^\.claude/skills/[a-z0-9-]+/SKILL\.md$")
_UNREADABLE = (OSError, ValueError, yaml.YAMLError)
_SECTION_DIRS = {"files": PSTACK_DIR, "factory": FACTORY_DIR, "kit": KIT_DIR}


@dataclass(frozen=True)
class Pinned:
    section: str
    rel: str

    @property
    def path(self) -> str:
        base = _SECTION_DIRS[self.section]
        return f"{base.as_posix()}/{self.rel}"

    def under(self, root: str) -> str:
        return f"{root}/{self.path}"


def _pstack(rel: str) -> Pinned:
    return Pinned("files", rel)


def _factory(rel: str) -> Pinned:
    return Pinned("factory", rel)


def _kit(rel: str) -> Pinned:
    return Pinned("kit", rel)


AGENT_FILES = (_pstack("agents/comment-sicko.md"), _pstack("agents/poteto-agent.md"))
PLAYBOOKS = {name: _pstack(f"skills/poteto-mode/playbooks/{name}.md") for name in TICKET_TYPES}
ACTION_FILES = {
    "plan": (_pstack("skills/how/SKILL.md"), _pstack("skills/architect/SKILL.md")),
    "build": (_pstack("skills/tdd/SKILL.md"),),
    "fix": (_pstack("skills/tdd/SKILL.md"),),
}
REVIEWER_FILES = (
    _factory("factory-reviewer/SKILL.md"),
    _pstack("skills/poteto-mode/SKILL.md"),
    _pstack("skills/interrogate/SKILL.md"),
    _pstack("skills/blast-radius/SKILL.md"),
)
ADAPTERS = {
    "claude-bot": _pstack("docs/harness/claude-code.md"),
    "codex-bot": _factory("harness/codex.md"),
    "cursor-bot": _factory("harness/cursor.md"),
    "gemini-bot": _factory("harness/gemini.md"),
}
_READ_WITH = {
    "claude-bot": "Read each file with `Read`, not the Skill tool (`{adapter}`, Tools).",
    "codex-bot": "Read each file with the shell (`{adapter}`, Tools).",
    "cursor-bot": "Read each file with the file-read tool or shell (`{adapter}`, Tools).",
    "gemini-bot": "Read each file with `read_file` or the shell (`{adapter}`, Tools).",
}


BUILDER_ROUTER = _factory("factory-builder/SKILL.md")


def deployed_path(rel: str, factory_root: str | None) -> str | None:
    if factory_root is None or _root_problem(factory_root) is not None:
        return None
    return f"{factory_root}/{rel}"


def method_only_note(factory_root: str | None) -> str:
    path = deployed_path(BUILDER_ROUTER.path, factory_root) or f"{BUILDER_ROUTER.path} in the deployed factory checkout"
    return (
        f"Your router is `{path}`. It gives a merge-only or rebase job no playbook and no helpers, "
        "so this Method is the whole job."
    )


@dataclass(frozen=True)
class Router:
    role: str
    bot: str
    paths: tuple[str, ...]
    models: str


def builder_files(action: str, ticket_type: str | None) -> tuple[Pinned, ...]:
    return (
        BUILDER_ROUTER,
        _pstack("skills/poteto-mode/SKILL.md"),
        _pstack("skills/figure-it-out/SKILL.md"),
        PLAYBOOKS[ticket_type or "feature"],
        *ACTION_FILES.get(action, ()),
        _pstack("skills/poteto-mode/playbooks/opening-a-pr.md"),
        _kit("deslop/SKILL.md"),
        _pstack("skills/principle-prove-it-works/SKILL.md"),
        _pstack("skills/no-comments/SKILL.md"),
        *AGENT_FILES,
    )


def skill_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and _SKILL_PATH.fullmatch(item)]


def resolve(
    role: str,
    bot: str,
    factory_root: str | None,
    pin_root: Path = ROOT,
    *,
    action: str = "build",
    ticket_type: str | None = None,
) -> Router | str:
    """The router for role as bot runs it under factory_root, or why there is none."""
    if factory_root is None:
        return "the placed device has no factory_root"
    problem = _root_problem(factory_root)
    if problem is not None:
        return problem
    adapter = ADAPTERS.get(bot)
    if adapter is None:
        return f"{bot} has no pstack harness adapter"
    try:
        pin = load_pin(pin_root)
    except _UNREADABLE as exc:
        return f"skills/skills.lock cannot be read ({exc})"
    pinned = {"files": pin.hashes(), "factory": pin.factory_hashes(), "kit": pin.kit_hashes()}
    files = (*(builder_files(action, ticket_type) if role == "builder" else REVIEWER_FILES), adapter)
    missing = [item.under(factory_root) for item in files if item.rel not in pinned[item.section]]
    if missing:
        return "not in skills/skills.lock: " + ", ".join(f"`{path}`" for path in missing)
    try:
        models = brief_lines(bot.removesuffix("-bot"), pin_root)
    except _UNREADABLE as exc:
        return f"the helper model map cannot be read ({exc})"
    return Router(role, bot, tuple(item.under(factory_root) for item in files), models)


def _root_problem(factory_root: str) -> str | None:
    if "REPLACE_WITH_" in factory_root:
        return f"factory_root `{factory_root}` is a placeholder"
    problem = "names an unresolved {bot}" if BOT_TOKEN in factory_root else workdir_problem(factory_root)
    if problem is not None:
        return f"factory_root `{factory_root}` {problem}"
    return None


def section(router: Router) -> str:
    lines = [
        "## Skills",
        "Read the router (the first path) and the pstack files it names, at these absolute paths "
        "in the deployed factory checkout, never the job's working directory. "
        + _READ_WITH[router.bot].format(adapter=ADAPTERS[router.bot].path),
        "1. Before you read any of them, check that every path below exists (`test -f`). "
        "If one is missing, post a `questions` handoff naming it, then stop: do no work.",
        "2. Read the router in full, then the rest as it says.",
        "",
        *(f"- `{path}`" for path in router.paths),
        "",
        "Helper models, one `<role>: <value>` line per pstack role:",
        "",
        "```",
        router.models.rstrip("\n"),
        "```",
        "",
        "You may start helper agents as pstack recommends.",
    ]
    return "\n".join(lines)


_PINNED_LINE = re.compile(r"^- `(/[^`]+)`$")
_FENCE = re.compile(r"^(`{3,})")


def _unfenced(brief: str) -> Iterator[str]:
    fence = ""
    for line in brief.splitlines():
        if fence:
            if line.startswith(fence) and not line.rstrip().strip("`"):
                fence = ""
        elif match := _FENCE.match(line):
            fence = match.group(1)
        else:
            yield line


def pinned_paths(brief: str) -> tuple[str, ...] | None:
    """The paths `section` named in brief, in order; None when it names none."""
    lines = _unfenced(brief)
    for line in lines:
        if line == "## Skills":
            break
    else:
        return None
    paths: list[str] = []
    for line in lines:
        if line.startswith("## "):
            break
        if match := _PINNED_LINE.match(line):
            paths.append(match.group(1))
    return tuple(paths) or None
