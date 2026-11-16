"""T3/T4 verdicts: approvals and whether an approval covers a head."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from lib.dispatch.constants import MERGED_BEFORE_HANDOFF, _NOT_FINISHED
from lib.dispatch.helpers import (
    _comment_link,
    _comment_order,
    _holds,
    _is_full_sha,
    _is_patch_id,
    _matching_head,
    _round_number,
    _scope,
)
from lib.dispatch.transitions import write_workflow_state
from lib.dispatch.types import AlreadyMergedShip, CIResult, Comment, TrackedTicket
from lib.dispatch.verdict.blocked import BlockedMixin
from lib.dispatch.verdict.coverage import ApprovalCoverageMixin
from lib.dispatch.verdict.latest import LatestVerdictMixin
from lib.dispatch.verdict.preview import PreviewedHeadMixin
from lib.dispatch.verdict.record import ApprovalRecordMixin
from lib.dispatch.verdict.repeat import RepeatBlockMixin
from lib.dispatch.verdict.shipped import ShippedApprovalMixin
from lib.dispatch.verdict.t3 import ReviseCIGate, T3Mixin
from lib.dispatch.verdict.t4 import T4Mixin
from lib.handoff import HandoffError, parse_comment
from lib.queue import JobRequest


class VerdictMixin(
    T3Mixin,
    RepeatBlockMixin,
    T4Mixin,
    BlockedMixin,
    ApprovalRecordMixin,
    ApprovalCoverageMixin,
    ShippedApprovalMixin,
    PreviewedHeadMixin,
    LatestVerdictMixin,
):
    pass
