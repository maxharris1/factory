from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "9260e46c3cf0946081db69861dc224cd8407a0c4821dcd49ab3e61817b703501",
    "efficiency.SCRIPT": "272f7db3aa464f0aced1b40d87b27683616988c6d9bbd7cb538756a5f057596a",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "2da26301d0862980ebee0d08b8250a2caa63fab7ab228fefa24c56ba7f0d9351",
    "seeded/chrome": "b32a0b8c4ef59de66fb3443c12618828f4582b42d5764764a54d683143aceae9",
    "hostile/main": "2da26301d0862980ebee0d08b8250a2caa63fab7ab228fefa24c56ba7f0d9351",
    "hostile/chrome": "514a40b58dd78ac318dcf00495187640e01f0499a3f1c27d96087649f2441e21",
    "empty/main": "144ee7728324301e2b449e1bc72c99c3c312f88f88c9f9a08b55c6515ede68a1",
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
