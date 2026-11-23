from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import ci, lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "f4a39fb9ca6ddd3c65fa300cf3a99a1d1b0be53e7fc969997b9a0ead0e14ee07",
    "lanes.SCRIPT": "9079b352a17095b911bf5d2d6965433c8bfb32e6a9c63f5218697d04f947b864",
    "ci.CSS": "544cfa203943aaabe9f99f5e42e54dfed2fb9d9d4a505db18a8b125e2b12c883",
    "ci.SCRIPT": "b911e0070f5debca7d894695d5484929f5abc168b05e1665bc793351c463bc34",
    "__LANES__": "0a070c5626c19ae07dc2fd8adfb927643b397d9899d61f93f67b20ee6f76e617",
    "seeded/main": "abe3aa634faae48b94196a9c3078f2f47ed827b85aefc18d4e49a032f18366e3",
    "seeded/chrome": "ac469d192b196070d55c009485a59051383052b9daaf5f935f8246bca96ebf63",
    "hostile/main": "abe3aa634faae48b94196a9c3078f2f47ed827b85aefc18d4e49a032f18366e3",
    "hostile/chrome": "990c2243f88d4766ab355f94374d60fbf1be3fd459df5560011b25ef9a989b4c",
    "empty/main": "44ef347cd5ad692e00988cca924af44859d416608870fc1b1160ab6a070d370b",
    "empty/chrome": "ba0b60e3bb52c1691fba5382f1b7db90dfc37d57ff99f2d85a08100ecde01130",
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
