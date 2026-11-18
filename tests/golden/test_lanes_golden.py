from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import ci, lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "f4a39fb9ca6ddd3c65fa300cf3a99a1d1b0be53e7fc969997b9a0ead0e14ee07",
    "lanes.SCRIPT": "0fee3d07703f910ef9076010a001fdae5ed7dad99455931fcc8291c1bf52c71f",
    "ci.CSS": "544cfa203943aaabe9f99f5e42e54dfed2fb9d9d4a505db18a8b125e2b12c883",
    "ci.SCRIPT": "94fe221acc3882029823674caee3d0dc683131ff004952d87977c0103ba9d91f",
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
