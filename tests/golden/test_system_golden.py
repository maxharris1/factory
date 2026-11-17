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
    "seeded/main": "215428423f716fcf2a606dd05cd456fb7a59dac2d44d3f9049e0c8959d76f21e",
    "seeded/chrome": "7a21832711325df10167dd19940715ed95881c93ac06c038bcc1856b202f0f05",
    "hostile/main": "215428423f716fcf2a606dd05cd456fb7a59dac2d44d3f9049e0c8959d76f21e",
    "hostile/chrome": "7d21ea51f5a2b1a2659ef981b2c1e304e714dbea141eac7aba314e152600c51d",
    "empty/main": "eae8c7bcb9f625a2e311e345966929282c1df13607290e0aef351213380f6472",
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
