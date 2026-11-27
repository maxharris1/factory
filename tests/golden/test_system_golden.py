from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import system
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "system.CSS": "5c600cf00c031880c09fb2c6e8d5ab9b74afefd30693613ec2634e2e5a76e701",
    "system.SCRIPT_LOG": "3ceb2a2103e9acb749ccd6b817bcdca1f0dc47842f6fd2c6b14c047c03a0548b",
    "system.SCRIPT_HEALTH": "3d0bbddf094e7caaba8887cb17183942d34282cf925c7dfc34df2a83af847f8d",
    "system.SCRIPT_COMPONENTS": "bc8d14f54bb5d024ae9c09abb84630b467bc1382f63be67f63badadea71fb9fb",
    "seeded/main": "6ea032b69396c72bfb58283894ba77b45a7f45427e46824f700620604c35d4a1",
    "seeded/chrome": "37df77c9b2cd6a63c24d05843a6ef8bd37b8abae01dc20e93857bfe1a47562fd",
    "hostile/main": "6ea032b69396c72bfb58283894ba77b45a7f45427e46824f700620604c35d4a1",
    "hostile/chrome": "5ec5a6ceb673e1d05bba85a18d3ccaa472ea28cdd52baa0e9a5a31cafbca77e7",
    "empty/main": "54cd978bd93448e0fcc969097aae53e66e4d8f40e87e3ec4d3b957ce94fa06e4",
    "empty/chrome": "3281ba5de3e718303ce29d7e0d69eeb851d7805dd1809d663084155f937cd6ef",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "system.CSS": system.CSS,
        "system.SCRIPT_LOG": system.SCRIPT_LOG,
        "system.SCRIPT_HEALTH": system.SCRIPT_HEALTH,
        "system.SCRIPT_COMPONENTS": system.SCRIPT_COMPONENTS,
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
