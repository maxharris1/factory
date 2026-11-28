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
    "seeded/main": "404c4f8d746e4cfe933ce6a9cfb6c405b4778092b4cb63e71f4f15619165153c",
    "seeded/chrome": "77f522df0b5fa92f16d56490a6cb7425c8c014c50aaa78630b1cb6c299ecb550",
    "hostile/main": "404c4f8d746e4cfe933ce6a9cfb6c405b4778092b4cb63e71f4f15619165153c",
    "hostile/chrome": "6a43b4821e2eff1a55a69bb5a60ecb9343ef86a136ea2329f0d9c613612ee51d",
    "empty/main": "c3e0d9c25aabec27753d9d9a8bf1cf72b07c0b0150084cc01304f5a757bb49c8",
    "empty/chrome": "f561cb34eeb711043ec196e6fa8e93a7fb34f7592acef2af0218b3de0ba27c3f",
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
