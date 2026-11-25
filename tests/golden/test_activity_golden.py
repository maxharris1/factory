from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import activity
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "activity.CSS": "f5bd34c68cca48aa53326ec3abfe8932d1a039ab6f258a16974ce28c7a3b1281",
    "activity.SCRIPT": "bb443a2008b689625cd301a36bd5c77b5e10f078afbd36cd17b1a629a3f858ba",
    "seeded/main": "480228e21a51d9a8c4230e0fc6ab204ed08bc4a65d018ecab2dcfd484362fbf0",
    "seeded/chrome": "0e85af44b8c2bc53e7ae7a226e33c1402920b4c489ea967f66e2681b3279d7b4",
    "hostile/main": "af269837ee4703e7d95879613f9db8d866dbd7d8e9eef0a9c63728d0588cce00",
    "hostile/chrome": "3ed39fc79701a4970f2411523711242e7a4fb0f2b8193483a4fe851630030e7c",
    "empty/main": "2fe240e00d2294ae940371dced8f397636d4c0c1386f0bbe6e213298513c7ae7",
    "empty/chrome": "61c34f5904b8f870ab97fea7ca64003a6e2154660d648aadf26c6d72c5de12cd",
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
