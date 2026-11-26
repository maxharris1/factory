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
    "seeded/chrome": "ccb9be0d9d4f40ee41934e726653a2993d591fcb4cd3f36f6a383e4b3ab25b63",
    "hostile/main": "249f8dc629486c79fccecb423ab7baf7f300b6f50771a3ce957b0539c07cacba",
    "hostile/chrome": "e4b93534cf5ff192300e63892ee6b28cd904de6c35171842c9d24a1ec760214a",
    "empty/main": "7bed3d5d139442b1f223e3b28d0fdbce13606d30bcef401f47fc4ed19328239d",
    "empty/chrome": "7518e72c6e3c2c9f11e64df1a93752a5cd36dd3d5f847f02e3cbc7b07a105a0e",
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
