from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import evals
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "evals.CSS": "8bb068c15598b0bd5e27dad2a3607b0aee89384c7a88efb31335bbfec89125d2",
    "evals.SCRIPT": "615199160361d7bd249232786f4446137a652171d17ecbb657f79a94c9070059",
    "seeded/main": "1c206fd1484ae4fb1072cedb13e3ada68a3cdbe6c9294b38074472c0d6b9d7b8",
    "seeded/chrome": "f88caeb074d48f715baeab3d03532bebdbfb2b8b0c61b891dbd374fea5752cb6",
    "hostile/main": "8fa23e066ac211b3f178bf6f5fdaebd2243ab8bc1ced90f2144aef04925c7adc",
    "hostile/chrome": "f5baac1c66da884c9fd8f8db294365abf5fd6af9623778dfd44ff040cd95528b",
    "empty/main": "5feb27640c6d506c005acc70c1e1fe857c601ee5744d611075772e1402245230",
    "empty/chrome": "27164a0866186f644be0cf9c2e058aa1289ea6fb6a7b67c66a66a4199f770178",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"evals.CSS": evals.CSS, "evals.SCRIPT": evals.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
