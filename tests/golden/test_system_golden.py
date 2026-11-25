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
    "seeded/main": "156af28150ab8af8c0c18b18b378e14d7436b6be46fa87f7e43630c927b94c81",
    "seeded/chrome": "b7396acde38869e4965381d658121528da01c63da9ee612c4f10039a24897730",
    "hostile/main": "156af28150ab8af8c0c18b18b378e14d7436b6be46fa87f7e43630c927b94c81",
    "hostile/chrome": "65f8767ce1346fee8a8369703477f86dfd2c49a3f216abf757581e729f3947f0",
    "empty/main": "f2e250fa756639df7b11fe2b6dfb4c15056abbc5ac6b212be8a4abe9c83353aa",
    "empty/chrome": "6bb379ad20cd9fcff7e2fe2a8e2cc321e7a2b707a6fbb4dbae991349be72365c",
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
