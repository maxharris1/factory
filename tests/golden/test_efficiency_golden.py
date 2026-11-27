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
    "seeded/chrome": "b190f2c6e6f863942520a896dc431239c9359b4ba346ba2d723eebb25d6741b3",
    "hostile/main": "402485b2fa5ff221f3e5a41f51649280b0149a53eeb0548d5d24012aa62ec468",
    "hostile/chrome": "7aceffd2548b12b99a7e9b3d81da64e3380be6165d8169bd511842a067ae750e",
    "empty/main": "a94aded36f9616fd5e9aa2eb02b5c79722e3fe5bd0281479c2386d2f171794ef",
    "empty/chrome": "c98452a1fb240ec7c4dfea4d769e9dec40374c8f88e64343847f5f0d8179e068",
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
