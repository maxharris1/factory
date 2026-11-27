from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import ci, cockpit, counts, lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "513e109eb1dae8d6c1bd68e7a32578f47d83937212a83abe48ab9483208217c2",
    "lanes.SCRIPT": "980b2880e4f16bd015928d8c4b66fe73fb8dd2d6495aa36406c74e64a23aff26",
    "ci.CSS": "544cfa203943aaabe9f99f5e42e54dfed2fb9d9d4a505db18a8b125e2b12c883",
    "ci.SCRIPT": "b911e0070f5debca7d894695d5484929f5abc168b05e1665bc793351c463bc34",
    "cockpit.CSS": "2bddd9fe81e8baf1d6f29272a6eba6ee66f5ebd00860f1fb7e9cdca1fce763e6",
    "cockpit.SCRIPT": "c2110e91918538773e559ae2846b4dc96844d9ded357a2374b8f46028f898e67",
    "counts.CSS": "fc5e8cfaa290c726bc5d6a5fb5bff25dd9bbff75864feb867f4419cbabc0ca02",
    "__LANES__": "0a070c5626c19ae07dc2fd8adfb927643b397d9899d61f93f67b20ee6f76e617",
    "seeded/main": "c919b5672aba984e4ccdae8d54bc14135c221048ebdcac583e6bd84256412296",
    "seeded/chrome": "aadbe4dfbea694de291073438eca2d8b35ecfd9fbd09e4249c76dff8e9c29bd0",
    "hostile/main": "c919b5672aba984e4ccdae8d54bc14135c221048ebdcac583e6bd84256412296",
    "hostile/chrome": "6381d02d574e86bfc1d9eb1cb283877a3e3f07d5a7cb1d4a425b0883c5411ef4",
    "empty/main": "c313c3b58d21e31405c4fb82dd692d91f8e289cea0a61ea894ebc1c7f0350217",
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
        "counts.CSS": counts.CSS,
        "__LANES__": substitutions(None)["__LANES__"],
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
