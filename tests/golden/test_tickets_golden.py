from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "12f7d2d337fa207a7bb5dd6fb35e21812f7c9974a18e16d5d3a5db75bfa82681",
    "tickets.SCRIPT_TICKETS": "68821d5e1408f914bca7325105f3c76e383e47ca7da6dd67f76b52cea393ca1b",
    "ticket_detail.CSS": "91dc87f051e6af291713f3f281bfc9743ea917b7cfcd1904d69c620ce2adb90c",
    "ticket_detail.SCRIPT": "abe80d37c3f2be8ef5752d74f973fb3657a38423bbb606d0aeaaa40183b4d7f9",
    "__BOARD__": "18a1bde89c615029c0c982725eb644dfe4e133f8b67095e03d82460e2d90c255",
    "__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "hostile/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "empty/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/main": "9f9e8b8c34ebe24fb105cd32ae7383ea7e871e2b397bf6e7ca152023033af120",
    "seeded/chrome": "f7fbdb4b81985537d0b9b806d2a07da4b102fe1755f187c40e31a8066858318f",
    "hostile/main": "9f9e8b8c34ebe24fb105cd32ae7383ea7e871e2b397bf6e7ca152023033af120",
    "hostile/chrome": "9d4afdf83cd1a61d6c1dfe9d932176bc1f16b189b825af695a60942964822a81",
    "empty/main": "00590b9b12a8345707b133f14e0fbfb92cc79a6aabf4e4db9799f7051b87fb8d",
    "empty/chrome": "2e576ea7d76b1582b5aed1b9bc4e6792f74a8379f9d21f33ec84ddca545478a4",
}


def digests(root: Path) -> dict[str, str]:
    assets = {
        "tickets.CSS": tickets.CSS,
        "tickets.SCRIPT_TICKETS": tickets.SCRIPT_TICKETS,
        "ticket_detail.CSS": ticket_detail.CSS,
        "ticket_detail.SCRIPT": ticket_detail.SCRIPT,
        "__BOARD__": substitutions(None)["__BOARD__"],
        "__FLOWMAP__": substitutions(load_chart())["__FLOWMAP__"],
    }
    for case, snapshot in snapshots(root / "charts").items():
        assets[f"{case}/__FLOWMAP__"] = substitutions(snapshot.get("statechart"))["__FLOWMAP__"]
    return page_digests(NAME, assets, root / "pages")


def test_the_page_and_its_parts_are_byte_identical(tmp_path: Path) -> None:
    assert digests(tmp_path) == GOLDEN


if __name__ == "__main__":
    record(digests)
