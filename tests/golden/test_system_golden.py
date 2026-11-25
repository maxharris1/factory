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
    "seeded/chrome": "36c0fce702fb3843b0850ebe1ce9b53d09cf18c1d1859314a00e237c3c3dbefe",
    "hostile/main": "156af28150ab8af8c0c18b18b378e14d7436b6be46fa87f7e43630c927b94c81",
    "hostile/chrome": "3b0fdfe73db0610fdcc2d71af5dc16bd4050cb7c861404d574b2bc645591c162",
    "empty/main": "f2e250fa756639df7b11fe2b6dfb4c15056abbc5ac6b212be8a4abe9c83353aa",
    "empty/chrome": "7ccfad9c442f949a77f177ce5b31a12a9d2961d344feb4b57635cd6ca7bafcdf",
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
