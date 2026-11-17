from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "2432d51bbb001fc4a892b4c21146fe1904cb0717c8daffef5334b00f3abcc0b0",
    "tickets.SCRIPT_TICKETS": "9a9918029b172f3e28ec98a869830b47eb4fdb2875fbc6a187f03582148acee4",
    "ticket_detail.CSS": "ea570dcd13a59ac984ab271497d3cabc5239b25934b33127b9afe0b36e50f72a",
    "ticket_detail.SCRIPT": "e5948868a590455204f814dda41c8db58d3ee369f28bae08f82dcebde1614d98",
    "__BOARD__": "988b51ce050bf3873af75787819c57711326377eaed5a138918d033658a9ef31",
    "__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "hostile/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "empty/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/main": "7a4273e49da515ce673c298d8af078607b54e3fd5637ee67e4ca6f480fec98c8",
    "seeded/chrome": "f7fbdb4b81985537d0b9b806d2a07da4b102fe1755f187c40e31a8066858318f",
    "hostile/main": "7a4273e49da515ce673c298d8af078607b54e3fd5637ee67e4ca6f480fec98c8",
    "hostile/chrome": "9d4afdf83cd1a61d6c1dfe9d932176bc1f16b189b825af695a60942964822a81",
    "empty/main": "4131688da3fe5d52c2613ae4bee45372b9a7a3380b4675438669c8924436953a",
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
