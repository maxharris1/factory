from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency, routing
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "159fdfbcc55b166f35d0fc8a141641a773e92ee2a5fae1224a6ae0d777336120",
    "efficiency.SCRIPT": "997f9c391299a200bb1580ccdfe68aa42a3911f30e2e0daba8c8b0d91abc6388",
    "routing.CSS": "404e162887036520802fd1766af5e90e83fe1008707498f57e93577098d52b72",
    "routing.SCRIPT": "0d94c7aa81470c9c26b09e5a36aca0ba6290fcc0e280cf88b43b49a1a83f1c73",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "249f8dc629486c79fccecb423ab7baf7f300b6f50771a3ce957b0539c07cacba",
    "seeded/chrome": "425286c58ed83cffc174dd0ad820954cefee741079893345fd2d83a055c2ebee",
    "hostile/main": "249f8dc629486c79fccecb423ab7baf7f300b6f50771a3ce957b0539c07cacba",
    "hostile/chrome": "2e39aff37188fc706bfb14ea8b5eb8b3f8aff006f1b95bf4fcb6fe9ab4dacff6",
    "empty/main": "7bed3d5d139442b1f223e3b28d0fdbce13606d30bcef401f47fc4ed19328239d",
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
