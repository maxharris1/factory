from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "20e435f7bc1dd966e3364fc84bd8f8a5c43ac84f609f2c098f2d60207bacc43e",
    "tickets.SCRIPT_TICKETS": "4fd6893d483cb777609c95493cef19750dd4bf8fb13c1db203fa3366fab2b755",
    "ticket_detail.CSS": "da37e77fbd8ab2d98c0a4bb0f507928b81a8d437688d9ef38f35a6517293d143",
    "ticket_detail.SCRIPT": "a6b429229374219c10e8244ae524ee57ca4562ed7898c80763c982fcc992715d",
    "__BOARD__": "01e5b284544994ee690831e02f718cd077d298cd2631a4df1441d6009a6a0277",
    "__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "hostile/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "empty/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/main": "5f49d3c897b1d7c52c9c9276aacd1076f5f63b2eef02e28c993c3ee300b11048",
    "seeded/chrome": "25243b2b23181bb500bb0ae75b458563a3776cbf60b71dd8a22b43fc11aa09d6",
    "hostile/main": "5f49d3c897b1d7c52c9c9276aacd1076f5f63b2eef02e28c993c3ee300b11048",
    "hostile/chrome": "6dd91374a98a4768353983eae7bf8a94e47b1cbd49b4c407ae20f48df672f03f",
    "empty/main": "370600786fd2ae62b641bd7805b85471c05aed8b9765ea3b83b52659feb5366b",
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
