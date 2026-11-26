from __future__ import annotations

from pathlib import Path

from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "seeded/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "seeded/chrome": "8d4ba928cc741dedbbb010fd59d622250f0d7636676f626c6d2ef1ddf3712534",
    "hostile/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "hostile/chrome": "cbb3d7d6047ea956de65dda19e1367f05334cb9b8d0f7172b1cf6e53a852aa03",
    "empty/main": "4b2657cf26146c0c01158006ab6a5d27092ad026b038f056acf306c4b480069a",
    "empty/chrome": "e7e64523221b9f1917919d372ff7c619c8a2fd60b22314533216f6fd141bd688",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
