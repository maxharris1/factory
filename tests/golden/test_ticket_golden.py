from __future__ import annotations

from pathlib import Path

from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "seeded/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "seeded/chrome": "dfef52614aa93127e069c0e94af43975ddf38e0a26bce9ebb781701122391350",
    "hostile/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "hostile/chrome": "6edc09a6db4edd731b124c878a70d52c5d8da8d9e15eb0561c872fe2ef756c36",
    "empty/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "empty/chrome": "0e08a67d9235d1bb0d5ef3804523ce32bbb2142b50bdb516bfcd9818853ee6c6",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
