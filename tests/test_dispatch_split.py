import ast
import importlib
import pkgutil
from pathlib import Path

import lib.dispatch
import lib.dispatcher
from lib.dispatcher import Dispatcher
from tests.test_wait import _exec_calls

ROOT = Path(__file__).resolve().parents[1]
FACADE = ROOT / "lib" / "dispatcher.py"
DISPATCH_FILES = sorted((ROOT / "lib" / "dispatch").glob("*.py"))

REPO_USED_NAMES = (
    "AlreadyMergedShip", "BASE_UPDATE_GRACE_SECONDS", "CIResult", "Comment",
    "Dispatcher", "Hook", "LINEAR_HANDOFF_QUERY", "LINEAR_STATE_PHASES", "LabelEvent",
    "OFF_LOOP_VERIFY_READS", "POLL_ERROR_LIMIT", "POLL_SECONDS", "PollLoop",
    "RetryableShipError", "RetryableStateError", "ReviewActions", "ReviewShipper",
    "ShipError", "ShipRequest", "TicketRef", "TrackedTicket", "_comment_order",
    "_holds", "_scope", "_spread", "poll_error_text", "safe_ship_error", "scrub_detail",
    "ship_mode", "trusted_bots",
)

NAMES_BEFORE_SPLIT = (
    "ACTED_VERDICT_OUTCOMES", "AlreadyMergedShip", "Approvals",
    "BASE_UPDATE_GRACE_SECONDS", "CIResult", "CORE_KINDS", "Callable", "Comment",
    "DEAD", "DEFAULT_JOB_TIMEOUT_MINUTES", "DeviceChecks", "Dispatcher",
    "FINISHED_STATES", "FactoryConfig", "Feeder", "GITHUB_STATE_LABEL_PREFIX",
    "GROK_WAKE_REASONS", "GrokDelivery", "GrokDeliveryError", "HARNESSES",
    "HandoffError", "HoldDecision", "Hook", "Job", "JobRequest", "JobStateConfig",
    "LINEAR_HANDOFF_QUERY", "LINEAR_STATE_PHASES", "LabelEvent", "Liveness",
    "MERGED_BEFORE_HANDOFF", "Mapping", "OFF_LOOP_VERIFY_READS", "ORCHESTRATOR",
    "POLL_ERROR_DETAIL_LIMIT", "POLL_ERROR_FRAMES", "POLL_ERROR_LIMIT",
    "POLL_ERROR_STREAK", "POLL_SECONDS", "Path", "PollLoop", "PollSource", "Protocol",
    "QueueError", "RELEASED_UNSTARTED", "RESTORED", "RUN_DIED", "RetryableShipError",
    "RetryableStateError", "ReviewActions", "ReviewShipper", "SETTLE_FAILURES_TO_WAKE",
    "SHIP_ERROR_LIMIT", "SLOTS_TABLE", "SOURCES_TABLE", "SQLiteJobQueue", "STALE",
    "STOPPED_POLL_SECONDS", "STOPPED_STATES", "STUCK_WAKE_FAILURE_LIMIT", "Sequence",
    "ShipError", "ShipRequest", "TITLE_LIMIT", "TicketRef", "TrackedTicket",
    "UPDATE_FAILURES_TO_STOP", "UPDATE_READY_GRACE_SECONDS", "UpdateClass",
    "UpdateClassifier", "WATCHED_STATES", "WORKER_KINDS", "_ABANDONED_JOB_DETAIL",
    "_ANSWERS", "_CREDENTIAL_WORD", "_FENCE_OPEN", "_HANDOFF_UNREAD", "_LIST_MARKER",
    "_NOT_FINISHED", "_NO_QUESTIONS", "_PLAIN_MESSAGE", "_PLAIN_REDACTED",
    "_POLL_ERROR_CAUSE", "_POLL_ERROR_CONTEXT", "_QUESTIONS_HEADING", "_REDACTED_MARKS",
    "_REJECTED_OUTCOMES", "_SECRET_ASSIGNMENT", "_SECRET_HOST_PATH", "_SECRET_NAME",
    "_SECRET_TOKEN", "_SECRET_URL", "_UNVERIFIED_HANDOFF", "_acted_verdict",
    "_add_missing_columns", "_after_poll", "_cap", "_collapse_fences", "_comment_link",
    "_comment_order", "_detail_project_bot", "_detail_round_number",
    "_grok_logical_key", "_has_questions", "_holds", "_is_full_sha", "_is_patch_id",
    "_job", "_matching_head", "_named", "_normalize_trusted_logins", "_plain_prefix",
    "_question_item", "_queue_state", "_redact", "_redact_spans", "_redrive_eligible",
    "_retry_detail", "_round_number", "_scope", "_section_asks", "_ship_state",
    "_spread", "_state_for_action", "_without_body", "annotations", "attention",
    "claimed_too_long", "dataclass", "decide", "derive", "free_slots", "islice", "json",
    "last_run_end", "parse_comment", "poll_error_detail", "poll_error_text", "prefixed",
    "project_rules", "re", "read_placements", "requeue_unstarted", "restored_claim",
    "retire_requeued", "safe_ship_error", "scrub_detail", "ship_mode", "sqlite3", "sys",
    "time", "traceback", "trusted_bots", "unstarted_too_long", "used_slots", "zlib",
)

