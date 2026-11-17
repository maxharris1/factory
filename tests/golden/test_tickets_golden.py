from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "20e435f7bc1dd966e3364fc84bd8f8a5c43ac84f609f2c098f2d60207bacc43e",
    "tickets.SCRIPT_TICKETS": "964a520883759cde2477cd1dffec9de0da04c9d8fde2cdcc38ffc3821766bbe4",
    "ticket_detail.CSS": "20e659a3057057d34df4bdf5b57e070d434f2a44ec0f2d3e34f9cf2fb91d4e41",
    "ticket_detail.SCRIPT": "676372d841300b64a56683375c888e3b810682a9a57e7e451b754f4b820160df",
    "__BOARD__": "18b21db47d9ddf72eff3caa915c12ee0cffde0834e62ca0477318d9629503780",
    "__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "hostile/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "empty/__FLOWMAP__": "3a9d4dba95bf7ab5d1381d15912ac2dfc0649cdae789858e64be64deb6ba893c",
    "seeded/main": "ec2bc504723a74264f51f25371698bb6096a48c0964b1553d35bb8e77aee9a5e",
    "seeded/chrome": "25243b2b23181bb500bb0ae75b458563a3776cbf60b71dd8a22b43fc11aa09d6",
    "hostile/main": "ec2bc504723a74264f51f25371698bb6096a48c0964b1553d35bb8e77aee9a5e",
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
