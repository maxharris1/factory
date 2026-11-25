from __future__ import annotations

from pathlib import Path

from lib.dashboard_flow import load_chart
from lib.dashboard_pages import ticket_detail, tickets
from tests.golden.pages import page_digests, page_name, record, snapshots, substitutions

NAME = page_name(__file__)

GOLDEN = {
    "tickets.CSS": "381084a5f2be20182876d9794216aaa9cbc407e662e2db03d5ce202d9cb8d821",
    "tickets.SCRIPT_TICKETS": "0eef082ad3f056652424d9669dd99c2fe165fd175dfb3258c47df4cbe5bea5c7",
    "ticket_detail.CSS": "983bc4533c83a77797187c1b508327834bc168b41487b07e988a326248c39197",
    "ticket_detail.SCRIPT": "ede4a5ff7fe0ced9e0160200caed0c5d6fdbd315d5586fce3f5124c05e0a885e",
    "__BOARD__": "611cc9d340cdc999b2086c8a76dad3a3170502fa3d7cfa41d439fa1f9eddf1b3",
    "__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "seeded/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "hostile/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "empty/__FLOWMAP__": "242c85c9a6bfbfca11bad8b93bcf0966842c8d743e216534233cefe3e04d630b",
    "seeded/main": "7f2c6b4708768fb3208c39c0dd3cf8389f8e36c38e92fb6f3abfcbab8a158efc",
    "seeded/chrome": "fe5f7ca3a20724d4a1aaa5f965f062a0f6169e212f7f6b85ba6c4ca918de8044",
    "hostile/main": "7f2c6b4708768fb3208c39c0dd3cf8389f8e36c38e92fb6f3abfcbab8a158efc",
    "hostile/chrome": "9a63ad2f08297092c5458a44e64d9ab8e4d8b947efef14ac26150da6edefabb8",
    "empty/main": "e36d217e80efb19b357b57a0949ab9f065931c323ae9ab40c99774ec7aa34f38",
    "empty/chrome": "358a7d40398d22514b7b83186fe2e5fa65b22f748604b59727a5d34c80461aa7",
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
