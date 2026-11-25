from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "8002a17cff56608f9ca457fa5e4304100695d4f079d4406b3aa2bfb8ff0725ba",
    "efficiency.SCRIPT": "9bdb3634fb6847e829216b3b844b2621fe7b8e3f20e4a568412c0f60ee87dbe5",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "2da26301d0862980ebee0d08b8250a2caa63fab7ab228fefa24c56ba7f0d9351",
    "seeded/chrome": "a5c1175cbda351543bf166c301723cbc475f8773091b323154c27ad3ab084eef",
    "hostile/main": "2da26301d0862980ebee0d08b8250a2caa63fab7ab228fefa24c56ba7f0d9351",
    "hostile/chrome": "7ef4c156a2146cb43d2f8fb19e6eba65e7517dcd89d86925f05c6182b2ffdd0c",
    "empty/main": "144ee7728324301e2b449e1bc72c99c3c312f88f88c9f9a08b55c6515ede68a1",
    "empty/chrome": "a08702330d8b0addc8938571383c714763f6f2e156926b9dbbbaac9cecdb08bd",
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
