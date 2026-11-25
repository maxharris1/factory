from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import ci, cockpit, lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "f4a39fb9ca6ddd3c65fa300cf3a99a1d1b0be53e7fc969997b9a0ead0e14ee07",
    "lanes.SCRIPT": "9079b352a17095b911bf5d2d6965433c8bfb32e6a9c63f5218697d04f947b864",
    "ci.CSS": "544cfa203943aaabe9f99f5e42e54dfed2fb9d9d4a505db18a8b125e2b12c883",
    "ci.SCRIPT": "b911e0070f5debca7d894695d5484929f5abc168b05e1665bc793351c463bc34",
    "cockpit.CSS": "2bddd9fe81e8baf1d6f29272a6eba6ee66f5ebd00860f1fb7e9cdca1fce763e6",
    "cockpit.SCRIPT": "9b7e55a9d89ab926bdc159278f1a325ddfeb62386525a78429ddb2edc77998f7",
    "__LANES__": "0a070c5626c19ae07dc2fd8adfb927643b397d9899d61f93f67b20ee6f76e617",
    "seeded/main": "bb842118ba687d913788c3f03d6a959aee29cdff79d661695f1db635dbf79736",
    "seeded/chrome": "9522b88ea36b14ce7c9ce4c9643e3ea92aeffbf26a7a6923d5aef01d95a1b69d",
    "hostile/main": "bb842118ba687d913788c3f03d6a959aee29cdff79d661695f1db635dbf79736",
    "hostile/chrome": "a356e44f87e1f0c9da99cd3328d7dd7747c1196b0922f3bb930dd7d3309a3341",
    "empty/main": "0430eee7a74cc525784e083c329b0a6dbfd65540dab0d05e13bbdc9862876745",
    "empty/chrome": "cf6c770af8638b93bb7d0a6c626e8d80263d8d3a51bf8f095b1f6f4154186a20",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "lanes.CSS": lanes.CSS,
        "lanes.SCRIPT": lanes.SCRIPT,
        "ci.CSS": ci.CSS,
        "ci.SCRIPT": ci.SCRIPT,
        "cockpit.CSS": cockpit.CSS,
        "cockpit.SCRIPT": cockpit.SCRIPT,
        "__LANES__": substitutions(None)["__LANES__"],
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
