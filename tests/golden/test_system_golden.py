from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import system
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "system.CSS": "909f100d79b9a28d946274d2a8809e53d3e8303aa18a80623559b14a945b7041",
    "system.SCRIPT_LOG": "3ceb2a2103e9acb749ccd6b817bcdca1f0dc47842f6fd2c6b14c047c03a0548b",
    "system.SCRIPT_HEALTH": "3d0bbddf094e7caaba8887cb17183942d34282cf925c7dfc34df2a83af847f8d",
    "system.SCRIPT_COMPONENTS": "2f6719537b3cc54e8ebd947dff426d42816c6539aca81fd951354aa827d38e18",
    "seeded/main": "0323350ce7969e3893b0576f89975b95a9ce4b0e141bd3d7da84680a00d03047",
    "seeded/chrome": "fd5c5866e624ce5c1aa81103e6bb94375716552d1cb1d29ad36b49223da2a81d",
    "hostile/main": "0323350ce7969e3893b0576f89975b95a9ce4b0e141bd3d7da84680a00d03047",
    "hostile/chrome": "5440924e9cb09309da7253b001bbfa52f2e9dd1be602b0a3ed5db4068e14542c",
    "empty/main": "936c98d9516106b36b4b0f2c514cc62e882adaac8a3e8ced17af8c72a490c76a",
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
