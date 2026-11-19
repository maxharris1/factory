"""Replays a week of recorded PR review stops: today's stop rules against converge() (lib/dispatch/verdict/convergence.py).

Run from the repo root: `python3 scripts/replay_stops.py fetch <data>` fills <data> from GitHub, then
`python3 scripts/replay_stops.py report <data> > report.md` writes the report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.dispatch.verdict.convergence import Continue, Repeated, RoundFindings, Stalled, converge  # noqa: E402
from lib.evidence.findings import blocking_ids  # noqa: E402
from lib.handoff import HandoffError, parse_comment, repeat_id  # noqa: E402

WINDOW = ("2026-11-12T00:00:00Z", "2026-11-19T23:59:59Z")
RULE_MERGED_AT = {"unsat": "2026-11-15T22:29:42Z", "repeat": "2026-11-16T00:41:03Z"}
FINDING_IDS_FROM = "2026-11-15T10:41:58Z"
ALL_RULES_FROM = max(RULE_MERGED_AT.values())
AUTO_EXTRA_ROUNDS = {"maxharris1/factory": 2}
BASE_CAP = {"low": 1, "high": 3}
MARKER = re.compile(r"<!-- orch-auto-extra verdict=(\d+) -->")
STOPS = ("repeat", "unsat", "capped")
REPOS = ("maxharris1/factory", "aut0-code/aut0", "maxharris1/Cleffy")
TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
META_KEYS = ("closed_at", "created_at", "merged_at", "state", "title")
LABEL = {Continue: "continue", Repeated: "repeat", Stalled: "stall"}

Entry = tuple[RoundFindings, bool]


@dataclass(frozen=True)
class Verdict:
    cid: int
    at: str
    url: str
    head: str
    round: int
    cap: int | None
    verdict: str
    risk: str
    repeat: str | None
    unsat: str | None
    blocking: frozenset[str]

    @property
    def findings(self) -> RoundFindings:
        return RoundFindings(self.round, self.blocking, self.repeat)

    def same_review(self, other: Verdict) -> bool:
        return self.round == other.round and self.head[:7] == other.head[:7]


@dataclass(frozen=True)
class Event:
    at: str
    kind: str
    who: str
    body: str


@dataclass
class Pr:
    repo: str
    number: int
    meta: dict
    verdicts: list[Verdict] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    markers: set[int] = field(default_factory=set)

    @property
    def outcome(self) -> str:
        if self.meta.get("merged_at"):
            return "merged"
        if any(v.verdict == "APPROVE" for v in self.verdicts):
            return "approved, not merged"
        return "open" if self.meta.get("state") == "open" else "closed unmerged"


def in_window(at: str) -> bool:
    return WINDOW[0] <= at <= WINDOW[1]


def hours(a: str, b: str) -> float:
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (parse(b) - parse(a)).total_seconds() / 3600


def walk(series: Sequence[Entry | None]) -> list[str | None]:
    out: list[str | None] = []
    run: list[RoundFindings] = []
    for entry in series:
        if entry is None:
            out.append(None)
            run = []
            continue
        findings, unsat = entry
        out.append("unsat" if unsat else LABEL[type(converge(run, findings))])
        run.append(findings)
    return out


def parse_verdict(c: dict) -> Verdict | None:
    try:
        h = parse_comment(c["body"])
    except HandoffError:
        return None
    f = h.as_dict()
    m = re.match(r"(\d+)(?:/(\d+))?$", f.get("round", ""))
    if f["kind"] != "verdict" or f.get("role") != "reviewer" or not f.get("from", "").endswith("-bot") or m is None:
        return None
    repeat = repeat_id(h)
    return Verdict(
        c["id"], c["created_at"], c["html_url"], f.get("head", ""), int(m.group(1)),
        int(m.group(2)) if m.group(2) else None, f["verdict"], f.get("risk", "high"), repeat, f.get("unsat"),
        blocking_ids(h.body) | ({repeat} if repeat else frozenset()),
    )


def load(data: Path) -> list[Pr]:
    prs = []
    for line in (data / "prs.txt").read_text().splitlines():
        if not line.strip():
            continue
        slug, number = line.split()
        pr = Pr(slug.replace("_", "/", 1), int(number), json.loads((data / "meta" / f"{slug}_{number}.json").read_text()))
        rows = [json.loads(x) for x in (data / "threads" / f"{slug}_{number}.json").read_text().splitlines() if x]
        for c in sorted(rows, key=lambda c: (c["created_at"], c["id"])):
            pr.markers |= {int(m) for m in MARKER.findall(c["body"])}
            if (v := parse_verdict(c)) is not None:
                pr.verdicts.append(v)
                continue
            try:
                h = parse_comment(c["body"])
            except HandoffError:
                if re.match(r"\s*/go\b", c["body"]):
                    pr.events.append(Event(c["created_at"], "go", c["user"]["login"], c["body"]))
                continue
            f = h.as_dict()
            if f["kind"] in ("go", "answers", "ready", "questions"):
                pr.events.append(Event(c["created_at"], f["kind"], f.get("from", ""), h.body))
        prs.append(pr)
    return prs


def gh(path: str, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in TOKENS}
    return subprocess.run(["gh", "api", "--paginate", *args, path], env=env, check=True, capture_output=True, text=True).stdout


def fetch(data: Path) -> None:
    (data / "threads").mkdir(parents=True, exist_ok=True)
    (data / "meta").mkdir(exist_ok=True)
    listed = []
    for repo in REPOS:
        since = gh(f"repos/{repo}/issues/comments?since={WINDOW[0]}&per_page=100", "--jq", ".[] | tojson")
        comments = [json.loads(line) for line in since.splitlines() if line]
        numbers = sorted({int(c["issue_url"].rsplit("/", 1)[1]) for c in comments
                          if in_window(c["created_at"]) and parse_verdict(c) is not None})
        slug = repo.replace("/", "_")
        for n in numbers:
            thread = gh(f"repos/{repo}/issues/{n}/comments?per_page=100", "--jq", ".[] | tojson")
            (data / "threads" / f"{slug}_{n}.json").write_text(thread)
            meta = json.loads(gh(f"repos/{repo}/pulls/{n}"))
            (data / "meta" / f"{slug}_{n}.json").write_text(json.dumps({k: meta[k] for k in META_KEYS}))
            listed.append(f"{slug} {n}")
    (data / "prs.txt").write_text("".join(f"{line}\n" for line in sorted(listed)))


def auto_extra_deployed_at(prs: list[Pr]) -> str:
    return min(v.at for pr in prs for v in pr.verdicts if v.cid in pr.markers)


def old_policy(pr: Pr, auto_from: str, era: bool) -> dict[int, str]:
    live = lambda rule, v: not era or v.at >= RULE_MERGED_AT[rule]
    out: dict[int, str] = {}
    accepted: dict[int, Verdict] = {}
    for v in pr.verdicts:
        if v.verdict != "REVISE":
            continue
        held = accepted.get(v.round)
        if held is not None and held.same_review(v) and not held.unsat:
            out[v.cid] = "duplicate"
            continue
        prev = accepted.get(v.round - 1)
        accepted[v.round] = v
        if v.unsat and live("unsat", v):
            out[v.cid] = "unsat"
        elif live("repeat", v) and (v.repeat or (prev is not None and v.blocking & prev.blocking)):
            out[v.cid] = "repeat"
        elif v.cap is not None and v.round >= v.cap:
            granted = v.cap - BASE_CAP.get(v.risk, 3)
            auto = (v.at >= auto_from or not era) and granted < AUTO_EXTRA_ROUNDS.get(pr.repo, 0)
            out[v.cid] = "auto-extra" if auto else "capped"
        else:
            out[v.cid] = "continue"
    return out


def thread_after(pr: Pr, v: Verdict) -> str:
    if v.cid in pr.markers:
        return "auto-extra"
    nxt = next((w.at for w in pr.verdicts if w.at > v.at), "9999")
    e = next((e for e in pr.events if v.at < e.at < nxt), None)
    if e is None:
        return "none"
    if e.kind == "ready":
        return "continued"
    if e.kind == "questions":
        return "questions"
    return "infra-unstick" if "unstick" in e.body.lower()[:200] else "stopped"


def resume(pr: Pr, v: Verdict) -> tuple[float, str]:
    e = next((e for e in pr.events if e.at > v.at and e.kind != "questions"), None)
    if e is not None:
        return hours(v.at, e.at), f"{e.who} {e.kind}"
    end = min(pr.meta.get("closed_at") or WINDOW[1], WINDOW[1])
    return hours(v.at, end), "never resumed (" + ("open at window end" if end == WINDOW[1] else "closed") + ")"


def effective_rounds(pr: Pr) -> list[Verdict]:
    out: list[Verdict] = []
    for v in pr.verdicts:
        if out and out[-1].same_review(v):
            if not (out[-1].verdict == v.verdict == "REVISE" and not out[-1].unsat):
                out[-1] = v
        else:
            out.append(v)
    return out


def fmt_series(seq: list[Verdict]) -> str:
    parts = []
    for i, v in enumerate(seq):
        if v.verdict != "REVISE":
            parts.append(v.verdict[0])
            continue
        prev = seq[i - 1] if i and seq[i - 1].verdict == "REVISE" else None
        mark = "*" if prev is not None and v.blocking & prev.blocking else ""
        parts.append(f"{len(v.blocking)}{mark}{'R' if v.repeat else ''}{'U' if v.unsat else ''}")
    return ",".join(parts)


def converge_outcome(kind: str, labels: list[str | None], seq: list[Verdict], i: int) -> str:
    label = "unsat" if kind == "unsat" else labels[i]
    if label != "continue":
        return str(label)
    later = next((j for j in range(i + 1, len(seq)) if labels[j] != "continue"), None)
    if later is None:
        return "skip; then no later round"
    return f"skip; then {seq[later].verdict} e{later + 1}" if labels[later] is None else f"skip; then stops e{later + 1} {labels[later]}"


def thread_check(kind: str, ev: str) -> str:
    if kind == "capped*":
        return "auto-extra simulated, thread shows a capped stop (counted as capped*)"
    if ev == "infra-unstick":
        return "infra unstick (not a review stop)"
    if ev == "questions":
        return "builder or reviewer questions (not a review stop)"
    stop = kind in STOPS
    if (stop and ev == "stopped") or (not stop and ev in ("continued", "auto-extra")) or kind == "duplicate":
        return "confirmed"
    return "simulated only" if ev == "none" else "contradicted"


def report(data: Path) -> None:
    prs = load(data)
    auto_from = auto_extra_deployed_at(prs)
    window = [v for pr in prs for v in pr.verdicts if in_window(v.at)]
    revises = [v for v in window if v.verdict == "REVISE"]
    check: Counter[str] = Counter()
    today: Counter[str] = Counter()
    contradicted, rows, new_stops = [], [], []
    kinds: dict[str, list[float]] = {}
    skipped: dict[str, list[tuple[float, str]]] = {"converge": [], "converge (all rules live)": []}
    vs_today = dict.fromkeys(("today stops", "skipped", "kept", "new stops"), 0)
    for pr in prs:
        era = old_policy(pr, auto_from, era=True)
        now = old_policy(pr, auto_from, era=False)
        seq = effective_rounds(pr)
        labels = walk([(v.findings, bool(v.unsat)) if v.verdict == "REVISE" else None for v in seq])
        where = {v.cid: i for i, v in enumerate(seq)}
        for v in pr.verdicts:
            if v.cid not in era or not in_window(v.at):
                continue
            today[now[v.cid]] += 1
            kind, ev = era[v.cid], thread_after(pr, v)
            kind = "capped*" if kind == "auto-extra" and ev == "stopped" else kind
            check[agreement := thread_check(kind, ev)] += 1
            if agreement == "contradicted":
                contradicted.append(f"{pr.repo}#{pr.number} r{v.round}/{v.cap} sim={kind} thread={ev} {v.url}")
            if kind not in STOPS and kind != "capped*":
                continue
            wait, how = resume(pr, v)
            i = where.get(v.cid, next(j for j, w in enumerate(seq) if w.same_review(v)))
            outcome = converge_outcome(kind, labels, seq, i)
            for suffix in ("", " (all rules live)") if v.at >= ALL_RULES_FROM else ("",):
                kinds.setdefault(kind + suffix, []).append(wait)
                if outcome.startswith("skip"):
                    skipped["converge" + suffix].append((wait, kind))
            rows.append((pr, v, kind, ev, wait, how, outcome, seq, i))
        for i, v in enumerate(seq):
            today_kind = now.get(v.cid)
            if today_kind is None or not in_window(v.at) or v.at < FINDING_IDS_FROM:
                continue
            if today_kind in STOPS:
                vs_today["today stops"] += 1
                vs_today["skipped" if labels[i] == "continue" and today_kind != "unsat" else "kept"] += 1
            elif labels[i] not in (None, "continue"):
                vs_today["new stops"] += 1
                new_stops.append((pr, v, i, labels[i], today_kind, seq))

    print("# Review-stop replay: today's rules vs converge (generated by scripts/replay_stops.py)\n")
    print(f"PR threads read: {len(prs)}. Trusted reviewer verdicts in window: {len(window)} "
          f"({len(revises)} REVISE, {sum(v.verdict == 'BLOCKED' for v in window)} BLOCKED). "
          f"REVISE with an empty blocking set: {sum(not v.blocking for v in revises)}.")
    print(f"Auto-extra first applied to a verdict at {auto_from} (earliest marker). Rule merge times: {RULE_MERGED_AT}.\n")
    print("Old-policy simulation (era-aware) vs thread evidence, per in-window REVISE: "
          + ", ".join(f"{k} {n}" for k, n in check.items()) + ".")
    for c in contradicted:
        print(f"- contradicted: {c}")
    print(f"\nToday's rules applied to every in-window REVISE (no era): {dict(sorted(today.items()))}\n")
    print("## Old stops\n")
    print("Series: one entry per effective round (e1, e2, ...), n = blocking ids including repeat=. `*` shares an id "
          "with the entry before, `R` explicit repeat=, `U` unsat=, `A`/`B` APPROVE/BLOCKED.\n")
    print("| PR | series | old stop | thread | wait h | resumed by | converge | PR outcome |")
    print("|---|---|---|---|---|---|---|---|")
    for pr, v, kind, ev, wait, how, outcome, seq, i in rows:
        print(f"| [{pr.repo.split('/')[1]}#{pr.number}]({v.url}) | {fmt_series(seq)} | {kind} e{i + 1} (r{v.round}/{v.cap}) "
              f"| {ev} | {wait:.2f} | {how} | {outcome} | {pr.outcome} |")
    print("\n## Totals\n")
    for kind, waits in sorted(kinds.items()):
        print(f"- old {kind}: {len(waits)} stops, {sum(waits):.1f} h waiting, median {sorted(waits)[len(waits) // 2]:.2f} h")
    for name, skips in skipped.items():
        by = dict(Counter(kind for _, kind in skips))
        print(f"- {name} skips {len(skips)} {by}, {sum(w for w, _ in skips):.1f} h waiting")
    print(f"\n## Today's rules (repeat, unsat, capped with auto-extra) vs converge, REVISE from {FINDING_IDS_FROM} (finding ids exist)\n")
    print(f"- converge: {vs_today}")
    print("\n## New stops: converge stops where today's rules continue\n")
    for pr, v, i, label, old, seq in new_stops:
        print(f"- {pr.repo}#{pr.number} e{i + 1} (r{v.round}/{v.cap}) {label} (today {old}), series {fmt_series(seq)}, {pr.outcome}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "report"))
    parser.add_argument("data", type=Path)
    args = parser.parse_args()
    if args.command == "fetch":
        fetch(args.data)
    else:
        report(args.data)


if __name__ == "__main__":
    main()
