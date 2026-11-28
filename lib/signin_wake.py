import contextlib
import json

from lib import account_doctor
from lib.device_pool import account_loads
from lib.machines_yml import MachinesConfig
from lib.queue import Job, JobRequest, SQLiteJobQueue


def announce(queue: SQLiteJobQueue, config: MachinesConfig, repo: str | None) -> list[Job]:
    if repo is None:
        return []
    with contextlib.closing(queue.connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            drops = account_doctor.take_unannounced(connection, config)
            loads = account_loads(connection, config, queue.now())
            jobs = [
                queue.enqueue_in_transaction(
                    connection,
                    _request(repo, drop, drop.account in loads.signed_in_on(config, drop.device, drop.harness)),
                    dedupe_key=_key(repo, drop),
                )
                for drop in drops
            ]
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return jobs


def _key(repo: str, drop: account_doctor.Drop) -> str:
    return f"signin-drop:{repo.lower()}:{drop.account}@{drop.device}:{drop.since}"


def _request(repo: str, drop: account_doctor.Drop, takes_jobs: bool) -> JobRequest:
    placed = "Jobs are still placed there." if takes_jobs else "No job is placed there."
    text = (
        f"The {drop.harness} sign-in of account {drop.account} on device {drop.device} dropped. "
        f"The doctor reads: {drop.reading}. {placed} "
        "Ask Max once to sign in from the dashboard Settings page. "
        "When Settings has no Sign in for this account and device, give Max the step in the reading. "
        "Do not run a login, and do not copy a login from another device."
    )
    return JobRequest(
        repo=repo,
        ticket_id=f"{drop.account}@{drop.device}",
        action="account-signin",
        recipient="grok",
        source_comment_id=_key(repo, drop),
        timeout_seconds=24 * 60 * 60,
        detail=json.dumps({
            "account": drop.account,
            "device": drop.device,
            "harness": drop.harness,
            "reading": drop.reading,
            "takes_jobs": takes_jobs,
            "link": f"https://github.com/{repo}/blob/HEAD/docs/ACCOUNTS.md#when-a-sign-in-drops",
            "detail": text,
        }, sort_keys=True),
    )
