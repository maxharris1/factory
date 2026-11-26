from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import evals
from tests.golden.pages import page_digests, page_name, record

NAME = page_name(__file__)

GOLDEN = {
    "evals.CSS": "8bb068c15598b0bd5e27dad2a3607b0aee89384c7a88efb31335bbfec89125d2",
    "evals.SCRIPT": "4fe2ca2923db01850de15af94b53bb3ea2ca7b450d1f89368af1896185611100",
    "seeded/main": "1c206fd1484ae4fb1072cedb13e3ada68a3cdbe6c9294b38074472c0d6b9d7b8",
    "seeded/chrome": "0d26cb58e2633041d70ece5f559b6ac636685e4ad115155fc27f1dad647d1899",
    "hostile/main": "8fa23e066ac211b3f178bf6f5fdaebd2243ab8bc1ced90f2144aef04925c7adc",
    "hostile/chrome": "17a75b03a320362742e80f6db1eb57c312af2505fd4ad6b2f1aff12fd4738543",
    "empty/main": "5feb27640c6d506c005acc70c1e1fe857c601ee5744d611075772e1402245230",
    "empty/chrome": "900d5f149f2e228f14da5a93a5d9880af0a67e73a1cd5b77c5fdb51e05e585d9",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {"evals.CSS": evals.CSS, "evals.SCRIPT": evals.SCRIPT}, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
