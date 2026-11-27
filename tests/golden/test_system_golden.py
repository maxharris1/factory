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
    "seeded/main": "b9c87acecea95ed0a63eac650c666700f5f6e7d244a3fb15f6093a4bcbefea2f",
    "seeded/chrome": "546179ec09bf59b51febf140c73ff28c8286332e9ff2f20caf3601f82241e713",
    "hostile/main": "b9c87acecea95ed0a63eac650c666700f5f6e7d244a3fb15f6093a4bcbefea2f",
    "hostile/chrome": "3f7f84f58fb6db83308036829426e4d44836f56fcabdb78023e9f354bc8652eb",
    "empty/main": "046c24f489a398f862663d4d92e59c8f8e0218b303a2005c06be86a207993afd",
    "empty/chrome": "fbbd1bd6249b0578fe273f993997c0463c8918108470f38d6809140e7c5b0d45",
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
