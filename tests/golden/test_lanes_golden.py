from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import ci, cockpit, counts, lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "7ab438e14d21019257e5ca705ce16c6da2058e2a0b11ecef4c98683a644d82c4",
    "lanes.SCRIPT": "557c45e15a2ec57dca452d98e23e264f28ae2480dcf01ff002a47e6ad789a535",
    "ci.CSS": "544cfa203943aaabe9f99f5e42e54dfed2fb9d9d4a505db18a8b125e2b12c883",
    "ci.SCRIPT": "b911e0070f5debca7d894695d5484929f5abc168b05e1665bc793351c463bc34",
    "cockpit.CSS": "b4a22253c8f8d79d52557f43efaf97b8d99fe8ec71eb5eb295234e5ee1d84f4c",
    "cockpit.SCRIPT": "a245bfe1f23fdcb617d61bf79fe4aae343d73fa875f33468e1735dd6ee60e904",
    "counts.CSS": "fc5e8cfaa290c726bc5d6a5fb5bff25dd9bbff75864feb867f4419cbabc0ca02",
    "counts.SCRIPT": "c65d30a7f29d994eb2324ccc339defe2ad8edc7da28db9f6d21b854f4af30c0a",
    "__LANES__": "3dd9188bae62459830f676d07c5cf72802acc80a888489e8f072dd6b5350d552",
    "seeded/main": "772139303fa08489da4933b14a7053046a6f30ad6aff0a016cb66dd514b31f7b",
    "seeded/chrome": "93ad3ca0eb36a1d294ab167bcb6b1242ae900fe0db81c0cefcdd119288ef6308",
    "hostile/main": "772139303fa08489da4933b14a7053046a6f30ad6aff0a016cb66dd514b31f7b",
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
