from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency, routing
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "159fdfbcc55b166f35d0fc8a141641a773e92ee2a5fae1224a6ae0d777336120",
    "efficiency.SCRIPT": "997f9c391299a200bb1580ccdfe68aa42a3911f30e2e0daba8c8b0d91abc6388",
    "routing.CSS": "60f7063494f043e67318e6a26deb9d1cc4c6044e1a83c72d651a163aa8079070",
    "routing.SCRIPT": "cfe73b60b32ff0a7ca7c2964e07e97c7a05794b3a136b4519e549f34f3e8c1c4",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "ebdc8cd691a5831ea96af4206e97b5761c9628e4420dbc30c6e5469c2443c32f",
    "seeded/chrome": "425286c58ed83cffc174dd0ad820954cefee741079893345fd2d83a055c2ebee",
    "hostile/main": "ebdc8cd691a5831ea96af4206e97b5761c9628e4420dbc30c6e5469c2443c32f",
    "hostile/chrome": "2e39aff37188fc706bfb14ea8b5eb8b3f8aff006f1b95bf4fcb6fe9ab4dacff6",
    "empty/main": "438ed8ba8e25822ebe9a2765c67a4df9ed3abced7a97d2041a31953a5a22a371",
    "empty/chrome": "20d8e3ed5aa84ad6552966eb8a60e6e1ac053dfd139d1a93cd86ef3a3960d4d1",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "efficiency.CSS": efficiency.CSS,
        "efficiency.SCRIPT": efficiency.SCRIPT,
        "routing.CSS": routing.CSS,
        "routing.SCRIPT": routing.SCRIPT,
        "TELEMETRY_CSS": TELEMETRY_CSS,
        "__TELEMETRY__": substitutions(None)["__TELEMETRY__"],
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