DISPATCHER_METHODS = (
    "__init__", "_abandon_closed", "_advance_merge_queue", "_advance_merge_queues",
    "_advance_poll_error_streak", "_after_finish", "_answered_by", "_app_may_post",
    "_approval_covers", "_approver_go", "_base_update_out", "_busy_recipients",
    "_cancel_answered", "_cancel_answered_jobs", "_check_devices", "_check_job_states",
    "_classify_update", "_clear_poll_error_streak", "_close_abandoned_jobs",
    "_complete_redrive", "_complete_ship_intent", "_config_cap", "_connect",
    "_deliver_grok_jobs", "_device_offline", "_discard_pending_ship_intent",
    "_discover", "_done_from_handoff", "_duplicate_verdict", "_effective_cap",
    "_enqueue_or_flag", "_enqueue_step", "_ensure_all_stuck_wakes",
    "_ensure_stuck_wake", "_ensure_stuck_wake_in_transaction", "_enter_merge_queue",
    "_enter_merged", "_enter_ship_state", "_external_ship_completed",
    "_fail_abandoned_queued", "_fail_dead", "_fail_died_run", "_feed",
    "_finish_external_ship", "_finished_identity", "_flag_attention_once",
    "_flag_ready_after_cap", "_fresh_ticket", "_generation", "_handled_outcome",
    "_handoff_problem", "_has_completed_ship_at", "_has_completed_ship_intent",
    "_has_review_job", "_held", "_hold_decision", "_hold_gate", "_hold_ok_outcome",
    "_hold_ship_intent", "_initialize", "_is_abandoned", "_is_handled", "_is_stuck",
    "_is_stuck_identity", "_job_builder_harness", "_job_detail", "_job_event_recorded",
    "_latest_plan_step", "_latest_review_head", "_latest_review_verdict",
    "_latest_step", "_leave_merge_queue", "_log_abandoned_jobs", "_log_job_event",
    "_mark_handled", "_mark_ship_intent_failed", "_mark_stuck_and_wake", "_may_approve",
    "_may_approve_go", "_merge_queue_head", "_merge_queue_members", "_merge_turn",
    "_migrate_ship_intent_redrive", "_note_timeout_held", "_observed",
    "_open_fix_for_head", "_over_cap", "_paused_between", "_posted_handoff",
    "_prepare_external_ship", "_prepare_ship_intent", "_previewed_head",
    "_process_comments", "_project_wake_detail", "_queue_base_update", "_queue_comment",
    "_queue_patch_review", "_queue_review", "_queue_row", "_queue_update",
    "_record_approval", "_record_ci_result", "_record_factory_reads",
    "_record_job_error", "_record_job_event", "_record_job_event_once",
    "_record_poll_error", "_redrive_handoffs", "_reenter_after_handoff",
    "_reject_edited", "_release_claimed", "_release_unstarted", "_replay_ship_intent",
    "_requeue_unanswered_jobs", "_restart_queue_wait", "_restored_failure_waits",
    "_resume_after_cap", "_review_actions", "_review_context", "_review_retry_waits",
    "_run_hooks", "_set_redrive", "_set_stuck_in_transaction", "_set_workflow_state",
    "_settle_external_ship", "_settle_external_ships", "_settle_update", "_ship_head",
    "_ship_intent", "_shipped_approval", "_skip_review_retry", "_starts_tracking",
    "_superseded_by_revise", "_t1", "_t2", "_t3", "_t4", "_t5_abandoned", "_t5_answers",
    "_t5_questions", "_t6", "_t_plan", "_title_pull", "_tracked_github_ids",
    "_tracked_linear_ids", "_tracked_refs", "_tracker", "_trusted", "_trusted_bots",
    "_trusted_login", "_trusted_reviewer", "_trusted_session",
    "_try_settle_external_ship", "_unpolled_handoff_refs", "_update_failed",
    "_update_job_before", "_update_unanswered", "_verdict_after",
    "_verdict_behind_question", "_verify_handoffs", "_wake_capped",
    "_wake_comment_problem", "_wake_done", "_wake_ship_failed", "_watched",
    "_write_state_label", "active_job_tickets", "clear_attention", "flag_attention",
    "gate_count", "is_stuck", "poll_once", "process", "reconcile_jobs",
    "record_poll_deferral", "record_poll_error", "record_poll_exception",
    "record_terminal", "unpolled_linear_tickets", "wake_grok_once",
)


