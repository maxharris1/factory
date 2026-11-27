from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency, routing
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "159fdfbcc55b166f35d0fc8a141641a773e92ee2a5fae1224a6ae0d777336120",
    "efficiency.SCRIPT": "997f9c391299a200bb1580ccdfe68aa42a3911f30e2e0daba8c8b0d91abc6388",
    "routing.CSS": "e6c7ee7099be006cebb80d2c965a9c60d3aa1d429ecf9a9af5dc9f797c39c137",
    "routing.SCRIPT": "a9d2c2f0bcda434daea5b4c0c186a0e21e6523307d9f19a6baf919a7e6bdcc21",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "b931b153c7772e347b087d49b3d83c458cdf6d22f7fcd7014dac02d41e2d4256",
    "seeded/chrome": "425286c58ed83cffc174dd0ad820954cefee741079893345fd2d83a055c2ebee",
    "hostile/main": "402485b2fa5ff221f3e5a41f51649280b0149a53eeb0548d5d24012aa62ec468",
    "hostile/chrome": "2e39aff37188fc706bfb14ea8b5eb8b3f8aff006f1b95bf4fcb6fe9ab4dacff6",
    "empty/main": "a94aded36f9616fd5e9aa2eb02b5c79722e3fe5bd0281479c2386d2f171794ef",
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
