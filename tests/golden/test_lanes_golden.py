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
    "counts.SCRIPT": "c65d30a7f29d994eb2324ccc339defe2ad8edc7da28db9f6d21b854f4af30c0a",
    "__LANES__": "0a070c5626c19ae07dc2fd8adfb927643b397d9899d61f93f67b20ee6f76e617",
    "seeded/main": "9826fe521ce780a39b1ff9f7d770c8edb463adb2bbdd7d19c08bec20ef33a8d8",
    "seeded/chrome": "f3e4dc388b191dc3315c5172f7e009449cf3ef04b1aed58408cb411801b84b1e",
    "hostile/main": "9826fe521ce780a39b1ff9f7d770c8edb463adb2bbdd7d19c08bec20ef33a8d8",
    "hostile/chrome": "1ed276e76b962443e7187ef5dbb2d2a7d2c56f4555bc769e360c388ce9137cdb",
    "empty/main": "a373ab34ec077b5d73a21a23118d1b06b8de40d73def9f7ce1e9680d3a9ebf52",
    "empty/chrome": "65af8a67750e034be56641d0d9d31e25e3bf7abb9964e2dc800885179beee081",
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
        "counts.SCRIPT": counts.SCRIPT,
        "__LANES__": substitutions(None)["__LANES__"],
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
