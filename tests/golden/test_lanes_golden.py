from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import ci, lanes
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "lanes.CSS": "7272263c6ae2becdddcd065bc2cb7633b9d51fcbb9c18003b516b1b9a4bae4d7",
    "lanes.SCRIPT": "11cd1c2abb6ce16770e7bffd9204e13069b4010a41ac8f1fb9e550355087e1f1",
    "ci.CSS": "09a02d0e81bd2e2849deb6c09ccc72598cb37754b0f7962d46a816c56fce08a3",
    "ci.SCRIPT": "494453a9ef7939dbbcb80bceed6c14fa82c7c81d29b6f44ce9d75f00bf984e9b",
    "__LANES__": "5b84435192910a2ae4df47f385717dd58485121b59cc158496e3a5e6109c25f6",
    "seeded/main": "eece028d2e9f0d0df70a13c2b9a324bdd36290c50acb9c3c5464c5f629fbeffe",
    "seeded/chrome": "4df25dbd89766a89f79509dbd61528ec43573f62d3bd858a6b7643c91a8eb54b",
    "hostile/main": "eece028d2e9f0d0df70a13c2b9a324bdd36290c50acb9c3c5464c5f629fbeffe",
    "hostile/chrome": "cd677df0067d82cc1e6031ed292eb58ea7f42df8d1d5f537173499417277a666",
    "empty/main": "6090485751d4a2057b4453e70eb0f95a6af2062336cc91023f3dd2e5fd14e4c3",
    "empty/chrome": "d23f23faebf48e37b2841bb639c678d998bee55e8e886b922b411cb29ef90c12",
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
