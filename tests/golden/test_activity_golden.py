from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import activity
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "activity.CSS": "f5bd34c68cca48aa53326ec3abfe8932d1a039ab6f258a16974ce28c7a3b1281",
    "activity.SCRIPT": "1e090c4f073b55f40a2fe9e4c5921e703da14c52037fc5b39cfd908eba5876d9",
    "seeded/main": "480228e21a51d9a8c4230e0fc6ab204ed08bc4a65d018ecab2dcfd484362fbf0",
    "seeded/chrome": "e6a08e46c579b8a8bfffc9c0a1b212ff159aaee6b177491f0dc7a51afd6d5132",
    "hostile/main": "af269837ee4703e7d95879613f9db8d866dbd7d8e9eef0a9c63728d0588cce00",
    "hostile/chrome": "6a4ccd9cc85a50572ce9383f82ba5647044edafe835c6cc56c4ac3d804a824dc",
    "empty/main": "2fe240e00d2294ae940371dced8f397636d4c0c1386f0bbe6e213298513c7ae7",
    "empty/chrome": "c2e3c377473d1aba630612c47e5eca50c14b7e9c422cff685f2ecbd9637caa86",
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
