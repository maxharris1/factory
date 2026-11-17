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
    "seeded/chrome": "87aa45406cfb21e3a8b55b80a8449a67fdc8cb50931240d8c583eac2d4338dd6",
    "hostile/main": "0323350ce7969e3893b0576f89975b95a9ce4b0e141bd3d7da84680a00d03047",
    "hostile/chrome": "f998539f4f098ea9855764ebdfb8d8d1b54a4cc426ca421f260ba7455b96f41b",
    "empty/main": "936c98d9516106b36b4b0f2c514cc62e882adaac8a3e8ced17af8c72a490c76a",
    "empty/chrome": "154d0c1de17b6110f3401c6a987db977e8f152fb96902d5c94308e9ecfd4c03a",
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
