"""The skill files a job reads: the repo's own, and the pinned router by absolute path.

A plan, build, fix or review job reads the factory router and the pstack files
it names, at absolute paths under the placed device's factory_root (the
deployed factory checkout, never the job's worktree). Each path must be listed
in skills/skills.lock. When a path is not pinned, the root is unusable, or the
harness has no adapter, resolve returns the reason and the brief fails closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from lib.machines_yml import BOT_TOKEN, workdir_problem
from lib.pstack_models import brief_lines
from lib.pstack_pin import FACTORY_DIR, PSTACK_DIR, ROOT, load_pin

_SKILL_PATH = re.compile(r"^\.claude/skills/[a-z0-9-]+/SKILL\.md$")
_UNREADABLE = (OSError, ValueError, yaml.YAMLError)


@dataclass(frozen=True)
class Pinned:
    section: str
    rel: str

    def under(self, root: str) -> str:
        base = PSTACK_DIR if self.section == "files" else FACTORY_DIR
        return f"{root}/{base.as_posix()}/{self.rel}"


def _pstack(rel: str) -> Pinned:
    return Pinned("files", rel)


def _factory(rel: str) -> Pinned:
    return Pinned("factory", rel)


AGENT_FILES = (_pstack("agents/comment-sicko.md"), _pstack("agents/poteto-agent.md"))
BUILDER_FILES = (
    _factory("factory-builder/SKILL.md"),
    _pstack("skills/poteto-mode/SKILL.md"),
    _pstack("skills/figure-it-out/SKILL.md"),
    _pstack("skills/poteto-mode/playbooks/feature.md"),
    _pstack("skills/poteto-mode/playbooks/opening-a-pr.md"),
    _pstack("skills/principle-prove-it-works/SKILL.md"),
    _pstack("skills/no-comments/SKILL.md"),
    *AGENT_FILES,
)
REVIEWER_FILES = (
    _factory("factory-reviewer/SKILL.md"),
    _pstack("skills/poteto-mode/SKILL.md"),
    _pstack("skills/interrogate/SKILL.md"),
    _pstack("skills/blast-radius/SKILL.md"),
)
ADAPTERS = {
    "claude-bot": _pstack("docs/harness/claude-code.md"),
    "codex-bot": _factory("harness/codex.md"),
}
_READ_WITH = {
    "claude-bot": "Read each file with `Read`: the Skill tool refuses most pstack skills "
    "(`docs/harness/claude-code.md`, Tools).",
    "codex-bot": "Read each file with the shell (`harness/codex.md`, Tools).",
}
_GENERAL_HELPER = {
    "claude-bot": '`subagent_type: "general-purpose"`',
    "codex-bot": '`agent_type: "default"`',
}


@dataclass(frozen=True)
class Router:
    role: str
    bot: str
    paths: tuple[str, ...]
    agents: tuple[str, ...]
    models: str


def skill_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and _SKILL_PATH.fullmatch(item)]


def resolve(role: str, bot: str, factory_root: str | None, pin_root: Path = ROOT) -> Router | str:
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
    pinned = {"files": pin.hashes(), "factory": pin.factory_hashes()}
    files = (*(BUILDER_FILES if role == "builder" else REVIEWER_FILES), adapter)
    missing = [item.under(factory_root) for item in files if item.rel not in pinned[item.section]]
    if missing:
        return "not in skills/skills.lock: " + ", ".join(f"`{path}`" for path in missing)
    try:
        models = brief_lines(bot.removesuffix("-bot"), pin_root)
    except _UNREADABLE as exc:
        return f"the helper model map cannot be read ({exc})"
    agents = tuple(item.under(factory_root) for item in AGENT_FILES) if role == "builder" else ()
    return Router(role, bot, tuple(item.under(factory_root) for item in files), agents, models)


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
        + _READ_WITH[router.bot],
        "1. Before you read any of them, check that every path below exists (`test -f`). "
        "If one is missing, post a `questions` handoff naming it, then stop: do no work.",
        "2. Read the router in full, then the rest as it says.",
        "",
        *(f"- `{path}`" for path in router.paths),
        "",
        "Helper models, one `<role>: <value>` line per pstack role. "
        "Where any other model config differs, these lines win:",
        "",
        "```",
        router.models.rstrip("\n"),
        "```",
        "",
    ]
    if router.role == "builder":
        lines.append("You may start helper agents as pstack recommends. They never post to GitHub or push.")
        lines.append(
            "If your harness does not know an agent name the router uses (`comment-sicko`, "
            f"`poteto-agent`), start a general helper ({_GENERAL_HELPER[router.bot]}) and "
            "tell it first to read and follow the agent file: "
            + ", ".join(f"`{path}`" for path in router.agents)
            + "."
        )
    else:
        lines.append(
            "You may start helper agents as pstack recommends. They do research only; you write the verdict."
        )
    return "\n".join(lines)


def blocked_task_lines(reason: str) -> list[str]:
    return [
        f"Do not start the work. This brief cannot name the pinned skill files it needs: {reason}.",
        "1. Post the `questions` handoff below. Name the problem above and ask Max to fix the "
        "factory checkout or the pin.",
        "2. Then stop: no branch, no commit, no push, no other handoff.",
    ]
