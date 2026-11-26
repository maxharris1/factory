from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "381084a5f2be20182876d9794216aaa9cbc407e662e2db03d5ce202d9cb8d821",
    "tickets.SCRIPT_TICKETS": "0eef082ad3f056652424d9669dd99c2fe165fd175dfb3258c47df4cbe5bea5c7",
    "ticket_detail.CSS": "fd3512c5794ef74eea233951fa545a364dd61fc8eec9c8a62ca1c942401b1a29",
    "ticket_detail.SCRIPT": "b30bdab792b1bd5d38ce9cc4617c05ef12b634f2d6397c020555d58aef6e8f02",
    "__BOARD__": "611cc9d340cdc999b2086c8a76dad3a3170502fa3d7cfa41d439fa1f9eddf1b3",
    "__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "seeded/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "hostile/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "empty/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "seeded/main": "7f2c6b4708768fb3208c39c0dd3cf8389f8e36c38e92fb6f3abfcbab8a158efc",
    "seeded/chrome": "cd7086042f8e5518731a7a5b99851545560e733833294e757b71e884ce95decd",
    "hostile/main": "7f2c6b4708768fb3208c39c0dd3cf8389f8e36c38e92fb6f3abfcbab8a158efc",
    "hostile/chrome": "15fc2db8a52aa61ff266440c21023679a17cc9cdd9751e5119e87c79d3cc49ea",
    "empty/main": "e36d217e80efb19b357b57a0949ab9f065931c323ae9ab40c99774ec7aa34f38",
    "empty/chrome": "2af4ce0596a328fb71e4acddafb78c311cbc524c71dcbf335bb05dd7f903aa4f",
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
