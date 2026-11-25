from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import activity
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "activity.CSS": "f5bd34c68cca48aa53326ec3abfe8932d1a039ab6f258a16974ce28c7a3b1281",
    "activity.SCRIPT": "1e090c4f073b55f40a2fe9e4c5921e703da14c52037fc5b39cfd908eba5876d9",
    "seeded/main": "480228e21a51d9a8c4230e0fc6ab204ed08bc4a65d018ecab2dcfd484362fbf0",
    "seeded/chrome": "90da75c4f90fd8b19ae56dee5ca21daaa0e8654974ee5d5c648b6b5ded0a3a8a",
    "hostile/main": "af269837ee4703e7d95879613f9db8d866dbd7d8e9eef0a9c63728d0588cce00",
    "hostile/chrome": "85c6c3f76e777d3b2ea857fc6677ba44cc7c8ecba480a216b59f848cd7916075",
    "empty/main": "2fe240e00d2294ae940371dced8f397636d4c0c1386f0bbe6e213298513c7ae7",
    "empty/chrome": "e3bad72e1c1812075cf9e7c30e9c9aefd780c22a58c991cd2d040d6fa7dfd663",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "activity.CSS": activity.CSS,
        "activity.SCRIPT": activity.SCRIPT,
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
