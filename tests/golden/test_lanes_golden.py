from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import ci, lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "f4a39fb9ca6ddd3c65fa300cf3a99a1d1b0be53e7fc969997b9a0ead0e14ee07",
    "lanes.SCRIPT": "5984381d83ff268f0ab69e06a915c0f703e7ae1ab13a7130fcac25c3916a42e3",
    "ci.CSS": "038671d04a68b99eaf8d159b1ccd019292f1a894ff7929de008a13763bcde3b6",
    "ci.SCRIPT": "f8c11f914bd0f88fb2834a40f6e7dcaba5d0b59f9b28a99512709b9f91b90fcf",
    "__LANES__": "0a070c5626c19ae07dc2fd8adfb927643b397d9899d61f93f67b20ee6f76e617",
    "seeded/main": "abe3aa634faae48b94196a9c3078f2f47ed827b85aefc18d4e49a032f18366e3",
    "seeded/chrome": "4df25dbd89766a89f79509dbd61528ec43573f62d3bd858a6b7643c91a8eb54b",
    "hostile/main": "abe3aa634faae48b94196a9c3078f2f47ed827b85aefc18d4e49a032f18366e3",
    "hostile/chrome": "cd677df0067d82cc1e6031ed292eb58ea7f42df8d1d5f537173499417277a666",
    "empty/main": "44ef347cd5ad692e00988cca924af44859d416608870fc1b1160ab6a070d370b",
    "empty/chrome": "d23f23faebf48e37b2841bb639c678d998bee55e8e886b922b411cb29ef90c12",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "lanes.CSS": lanes.CSS,
        "lanes.SCRIPT": lanes.SCRIPT,
        "ci.CSS": ci.CSS,
        "ci.SCRIPT": ci.SCRIPT,
        "__LANES__": substitutions(None)["__LANES__"],
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
