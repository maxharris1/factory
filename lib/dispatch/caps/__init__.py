"""T1 and round caps: over-cap checks, effective caps, cap wakes and resume."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from lib.dispatch.caps.effective import EffectiveCapMixin
from lib.dispatch.caps.resume import ResumeAfterCapMixin
from lib.dispatch.caps.t1 import T1Mixin
from lib.dispatch.caps.wake import CappedWakeMixin
from lib.dispatch.constants import WATCHED_STATES
from lib.dispatch.helpers import _comment_link, _route_go, _scope
from lib.dispatch.types import Comment, TrackedTicket
from lib.queue import JobRequest


class CapsMixin(T1Mixin, ResumeAfterCapMixin, EffectiveCapMixin, CappedWakeMixin):
    pass
