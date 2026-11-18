from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import activity
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "activity.CSS": "f5bd34c68cca48aa53326ec3abfe8932d1a039ab6f258a16974ce28c7a3b1281",
    "activity.SCRIPT": "589a8416c971bef40fba38bc31fbf26460de0aef5d575dae9f4230a721b17fd9",
    "seeded/main": "480228e21a51d9a8c4230e0fc6ab204ed08bc4a65d018ecab2dcfd484362fbf0",
    "seeded/chrome": "3817c9e9c0c09ba9aebaaad4d706e2d6b505128d284ed0a2d170c543df48393e",
    "hostile/main": "af269837ee4703e7d95879613f9db8d866dbd7d8e9eef0a9c63728d0588cce00",
    "hostile/chrome": "ab516805b87929c9378b809bde754d3a2b659ccc0dcbf0384057fe37a7fc2a12",
    "empty/main": "2fe240e00d2294ae940371dced8f397636d4c0c1386f0bbe6e213298513c7ae7",
    "empty/chrome": "8794fb2dac259139ee1bcc62d1f9c776f4ea023a3e7bfaeef84a436d6c654ba2",
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
