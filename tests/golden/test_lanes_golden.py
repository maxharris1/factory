from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import ci, cockpit, counts, lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "cac492d6fbe3aed219ac03b73f808fd28567307584784f6a61d20cf1b263f643",
    "lanes.SCRIPT": "980b2880e4f16bd015928d8c4b66fe73fb8dd2d6495aa36406c74e64a23aff26",
    "ci.CSS": "544cfa203943aaabe9f99f5e42e54dfed2fb9d9d4a505db18a8b125e2b12c883",
    "ci.SCRIPT": "b911e0070f5debca7d894695d5484929f5abc168b05e1665bc793351c463bc34",
    "cockpit.CSS": "2bddd9fe81e8baf1d6f29272a6eba6ee66f5ebd00860f1fb7e9cdca1fce763e6",
    "cockpit.SCRIPT": "c2110e91918538773e559ae2846b4dc96844d9ded357a2374b8f46028f898e67",
    "counts.CSS": "fc5e8cfaa290c726bc5d6a5fb5bff25dd9bbff75864feb867f4419cbabc0ca02",
    "counts.SCRIPT": "c65d30a7f29d994eb2324ccc339defe2ad8edc7da28db9f6d21b854f4af30c0a",
    "__LANES__": "0a070c5626c19ae07dc2fd8adfb927643b397d9899d61f93f67b20ee6f76e617",
    "seeded/main": "620124376ea8b19033dbaf5216712da57e2035f00c01132ae1ca4a9a526885f0",
    "seeded/chrome": "93ad3ca0eb36a1d294ab167bcb6b1242ae900fe0db81c0cefcdd119288ef6308",
    "hostile/main": "620124376ea8b19033dbaf5216712da57e2035f00c01132ae1ca4a9a526885f0",
    "hostile/chrome": "794be4754f2624a81b9d295c9aaedfe234da12e1633e2a44a8fed77560b1a3fa",
    "empty/main": "0a5da8f27f8d251de6e625ad1456e7aef3bb7f4f9b329da22b6a56c17d158dc8",
    "empty/chrome": "2daa26c130e43cf3716873a27dc86223f95e191cb27a350787dc68a93607f00e",
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
