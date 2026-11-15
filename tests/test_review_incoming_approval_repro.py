import json

import pytest

from lib.queue import _job
from lib.update_check import rebase_scope
from tests.merge_queue_support import APPROVE, Board
from tests.test_force_push_review import brief
from tests.test_rebase_scope import Repo


@pytest.mark.job_bound
def test_incoming_approval_is_the_review_baseline(tmp_path):
    git = Repo(tmp_path / 'git')
    first = git.pr_commit('value = 1\n')
    reviewed = git.pr_commit('value = 2\n')
    git.git('checkout', '-q', '-B', 'pr', git.base)
    (git.root / 'feature.py').write_text('value = 1\n')
    latest = git.commit('restore round 1 patch')
    assert git.git('diff', first, latest) == ''
    assert git.git('diff', reviewed, latest) != ''
    board = Board(tmp_path)
    board.dispatcher.rebase_classifier = lambda _repo, _base, last, head: rebase_scope(
        git.gitdir, last, head, 'refs/heads/main'
    )
    pr = board.open('42', first)
    pr.mergeable = pr.mergeable_state = None
    board.go(pr, 'go-42')
    board.poll()

    def ready(head):
        build = board.claim('claude')
        cid = board.ready(pr, head)
        board.queue.finish(build.id, owner='claude', handoff_comment_id=cid)
        board.poll()
        assert board.outcome(cid) == 'queued-review'

    ready(first)
    review = board.claim('codex')
    revise = board.revise(pr, '1/3')
    board.queue.finish(review.id, owner='codex', handoff_comment_id=revise)
    board.poll()
    assert board.outcome(revise) == 'queued-review-fix'
    pr.push(reviewed)
    ready(reviewed)
    review = board.claim('codex')
    pr.push(latest)
    board.post(pr, 'approve-reviewed', APPROVE.format(round='2/3', head=reviewed), 'codex-login')
    board.queue.finish(review.id, owner='codex', handoff_comment_id='approve-reviewed')
    board.poll()
    assert board.outcome('approve-reviewed') == 'queued-patch-review'
    row = board.jobs('review')[-1]
    detail = json.loads(row['detail'])
    assert (row['head'], row['state'], detail['round']) == (latest, 'queued', '2/3')
    assert detail['evidence']['findings']['reviewed_head'] == reviewed
    assert detail['rebase']['last_reviewed_head'] == reviewed
    assert f'git range-diff {git.base}..{reviewed} {git.base}..{latest}' in brief(_job(row))
