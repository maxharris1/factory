from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import activity
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "activity.CSS": "ccf8837b0d3facf6606df468a2839c4f18ae9ef83424851d6e63d309c442a8f0",
    "activity.SCRIPT": "244a86a3077d6932579339f30fe3ebf5e4708ea5bd099bc46be5b881bbdbc344",
    "seeded/main": "4a4b7c2357740c4452dde36d154880554215050381f595b39869cc925c1fffd6",
    "seeded/chrome": "df5260d3c44e18b2faeabc80086acab78beb4fb5bf199d1dd0c44f5e3b2fa8b5",
    "hostile/main": "e03ff0405f60d55fe410d39165575d7f2a8ece2be8317fa2b716f1495edbc483",
    "hostile/chrome": "13f9c25334b997dc939fc5b3d958c882848a23ad9aa4732229dd24eba96a8b9e",
    "empty/main": "029f149dfdae7ecbce89013737a96cfc215b095c1ee28cd309d9ece0cab74655",
    "empty/chrome": "4da2d0bf5812c623a819ffc8a1d860a597c6b242473a3deee3c57068275d7591",
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
