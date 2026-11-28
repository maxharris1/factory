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
    "cockpit.CSS": "8c0e54d75213b13901c926f160dc16ffc52d1e4cb37d306220a304aa57999ea0",
    "cockpit.SCRIPT": "9c8d1505c690901cfa4fa53c63ff3fefc92f4c64739ff84c342faf8a47f93356",
    "counts.CSS": "fc5e8cfaa290c726bc5d6a5fb5bff25dd9bbff75864feb867f4419cbabc0ca02",
    "counts.SCRIPT": "c65d30a7f29d994eb2324ccc339defe2ad8edc7da28db9f6d21b854f4af30c0a",
    "__LANES__": "0a070c5626c19ae07dc2fd8adfb927643b397d9899d61f93f67b20ee6f76e617",
    "seeded/main": "e954b4f0f05eff3936a0da7c551f1f28089a670eee046b95b37cd15478aabd2a",
    "seeded/chrome": "93ad3ca0eb36a1d294ab167bcb6b1242ae900fe0db81c0cefcdd119288ef6308",
    "hostile/main": "e954b4f0f05eff3936a0da7c551f1f28089a670eee046b95b37cd15478aabd2a",
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
