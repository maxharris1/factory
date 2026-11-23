"""The shape of a `target: account` settings request (issue #1066).

The dashboard server writes these to the settings spool and the applier
reads them back. The server is standard library only, so it keeps its own
copy of these tables; tests/test_dashboard_http.py fails when the two differ.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

COMMON_KEYS = frozenset({"id", "actor", "requested_at", "target", "op", "account"})
OPS: Mapping[str, frozenset[str]] = MappingProxyType({
    "add": frozenset({"harness", "max_jobs", "signed_in_on"}),
    "set": frozenset({"field", "expect", "value"}),
    "remove": frozenset({"expect"}),
    "prepare": frozenset({"device"}),
    "doctor": frozenset({"device"}),
})
SET_FIELDS = frozenset({"max_jobs", "signed_in_on", "paused"})
REMOVE_EXPECT_KEYS = frozenset({"harness", "max_jobs", "paused", "signed_in_on"})
