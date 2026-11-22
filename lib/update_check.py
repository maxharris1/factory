"""Is an update-from-main merge trivial? (#130)

The merge queue carries an approval to an update only when the update is
the approved head merged with base and nothing more: every conflict hunk
resolved by taking exactly one side, and every other path exactly as git's
own merge leaves it. In a file matching the project's `shared_paths`
(issue #261), a hunk may also keep both sides, ours then theirs or theirs
then ours, byte for byte. Anything else, or any doubt, is non-trivial and
goes back to review.

The check runs on a bare clone that already holds the three commits, with
git plumbing only (`git merge-tree --write-tree`, git 2.38 or newer). It
never reads or writes a worktree and never changes a ref.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib.feeder_gates import path_matches
from lib.hold import MergeStep, MergeUpdate
from lib.secret_scan import ScanResult, scan_added

MARKER_SIZE = 61
_MARKER_RUN = re.compile(rb"([<|=>])\1*")
_LINE = re.compile(rb"[^\n]*\n|[^\n]+")
TIMEOUT_SECONDS = 120.0
_REGULAR_MODES = ("100644", "100755")
_TREE_ID = re.compile(rb"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class HeadDiff:
    patch_id: str | None
    files: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class UpdateScope:
    last_reviewed_head: str
    merge: str
    commits: tuple[str, ...]

    @classmethod
    def from_detail(
        cls, detail: dict[str, object], head: str | None
    ) -> UpdateScope | None:
        if detail.get("kind") != "update" or detail.get("head") != head:
            return None
        return cls(
            str(detail["last_reviewed_head"]),
            str(detail["update_merge"]),
            tuple(str(sha) for sha in detail["update_commits"]),
        )


@dataclass(frozen=True)
class RebaseScope:
    last_reviewed_head: str
    reviewed_base: str
    base: str


def rebase_scope(
    clone: Path, last_reviewed_head: str, head: str, base_ref: str
) -> RebaseScope | None:
    """None when head descends from last_reviewed_head (a plain push);
    otherwise both fork points from base_ref, for `git range-diff`.
    Raises ValueError when the clone cannot answer."""
    last = _resolve(clone, last_reviewed_head)
    new = _resolve(clone, head)
    ancestry = _run(clone, "merge-base", "--is-ancestor", last, new).returncode
    if ancestry == 0:
        return None
    if ancestry != 1:
        raise ValueError("git merge-base --is-ancestor failed")
    return RebaseScope(last, _merge_base(clone, last, base_ref), _merge_base(clone, new, base_ref))


def merge_update(clone: Path, approved_head: str, head: str, base_ref: str) -> MergeUpdate:
    """What lib.hold.ok_carries needs to judge whether an ok at
    approved_head covers head (issue #574). Raises ValueError (or an
    OSError or SubprocessError) when the clone cannot answer."""
    approved = _resolve(clone, approved_head)
    new = _resolve(clone, head)
    ancestry = _run(clone, "merge-base", "--is-ancestor", approved, new).returncode
    if ancestry == 1:
        return MergeUpdate(approved, (), frozenset(), frozenset())
    if ancestry != 0:
        raise ValueError("git merge-base --is-ancestor failed")
    chain = _output(
        clone, "rev-list", "--first-parent", "--parents", "--reverse", f"{approved}..{new}"
    ).decode("ascii")
    steps = []
    for line in chain.splitlines():
        commit, *parents = line.split()
        on_base = all(_on_base(clone, parent, base_ref) for parent in parents[1:])
        hand_merged = (
            _hand_merged_paths(clone, commit, *parents)
            if len(parents) == 2 and on_base
            else frozenset()
        )
        steps.append(MergeStep(commit, tuple(parents), on_base, hand_merged))
    return MergeUpdate(
        approved,
        tuple(steps),
        _own_files(clone, approved, base_ref),
        _own_files(clone, new, base_ref),
    )


@dataclass(frozen=True)
class MovedHead:
    parents: tuple[str, ...]
    second_parent_on_base: bool
    remerge_empty: bool
    paths: frozenset[str]
    scan: ScanResult


def moved_head_probe(clone: Path, base_ref: str, ok: str, head: str) -> MovedHead:
    """What a head that moved past a hold ok at `ok` is (#1310): its parents,
    whether its second parent is on base_ref, whether git's own remerge of
    those parents gives exactly head, every path changed since ok, and the
    secrets scan of the lines added since ok. Raises ValueError (or an
    OSError or SubprocessError) when the clone cannot answer."""
    old = _resolve(clone, ok)
    new = _resolve(clone, head)
    _resolve(clone, base_ref)
    _commit, *parents = _output(clone, "rev-list", "--parents", "-n1", new).decode("ascii").split()
    remerge = _output(
        clone, "show", "--remerge-diff", "--format=", "--no-ext-diff", "--no-color", new
    )
    listed = _output(clone, "diff", "--name-only", "-z", "--no-renames", old, new)
    added = _output(
        clone, "diff", "--no-ext-diff", "--no-textconv", "--text", "--unified=0",
        "--src-prefix=a/", "--dst-prefix=b/", old, new,
    )
    return MovedHead(
        tuple(parents),
        len(parents) >= 2 and _on_base(clone, parents[1], base_ref),
        not remerge,
        frozenset(path.decode("utf-8") for path in listed.split(b"\0") if path),
        scan_added(added),
    )


def head_diff(clone: Path, since: str | None, head: str, base_ref: str) -> HeadDiff:
    """What QA scope needs to know about head (#763): the stable patch id of
    head's own change from base_ref, and each path changed since `since`
    (head's fork from base_ref when None) with its added plus deleted lines.
    Raises ValueError when the clone cannot list the paths."""
    fork = _merge_base(clone, head, base_ref)
    listed = _output(clone, "diff", "--numstat", "--no-renames", "-z", since or fork, head)
    files = []
    for record in listed.split(b"\0"):
        if record:
            added, deleted, path = record.decode("utf-8").split("\t", 2)
            files.append((path, 1 if added == "-" else int(added) + int(deleted)))
    return HeadDiff(_patch_id(clone, fork, head), tuple(files))


def _patch_id(clone: Path, fork: str, head: str) -> str | None:
    diff = _run(clone, "diff", fork, head)
    if diff.returncode != 0 or not diff.stdout:
        return None
    result = _run(clone, "patch-id", "--stable", stdin=diff.stdout)
    words = result.stdout.split()
    return words[0].decode("ascii") if result.returncode == 0 and words else None


def _on_base(clone: Path, commit: str, base_ref: str) -> bool:
    code = _run(clone, "merge-base", "--is-ancestor", commit, base_ref).returncode
    if code not in (0, 1):
        raise ValueError(f"git merge-base --is-ancestor {commit} failed")
    return code == 0


def _hand_merged_paths(clone: Path, merge: str, first: str, second: str) -> frozenset[str]:
    merged = _run(
        clone, "merge-tree", "--write-tree", "--name-only", "-z", "--no-messages", first, second
    )
    if merged.returncode not in (0, 1):
        raise ValueError("git merge-tree failed")
    tree, *conflicted = merged.stdout.split(b"\0")
    if not _TREE_ID.fullmatch(tree):
        raise ValueError("git merge-tree wrote no tree")
    changed = _output(
        clone, "diff-tree", "-r", "--no-renames", "--name-only", "-z", tree.decode("ascii"), merge
    )
    return frozenset(
        path.decode("utf-8") for path in (*conflicted, *changed.split(b"\0")) if path
    )


def _own_files(clone: Path, head: str, base_ref: str) -> frozenset[str]:
    fork = _merge_base(clone, head, base_ref)
    listed = _output(clone, "diff-tree", "-r", "--no-renames", "--name-only", "-z", fork, head)
    return frozenset(path.decode("utf-8") for path in listed.split(b"\0") if path)


def _output(clone: Path, *args: str) -> bytes:
    result = _run(clone, *args)
    if result.returncode != 0:
        raise ValueError(f"git {args[0]} failed")
    return result.stdout


def _resolve(clone: Path, name: str) -> str:
    result = _run(clone, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
    if result.returncode != 0:
        raise ValueError(f"no commit {name}")
    return result.stdout.decode("ascii").strip()


def _merge_base(clone: Path, commit: str, base_ref: str) -> str:
    result = _run(clone, "merge-base", commit, base_ref)
    if result.returncode != 0:
        raise ValueError(f"no merge base for {commit}")
    return result.stdout.decode("ascii").strip()


@dataclass(frozen=True)
class UpdateClass:
    trivial: bool
    reason: str
    scope: UpdateScope | None = None


class _NotTrivial(Exception):
    pass


def classify(
    clone: Path,
    approved_head: str,
    new_head: str,
    base_ref: str,
    shared_paths: tuple[str, ...] = (),
) -> UpdateClass:
    """Whether new_head is a trivial update of approved_head from base_ref."""
    try:
        return UpdateClass(
            True, _check(clone, approved_head, new_head, base_ref, shared_paths)
        )
    except _NotTrivial as exc:
        return UpdateClass(False, str(exc), _scope(clone, approved_head, new_head, base_ref))
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError) as exc:
        return UpdateClass(False, f"check failed: {type(exc).__name__}")


def _check(
    clone: Path,
    approved_head: str,
    new_head: str,
    base_ref: str,
    shared_paths: tuple[str, ...],
) -> str:
    approved = _rev(clone, f"{approved_head}^{{commit}}")
    parents = _git(clone, "rev-list", "--parents", "-n", "1", new_head).split()
    if len(parents) != 3 or parents[1] != approved:
        raise _NotTrivial("not a merge of the approved head and base")
    ours, theirs = parents[1], parents[2]
    if _run(clone, "merge-base", "--is-ancestor", theirs, base_ref).returncode != 0:
        raise _NotTrivial("second parent is not on base")
    merged = _run(clone, "merge-tree", "--write-tree", "-z", ours, theirs)
    if merged.returncode not in (0, 1):
        raise _NotTrivial("git merge-tree failed")
    tree, conflicts = _merge_tree_output(merged.stdout)
    listed = _git(clone, "diff-tree", "-r", "--no-renames", "--name-only", "-z", tree, new_head)
    extra = sorted(set(filter(None, listed.split("\0"))) - set(conflicts))
    if extra:
        raise _NotTrivial("edits outside conflict resolution: " + ", ".join(extra[:5]))
    both = False
    for path, stages in sorted(conflicts.items()):
        both |= _check_conflict(
            clone, path, stages, new_head, path_matches(shared_paths, path)
        )
    if not conflicts:
        return "clean merge of base"
    if both:
        return (
            f"{len(conflicts)} conflicted file(s), each hunk resolved to one side"
            " or, in a shared path, both"
        )
    return f"{len(conflicts)} conflicted file(s), each hunk resolved to one side"


def _scope(
    clone: Path, approved_head: str, new_head: str, base_ref: str
) -> UpdateScope | None:
    try:
        approved = _rev(clone, f"{approved_head}^{{commit}}")
        head = _rev(clone, f"{new_head}^{{commit}}")
        chain = [
            line.split()
            for line in _git(
                clone, "rev-list", "--first-parent", "--parents", "--reverse",
                f"{approved}..{head}",
            ).splitlines()
        ]
        if not chain or len(chain[0]) != 3 or chain[0][1] != approved:
            return None
        merge, _ours, theirs = chain[0]
        if _run(clone, "merge-base", "--is-ancestor", theirs, base_ref).returncode != 0:
            return None
        if any(len(commit) != 2 for commit in chain[1:]):
            return None
        return UpdateScope(approved, merge, tuple(commit[0] for commit in chain[1:]))
    except (_NotTrivial, OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return None


def _merge_tree_output(output: bytes) -> tuple[str, dict[str, dict[int, tuple[str, str]]]]:
    """The merged tree and, per conflicted path, its stages (mode, blob)."""
    fields = output.split(b"\0")
    tree = fields[0].decode("ascii").strip()
    conflicts: dict[str, dict[int, tuple[str, str]]] = {}
    for field in fields[1:]:
        if not field:
            break
        info, _tab, path = field.partition(b"\t")
        mode, oid, stage = info.decode("ascii").split(" ")
        conflicts.setdefault(path.decode("utf-8"), {})[int(stage)] = (mode, oid)
    return tree, conflicts


def _check_conflict(
    clone: Path,
    path: str,
    stages: dict[int, tuple[str, str]],
    new_head: str,
    shared: bool = False,
) -> bool:
    """Raises unless every hunk took one side (or, when shared, both);
    returns whether some hunk needed both sides."""
    if 2 not in stages or 3 not in stages:
        raise _NotTrivial(f"{path}: not a content conflict")
    modes = {mode for mode, _oid in stages.values()}
    if len(modes) != 1 or not modes <= set(_REGULAR_MODES):
        raise _NotTrivial(f"{path}: mode or file-type conflict")
    entry = _git(clone, "ls-tree", "-z", new_head, "--", path).rstrip("\0")
    meta, _tab, _name = entry.partition("\t")
    parts = meta.split(" ")
    if len(parts) != 3 or parts[1] != "blob" or parts[0] not in modes:
        raise _NotTrivial(f"{path}: resolved to something other than the file")
    blobs = {stage: _blob(clone, oid) for stage, (_mode, oid) in stages.items()}
    blobs.setdefault(1, b"")
    if any(b"\0" in blob for blob in blobs.values()):
        raise _NotTrivial(f"{path}: binary conflict")
    segments = _conflict_segments(blobs[2], blobs[1], blobs[3])
    resolved = _blob(clone, parts[2])
    if _one_side_each(segments, resolved):
        return False
    if shared and _one_side_each(segments, resolved, both=True):
        return True
    raise _NotTrivial(f"{path}: a conflict hunk was not resolved to exactly one side")


def _conflict_segments(ours: bytes, base: bytes, theirs: bytes) -> list[tuple[bytes, ...]]:
    """git merge-file's result as text segments (1-tuples) and conflict
    hunks (ours, theirs)."""
    with tempfile.TemporaryDirectory() as scratch:
        names = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            target = Path(scratch) / name
            target.write_bytes(data)
            names.append(str(target))
        size = _marker_size(ours, base, theirs)
        result = subprocess.run(
            ["git", "merge-file", "-p", f"--marker-size={size}", *names],
            cwd=scratch,
            env=_environment(),
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    if not 0 <= result.returncode < 128:
        raise _NotTrivial("git merge-file failed")
    eol = b"\r\n" if result.stdout.endswith(b"\r\n") else b"\n"
    return _restore_final_newlines(
        _parse_markers(result.stdout, size), ours, theirs, eol
    )


def _restore_final_newlines(
    segments: list[tuple[bytes, ...]], ours: bytes, theirs: bytes, eol: bytes
) -> list[tuple[bytes, ...]]:
    """git merge-file ends a hunk side with the marker EOL (LF, or CRLF when
    the hunk's lines use CRLF) before the next marker even when that side is
    the end of a file with no final LF; drop those bytes so every side is the
    file's own bytes."""
    if len(segments) < 2 or segments[-1] != (b"",) or len(segments[-2]) != 2:
        return segments
    sides = tuple(
        side[: -len(eol)] if side.endswith(eol) and blob and not blob.endswith(b"\n") else side
        for side, blob in zip(segments[-2], (ours, theirs))
    )
    return [*segments[:-2], sides, segments[-1]]


def _marker_size(*texts: bytes) -> int:
    """Longer than any run of marker characters that starts a line in the
    inputs, so text already in a file never reads as a conflict marker."""
    longest = max(
        (len(run.group(0)) for text in texts for line in _lines(text)
         if (run := _MARKER_RUN.match(line))),
        default=0,
    )
    return max(MARKER_SIZE, longest + 1)


def _parse_markers(text: bytes, size: int) -> list[tuple[bytes, ...]]:
    start, base, middle, end = (bytes([char]) * size for char in b"<|=>")
    segments: list[tuple[bytes, ...]] = []
    plain: list[bytes] = []
    ours: list[bytes] = []
    theirs: list[bytes] = []
    state = "text"
    for line in _lines(text):
        bare = line.rstrip(b"\r\n")
        if state == "text" and _is_marker(bare, start):
            segments.append((b"".join(plain),))
            plain, ours, theirs = [], [], []
            state = "ours"
        elif state == "ours" and _is_marker(bare, base):
            state = "base"
        elif state in ("ours", "base") and bare == middle:
            state = "theirs"
        elif state == "theirs" and _is_marker(bare, end):
            segments.append((b"".join(ours), b"".join(theirs)))
            state = "text"
        elif state == "text":
            plain.append(line)
        elif state == "ours":
            ours.append(line)
        elif state == "theirs":
            theirs.append(line)
    if state != "text":
        raise _NotTrivial("unterminated conflict hunk")
    segments.append((b"".join(plain),))
    return segments


def _lines(text: bytes) -> list[bytes]:
    """Lines as git's xdiff sees them: ended by LF only, never by a bare CR."""
    return _LINE.findall(text)


def _is_marker(line: bytes, marker: bytes) -> bool:
    return line == marker or line.startswith(marker + b" ")


def _one_side_each(
    segments: list[tuple[bytes, ...]], resolved: bytes, both: bool = False
) -> bool:
    positions = {0}
    for segment in segments:
        options = set(segment)
        if both and len(segment) == 2:
            options |= {segment[0] + segment[1], segment[1] + segment[0]}
        positions = {
            position + len(option)
            for position in positions
            for option in options
            if resolved.startswith(option, position)
        }
        if not positions:
            return False
    return len(resolved) in positions


def _rev(clone: Path, name: str) -> str:
    return _git(clone, "rev-parse", "--verify", "--quiet", name).strip()


def _blob(clone: Path, oid: str) -> bytes:
    result = _run(clone, "cat-file", "blob", oid)
    if result.returncode != 0:
        raise _NotTrivial(f"cannot read blob {oid}")
    return result.stdout


def _git(clone: Path, *args: str) -> str:
    result = _run(clone, *args)
    if result.returncode != 0:
        raise _NotTrivial(f"git {args[0]} failed")
    return result.stdout.decode("utf-8")


def merge_base(clone: Path, head: str, base_ref: str) -> str:
    result = _run(clone, "merge-base", head, base_ref)
    if result.returncode != 0:
        raise ValueError("git merge-base failed")
    return result.stdout.decode().strip()


def _run(clone: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "--git-dir", str(clone), *args],
        input=stdin,
        env=_environment(),
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def _environment() -> dict[str, str]:
    """No user or system git config: a conflict style or merge driver set
    there must not change what the check sees. Every path is literal, so a
    filename such as `:(literal)x` or `*.py` names only itself."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in ("GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_LITERAL_PATHSPECS="1",
    )
    return environment
