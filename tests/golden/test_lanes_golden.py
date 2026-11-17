from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "41f3dd42f6648bc41df723d2f105df2f9d1e6bb7682d8b324d51d16f7f00bdb8",
    "lanes.SCRIPT": "fa2040323c9be7deb2791612daa289d1a25f50a2fa80a96ddfe15c879d7605d8",
    "__LANES__": "a18c91d294d605cbae44a62bfa25dd20ca42e586d9fec1cc717375ebce439501",
    "seeded/main": "d6168546a4bb72b241e88fa43b36eef4ff205d61a8872f5e522cd586307b7591",
    "hostile/main": "d6168546a4bb72b241e88fa43b36eef4ff205d61a8872f5e522cd586307b7591",
    "empty/main": "12e3d99225de409cfd2a61a54148a5a92958027c5b0d3debff45b75a27c08e49",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "lanes.CSS": lanes.CSS,
        "lanes.SCRIPT": lanes.SCRIPT,
        "__LANES__": substitutions(None)["__LANES__"],
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
