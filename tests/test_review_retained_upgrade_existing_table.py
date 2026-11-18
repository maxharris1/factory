from __future__ import annotations

import ast
import inspect
import subprocess
import types
from pathlib import Path

import pytest

import lib.dispatch.comments as comments_module
from lib.dispatcher import Dispatcher
from tests.merge_queue_support import APPROVE, TRUSTED_LOGINS, sha
from tests.test_review_before_ci import board_with, built_with_ci_pending, reviewed_at_pending_ci

PRE_FLOOR_MAIN = "eb2b606e70d9509435281ed76e460c8fb35bf6d7"


def _pre_floor_methods():
    try:
        source = subprocess.run(
            ["git", "show", f"{PRE_FLOOR_MAIN}:lib/dispatch/comments.py"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip(f"needs {PRE_FLOOR_MAIN[:7]} in the git history (a shallow checkout lacks it)")
    mixin = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef) and node.name == "CommentsMixin"
    )
    methods = [
        node
        for node in mixin.body
        if isinstance(node, ast.FunctionDef)
        and (node.name == "_process_comments" or _signature_changed(node))
    ]
    namespace = vars(comments_module).copy()
    exec(
        compile(ast.Module(body=methods, type_ignores=[]), f"{PRE_FLOOR_MAIN}:comments.py", "exec"),
        namespace,
    )
    return {node.name: namespace[node.name] for node in methods}


def _signature_changed(node: ast.FunctionDef) -> bool:
    current = getattr(comments_module.CommentsMixin, node.name, None)
    if current is None:
        return False
    return [arg.arg for arg in node.args.args] != list(inspect.signature(current).parameters)


def _bind_old_methods(dispatcher: Dispatcher, methods: dict) -> None:
    for name, method in methods.items():
        setattr(dispatcher, name, types.MethodType(method, dispatcher))


def test_an_upgrade_over_an_interrupted_old_process_leaves_runnable_recovery(
    tmp_path: Path,
) -> None:
    old_methods = _pre_floor_methods()
    board = board_with(tmp_path, on=True)
    older = sha(1)
    pr = built_with_ci_pending(board, older)
    reviewed_at_pending_ci(board, pr, older, "approve-older")
    with board.queue.connect() as connection:
        connection.execute("ALTER TABLE verdict_heads DROP COLUMN posted_at")
        connection.execute("DROP TABLE verdict_heads_floor")
    _bind_old_methods(board.dispatcher, old_methods)
    mark = board.dispatcher._mark_handled

    def interrupted(ticket, comment_id, outcome):
        mark(ticket, comment_id, outcome)
        if comment_id == "approve-older":
            raise KeyboardInterrupt()

    board.dispatcher._mark_handled = interrupted
    pr.push(sha(2), ci="failure")
    board.post(pr, "approve-older", APPROVE.format(round="1/3", head=older), "codex-login")
    with pytest.raises(KeyboardInterrupt):
        board.poll()
    board.finish("claude")
    hidden = next(item for item in pr.comments if item.id == "approve-older")
    pr.comments.remove(hidden)
    board.dispatcher = Dispatcher(
        tmp_path / "d.db",
        board.queue,
        trusted_logins=TRUSTED_LOGINS,
        review_actions=board.actions,
        grok_delivery=board.grok,
        log=board.log.append,
    )
    with board.queue.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM verdict_heads WHERE comment_id = 'approve-older'"
            ).fetchone()[0]
            == 0
        )
    pr.push(older, ci="failure")
    board.ready(pr, older, "return-ready")
    board.poll()
    pr.comments.append(hidden)
    board.poll()

    fix = board.queue.claim("claude")
    review = board.queue.claim("codex")
    assert fix is not None or review is not None or board.outcome("return-ready") is None, (
        "upgrade after an old-process interruption left a handled ready and no runnable recovery"
    )
