from __future__ import annotations

from pathlib import Path

from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "seeded/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "seeded/chrome": "d77c397c9ec828b778689764c5085f3c34bc6a4658cafd56b0bffa816aa8c7ed",
    "hostile/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "hostile/chrome": "484f96bc43778eb1d62dc57412945267d7b812a2330b29f97daee0990de299d8",
    "empty/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "empty/chrome": "616a94811b34bdd52af34e3d7d5b560135c698203e63e66be9474f9460908162",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
