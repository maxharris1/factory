from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import ci, cockpit, lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "edb935d198032eb97ddbc3f1c3a567615c9e9c5d92f59debc9af2389518dea94",
    "lanes.SCRIPT": "980b2880e4f16bd015928d8c4b66fe73fb8dd2d6495aa36406c74e64a23aff26",
    "ci.CSS": "544cfa203943aaabe9f99f5e42e54dfed2fb9d9d4a505db18a8b125e2b12c883",
    "ci.SCRIPT": "b911e0070f5debca7d894695d5484929f5abc168b05e1665bc793351c463bc34",
    "cockpit.CSS": "2bddd9fe81e8baf1d6f29272a6eba6ee66f5ebd00860f1fb7e9cdca1fce763e6",
    "cockpit.SCRIPT": "c2110e91918538773e559ae2846b4dc96844d9ded357a2374b8f46028f898e67",
    "__LANES__": "0a070c5626c19ae07dc2fd8adfb927643b397d9899d61f93f67b20ee6f76e617",
    "seeded/main": "bb842118ba687d913788c3f03d6a959aee29cdff79d661695f1db635dbf79736",
    "seeded/chrome": "aadbe4dfbea694de291073438eca2d8b35ecfd9fbd09e4249c76dff8e9c29bd0",
    "hostile/main": "bb842118ba687d913788c3f03d6a959aee29cdff79d661695f1db635dbf79736",
    "hostile/chrome": "6381d02d574e86bfc1d9eb1cb283877a3e3f07d5a7cb1d4a425b0883c5411ef4",
    "empty/main": "0430eee7a74cc525784e083c329b0a6dbfd65540dab0d05e13bbdc9862876745",
    "empty/chrome": "d8299bd08e7a667a0f7eef8243d7c3289688d19b6059608781e484db8564bb69",
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
