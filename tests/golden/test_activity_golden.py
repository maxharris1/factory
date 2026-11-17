from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import activity
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "activity.CSS": "3a69efa664921ac94bc562b6dce0674409ed8285aed1cedfa01dd7d6ee774462",
    "activity.SCRIPT": "9a5bf7fd1adce89751d5a4bee3a9665acbe6f2b9de1062e7c66fc423d288c6e1",
    "seeded/main": "b81d28ec195a1f0ee76a2fdca71e7bc46b433499773741df41c03735140d5036",
    "seeded/chrome": "df5260d3c44e18b2faeabc80086acab78beb4fb5bf199d1dd0c44f5e3b2fa8b5",
    "hostile/main": "baeb0eb03c5bf8dc144494941cae187008c06d2db87c48b83714bd86e0be7229",
    "hostile/chrome": "13f9c25334b997dc939fc5b3d958c882848a23ad9aa4732229dd24eba96a8b9e",
    "empty/main": "029f149dfdae7ecbce89013737a96cfc215b095c1ee28cd309d9ece0cab74655",
    "empty/chrome": "4da2d0bf5812c623a819ffc8a1d860a597c6b242473a3deee3c57068275d7591",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "activity.CSS": activity.CSS,
        "activity.SCRIPT": activity.SCRIPT,
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
