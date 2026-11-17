from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import system
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "system.CSS": "faf82fc7daee2d96cac2ac9c0e4c415f3481c106babcb428415dfa0db36d56dd",
    "system.SCRIPT_LOG": "3ceb2a2103e9acb749ccd6b817bcdca1f0dc47842f6fd2c6b14c047c03a0548b",
    "system.SCRIPT_HEALTH": "3d0bbddf094e7caaba8887cb17183942d34282cf925c7dfc34df2a83af847f8d",
    "system.SCRIPT_COMPONENTS": "bc8d14f54bb5d024ae9c09abb84630b467bc1382f63be67f63badadea71fb9fb",
    "seeded/main": "e76a44870101b70a3235a9f0647fa3796313f250a9864b482d10454664d34665",
    "seeded/chrome": "cded770f1db69ba4119e1550122465e1e029a1d7d82805ae942b6f20531218ea",
    "hostile/main": "e76a44870101b70a3235a9f0647fa3796313f250a9864b482d10454664d34665",
    "hostile/chrome": "4cc8c46b4f933b280fb2db70b109554c944d090c677a204294947a44edb1845a",
    "empty/main": "5c74eed81dbe9833db177f7b42ef7b96987d7a2281bbae9b4a9280730e20530b",
    "empty/chrome": "f5f301d6ad94422f9354337b1e98231833b5d946aabbb8ba736c86900075a224",
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
