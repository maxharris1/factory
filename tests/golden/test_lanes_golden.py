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
    "cockpit.SCRIPT": "c2110e91918538773e559ae2846b4dc96844d9ded357a2374b8f46028f898e67",
    "__LANES__": "0a070c5626c19ae07dc2fd8adfb927643b397d9899d61f93f67b20ee6f76e617",
    "seeded/main": "bb842118ba687d913788c3f03d6a959aee29cdff79d661695f1db635dbf79736",
    "seeded/chrome": "7754112969551732eac6190685750455b4bc996c39b7b9ed98b489cad8a0ab0f",
    "hostile/main": "bb842118ba687d913788c3f03d6a959aee29cdff79d661695f1db635dbf79736",
    "hostile/chrome": "3ec9de0566a75e9dde2cf666830a6ffda9d2bfd679b41bfdbf48632300f6ae88",
    "empty/main": "0430eee7a74cc525784e083c329b0a6dbfd65540dab0d05e13bbdc9862876745",
    "empty/chrome": "267ca8331580ee53274eb32b22ba72e6e1db254e0e6e9a52efaf76fb9efbb9d2",
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
