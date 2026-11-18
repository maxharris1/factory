from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "ce4eed82f3e1d4c1d1839c86272555cec9c592d9838d862e96d134a8ede4b900",
    "efficiency.SCRIPT": "92c7a86535f823023ac9db92787c94e819a92b5b84a06d66b9ee8e3e65e07e4d",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "25f0a842b84a4bdcc2bb96b8d6690129d8f83be794752722792e6968cea4af1d",
    "seeded/chrome": "b32a0b8c4ef59de66fb3443c12618828f4582b42d5764764a54d683143aceae9",
    "hostile/main": "25f0a842b84a4bdcc2bb96b8d6690129d8f83be794752722792e6968cea4af1d",
    "hostile/chrome": "514a40b58dd78ac318dcf00495187640e01f0499a3f1c27d96087649f2441e21",
    "empty/main": "1d7925930a63c3dfee60f517b44c37ab998e9e0bb7b005972c6e2f19ad4cfc33",
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
