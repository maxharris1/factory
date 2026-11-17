from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import ci, lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "637b6fc44546f982e3da558356cc3ea7396c3517211ef1a3d0da863db9b12033",
    "lanes.SCRIPT": "11cd1c2abb6ce16770e7bffd9204e13069b4010a41ac8f1fb9e550355087e1f1",
    "ci.CSS": "09a02d0e81bd2e2849deb6c09ccc72598cb37754b0f7962d46a816c56fce08a3",
    "ci.SCRIPT": "494453a9ef7939dbbcb80bceed6c14fa82c7c81d29b6f44ce9d75f00bf984e9b",
    "__LANES__": "5b84435192910a2ae4df47f385717dd58485121b59cc158496e3a5e6109c25f6",
    "seeded/main": "eece028d2e9f0d0df70a13c2b9a324bdd36290c50acb9c3c5464c5f629fbeffe",
    "seeded/chrome": "ac469d192b196070d55c009485a59051383052b9daaf5f935f8246bca96ebf63",
    "hostile/main": "eece028d2e9f0d0df70a13c2b9a324bdd36290c50acb9c3c5464c5f629fbeffe",
    "hostile/chrome": "990c2243f88d4766ab355f94374d60fbf1be3fd459df5560011b25ef9a989b4c",
    "empty/main": "6090485751d4a2057b4453e70eb0f95a6af2062336cc91023f3dd2e5fd14e4c3",
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
