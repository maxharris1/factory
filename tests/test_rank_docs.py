import re
from pathlib import Path

from lib.queue import FINISHING_ACTIONS

ROOT = Path(__file__).resolve().parents[1]


def _device_pool_claim_sentence() -> str:
    spec = (ROOT / "docs/SPEC.md").read_text(encoding="utf-8")
    match = re.search(r"The claim takes the queued job for that harness[^.]*(?:\.[^ ][^.]*)*\.", spec)
    assert match is not None
    return match.group(0)


def test_spec_device_pool_claim_order_names_the_full_rank():
    sentence = _device_pool_claim_sentence()
    assert sentence.index("effective tier") < sentence.index("finishing") < sentence.index("urgent")
    for action in FINISHING_ACTIONS:
        assert f"`{action}`" in sentence


def test_rank_docs_cover_every_rule():
    for doc in ("docs/SPEC.md", "README.md", "docs/decisions/1050-global-rank-v2.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        for term in ("effective tier", "capacity_share", "critical-path-blocker", "`update-from-main`"):
            assert term in text, (doc, term)
