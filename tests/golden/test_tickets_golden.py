from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "381084a5f2be20182876d9794216aaa9cbc407e662e2db03d5ce202d9cb8d821",
    "tickets.SCRIPT_TICKETS": "6b6280667c5dab42cbce08fd4c8c1b21e3a94a63e87e34e8afd9cea96e1ead15",
    "ticket_detail.CSS": "98304f26e3acafe1d308c528c2f0b460f08c9c942477255b191b388e5b0db282",
    "ticket_detail.SCRIPT": "7a7aeef10a1c48fac6ce723bed7ea6c262de4a318a91424464bff27a87023ebb",
    "__BOARD__": "cf46429059149151ee84a5d841667bf567b6bc6e51532f4f8815a23addde9fdb",
    "__FLOWMAP__": "cf92e47672a73474f1d5c5fc67a2ac14fc381f2d5f4499c203fc8fa54a575f2c",
    "seeded/__FLOWMAP__": "cf92e47672a73474f1d5c5fc67a2ac14fc381f2d5f4499c203fc8fa54a575f2c",
    "hostile/__FLOWMAP__": "cf92e47672a73474f1d5c5fc67a2ac14fc381f2d5f4499c203fc8fa54a575f2c",
    "empty/__FLOWMAP__": "cf92e47672a73474f1d5c5fc67a2ac14fc381f2d5f4499c203fc8fa54a575f2c",
    "seeded/main": "7f2c6b4708768fb3208c39c0dd3cf8389f8e36c38e92fb6f3abfcbab8a158efc",
    "seeded/chrome": "f7fbdb4b81985537d0b9b806d2a07da4b102fe1755f187c40e31a8066858318f",
    "hostile/main": "7f2c6b4708768fb3208c39c0dd3cf8389f8e36c38e92fb6f3abfcbab8a158efc",
    "hostile/chrome": "9d4afdf83cd1a61d6c1dfe9d932176bc1f16b189b825af695a60942964822a81",
    "empty/main": "e36d217e80efb19b357b57a0949ab9f065931c323ae9ab40c99774ec7aa34f38",
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
