from __future__ import annotations

from pathlib import Path

from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "seeded/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "seeded/chrome": "ef3462dca429a13165eb1c3f98fed18bc3638b100fa06913a2e99384db278195",
    "hostile/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "hostile/chrome": "072d99efb8ef289fe7ae15bd3cec6958f6b0d1413b8f18f27bf3089b937a0314",
    "empty/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "empty/chrome": "c7397800d93e6353e549a56d2ed4b3b7bb3df468993f524f4bfaede5c0ba420c",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