def _dispatch_modules():
    return [
        importlib.import_module(f"lib.dispatch.{info.name}")
        for info in pkgutil.iter_modules(lib.dispatch.__path__)
    ]


def test_every_name_the_facade_had_still_resolves() -> None:
    assert set(REPO_USED_NAMES) <= set(NAMES_BEFORE_SPLIT)
    for name in NAMES_BEFORE_SPLIT:
        assert hasattr(lib.dispatcher, name), name


def test_facade_names_are_the_objects_their_dispatch_module_defines() -> None:
    for module in _dispatch_modules():
        for name, value in vars(module).items():
            if getattr(value, "__module__", None) == module.__name__ and name in NAMES_BEFORE_SPLIT:
                assert getattr(lib.dispatcher, name) is value, name


def test_every_dispatcher_method_is_defined_once_and_only_in_a_mixin() -> None:
    owners = {}
    for cls in Dispatcher.__mro__[1:]:
        for name, value in vars(cls).items():
            if callable(value) and cls is not object:
                assert name not in owners, (name, owners.get(name), cls)
                owners[name] = cls
    for name in DISPATCHER_METHODS:
        assert name in owners, name
        assert owners[name].__module__.startswith("lib.dispatch."), name
    assert not [v for v in vars(Dispatcher).values() if callable(v)]


def test_the_facade_only_imports_and_composes() -> None:
    text = FACADE.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 400
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        elif isinstance(node, ast.ClassDef):
            assert node.name == "Dispatcher"
            assert all(isinstance(item, (ast.Expr, ast.Pass)) for item in node.body)
        else:
            assert isinstance(node, (ast.Import, ast.ImportFrom)), ast.dump(node)


def _runtime_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    type_only = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for inner in ast.walk(node)
    }
    modules: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in type_only:
            continue
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_the_moved_code_keeps_the_dispatcher_invariants() -> None:
    forbidden = {"subprocess", "os", "lib.waiter", "lib.job_run", "lib.brief", "lib.placed_run"}
    for path in [FACADE, *DISPATCH_FILES]:
        assert not forbidden & _runtime_imports(path), path
        assert _exec_calls(path) == [], path
        text = path.read_text(encoding="utf-8")
        assert "90 minutes" not in text, path
        assert "job_timeout: 90" not in text, path
    for path in DISPATCH_FILES:
        assert "lib.dispatcher" not in _runtime_imports(path), path


def test_the_readme_names_every_dispatch_module() -> None:
    readme = (ROOT / "lib" / "dispatch" / "README.md").read_text(encoding="utf-8")
    for path in DISPATCH_FILES:
        if path.name != "__init__.py":
            assert f"`{path.name}`" in readme, path.name
