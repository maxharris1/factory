from __future__ import annotations

from pathlib import Path

from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "seeded/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "seeded/chrome": "a61ad7930fbaf297ec96acd5c0d6f31c259fb23a443c792497247fe78c2eda38",
    "hostile/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "hostile/chrome": "42f5835225db7211318588317e988769cc3e508b241875dc5736bc023f696987",
    "empty/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "empty/chrome": "07c891e5a7f7826a932fb54ecb2afdc739428d633025b1982282db225f24124b",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
