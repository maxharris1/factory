from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import evals
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "evals.CSS": "8bb068c15598b0bd5e27dad2a3607b0aee89384c7a88efb31335bbfec89125d2",
    "evals.SCRIPT": "4fe2ca2923db01850de15af94b53bb3ea2ca7b450d1f89368af1896185611100",
    "seeded/main": "1c206fd1484ae4fb1072cedb13e3ada68a3cdbe6c9294b38074472c0d6b9d7b8",
    "seeded/chrome": "c0e271f5e4da051eb1206532cced8b9b172e9d47375977d65568901229df4372",
    "hostile/main": "8fa23e066ac211b3f178bf6f5fdaebd2243ab8bc1ced90f2144aef04925c7adc",
    "hostile/chrome": "6af4b3225385530a82e09559c4151c982d772f29f904858a20a5724a89aaa29e",
    "empty/main": "5feb27640c6d506c005acc70c1e1fe857c601ee5744d611075772e1402245230",
    "empty/chrome": "3fdc41bd799d807a570390917e0df8fca86c115b1ed04b80f2819dbec0560ec9",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"evals.CSS": evals.CSS, "evals.SCRIPT": evals.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
