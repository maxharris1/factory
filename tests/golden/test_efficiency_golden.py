from __future__ import annotations

from pathlib import Path

from lib.dashboard_pages import efficiency, routing
from lib.dashboard_telemetry import TELEMETRY_CSS
from tests.golden.pages import page_digests, page_name, record, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "efficiency.CSS": "8002a17cff56608f9ca457fa5e4304100695d4f079d4406b3aa2bfb8ff0725ba",
    "efficiency.SCRIPT": "9bdb3634fb6847e829216b3b844b2621fe7b8e3f20e4a568412c0f60ee87dbe5",
    "routing.CSS": "e6c7ee7099be006cebb80d2c965a9c60d3aa1d429ecf9a9af5dc9f797c39c137",
    "routing.SCRIPT": "a9d2c2f0bcda434daea5b4c0c186a0e21e6523307d9f19a6baf919a7e6bdcc21",
    "TELEMETRY_CSS": "649dcbbee7a9591b4dac5c5d49a14247192fb5baffe024921698d935b8437071",
    "__TELEMETRY__": "a36660b73d6a3ff62efe3db3e1e644deb2aa93f51ddf1021c50b9c150f96e114",
    "seeded/main": "3a0d6e6d925547b0312fcea7b587c43c1b9473e55b2d3824d3f41e956374dd6d",
    "seeded/chrome": "a5c1175cbda351543bf166c301723cbc475f8773091b323154c27ad3ab084eef",
    "hostile/main": "3a0d6e6d925547b0312fcea7b587c43c1b9473e55b2d3824d3f41e956374dd6d",
    "hostile/chrome": "7ef4c156a2146cb43d2f8fb19e6eba65e7517dcd89d86925f05c6182b2ffdd0c",
    "empty/main": "4aba45bb88d890f26241c0c09c2a18e58048b0322347b69e74662e26d2286bf3",
    "empty/chrome": "a08702330d8b0addc8938571383c714763f6f2e156926b9dbbbaac9cecdb08bd",
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
