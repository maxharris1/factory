from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import evals
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "evals.CSS": "8bb068c15598b0bd5e27dad2a3607b0aee89384c7a88efb31335bbfec89125d2",
    "evals.SCRIPT": "4fe2ca2923db01850de15af94b53bb3ea2ca7b450d1f89368af1896185611100",
    "seeded/main": "1c206fd1484ae4fb1072cedb13e3ada68a3cdbe6c9294b38074472c0d6b9d7b8",
    "seeded/chrome": "a9026fa8a53bcb7edc801ae765f8ef33a9a011f106121e1699e61c60e1ef4099",
    "hostile/main": "8fa23e066ac211b3f178bf6f5fdaebd2243ab8bc1ced90f2144aef04925c7adc",
    "hostile/chrome": "1f7c856f707e3e493aac537e9216a4ac47ffd491946bf64b0d1641e6e60ed30f",
    "empty/main": "5feb27640c6d506c005acc70c1e1fe857c601ee5744d611075772e1402245230",
    "empty/chrome": "c228726eb31fc5cac2504b0e08d7433e9385b6b9b2574b611502fd2de91fda50",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"evals.CSS": evals.CSS, "evals.SCRIPT": evals.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
