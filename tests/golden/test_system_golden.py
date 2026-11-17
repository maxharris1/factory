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
    "seeded/main": "38970e3359032f5eb24da2b37b68305959e6bcde31e2b1202bd1f30bf9dae8f8",
    "seeded/chrome": "106da6fad7b2406d54acc8de849adb52cb29d5b3ab8412709149097e62511855",
    "hostile/main": "38970e3359032f5eb24da2b37b68305959e6bcde31e2b1202bd1f30bf9dae8f8",
    "hostile/chrome": "9cd584fb5ae1d2ce76af90a56973f6084a7e1f594f57fd71b1b5dfa2abd93870",
    "empty/main": "9824cf09c47364d0bdafb900bc774c061f6b49ad489179d70fe54c392291a992",
    "empty/chrome": "d1844de40ccfb01f354eabeea89b1c738f126f32810477c632b28d3ed7fc341f",
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
