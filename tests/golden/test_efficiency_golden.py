from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "ce4eed82f3e1d4c1d1839c86272555cec9c592d9838d862e96d134a8ede4b900",
    "efficiency.SCRIPT": "644764d703e4b078581435d62cde2e304427601235123d85b0173b00668833d1",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "d61e6037003a32080c9c795ef23a8bd963e45f5b782bb0939670a9c8f3a26e5a",
    "seeded/chrome": "b32a0b8c4ef59de66fb3443c12618828f4582b42d5764764a54d683143aceae9",
    "hostile/main": "d61e6037003a32080c9c795ef23a8bd963e45f5b782bb0939670a9c8f3a26e5a",
    "hostile/chrome": "514a40b58dd78ac318dcf00495187640e01f0499a3f1c27d96087649f2441e21",
    "empty/main": "11b7889ff1db55010f085577b4298d897132e798ef89c9f51561a622f8ef60ff",
    "empty/chrome": "5f809518044a7ff1f5e4a19a9a8e829477c13b27b444fa89a37c7b09af0fa2b7",
}


def digests(root: Path) -> dict[str, str]:
    return page_digests(NAME, {
        "efficiency.CSS": efficiency.CSS,
        "efficiency.SCRIPT": efficiency.SCRIPT,
        "TELEMETRY_CSS": TELEMETRY_CSS,
        "__TELEMETRY__": substitutions(None)["__TELEMETRY__"],
    }, root)


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
