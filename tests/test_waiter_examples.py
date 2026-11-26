"""Every shipped waiter example loads the way `orch wait --autostart` loads a copy of it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lib import waiter
from lib.load import load_yaml
from lib.machines_yml import BOT_USERS
from lib.pstack_pin import ROOT

EXAMPLES = sorted((ROOT / "config").glob("waiter*.example.yml"))
_NAMED = re.compile(r"waiter-(?P<bot>[a-z0-9-]+)\.example\.yml")


def example_bot(path: Path) -> str:
    """The bot an example is for: its file name's, else the one it names (the generic example)."""
    named = _NAMED.fullmatch(path.name)
    if named is not None:
        return named["bot"]
    data = load_yaml(path.read_text(encoding="utf-8"))
    return data["bot"]


def installed_copy(tmp_path: Path, example: Path, bot: str) -> Path:
    """The example copied to waiter-<bot>.yml, mode 600, as its header says to install it."""
    folder = tmp_path / "factory"
    folder.mkdir(mode=0o700)
    path = folder / f"waiter-{bot}.yml"
    path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_the_examples_include_every_per_bot_one() -> None:
    names = {path.name for path in EXAMPLES}
    assert {"waiter.example.yml", "waiter-gemini-bot.example.yml"} <= names


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda path: path.name)
def test_a_shipped_waiter_example_loads_for_its_bot(tmp_path: Path, example: Path) -> None:
    bot = example_bot(example)
    assert bot in BOT_USERS.values()
    loaded = waiter.load_harness_command(installed_copy(tmp_path, example, bot), bot)
    assert loaded.bot == bot
    assert loaded.command and not loaded.command[0].startswith("-")


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda path: path.name)
def test_a_shipped_waiter_example_names_the_bot_it_is_installed_for(example: Path) -> None:
    data = load_yaml(example.read_text(encoding="utf-8"))
    assert data.get("bot") == example_bot(example)
