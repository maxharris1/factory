from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "ce4eed82f3e1d4c1d1839c86272555cec9c592d9838d862e96d134a8ede4b900",
    "efficiency.SCRIPT": "bb252f4bbd2429cc1c092ac3ee356db4196d9262d98039f2b07204fea1aa13d3",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "25f0a842b84a4bdcc2bb96b8d6690129d8f83be794752722792e6968cea4af1d",
    "seeded/chrome": "7a3bcff265d9a1ca720fa41f0f7637468ae825f0ae4ff3c39a745b59fd8d93fc",
    "hostile/main": "25f0a842b84a4bdcc2bb96b8d6690129d8f83be794752722792e6968cea4af1d",
    "hostile/chrome": "90a3ea12157efa86e5fa07f39110c45c7bbeedf96a8853e62ff685491ae999f3",
    "empty/main": "1d7925930a63c3dfee60f517b44c37ab998e9e0bb7b005972c6e2f19ad4cfc33",
    "empty/chrome": "e29f9c314b03a8c630f24395b16e4af5d15a509e3d390dca018466426827f73b",
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
