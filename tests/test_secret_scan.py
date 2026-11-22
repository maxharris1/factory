"""#1310: the added-line secrets scan. Every fixture token is joined at
runtime, so this file holds no literal secret for push protection to catch."""

from __future__ import annotations

import pytest

from lib.secret_scan import MAX_DIFF_BYTES, PATTERNS, ScanResult, SecretHit, scan_added

TOKENS = {
    "private-key": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
    "github-token": "gh" + "p_" + "A1b2C3d4E5" * 4,
    "github-pat": "github" + "_pat_" + "11ABCDEFG0" * 3 + "_" + "x" * 30,
    "aws-access-key": "AK" + "IA" + "IOSFODNN7EXAMPLE",
    "aws-session-key": "AS" + "IA" + "IOSFODNN7EXAMPLE",
    "slack-token": "xo" + "xb-" + "1234567890-abcdefghij",
    "api-key": "s" + "k-" + "proj" + "Q" * 24,
    "anthropic-key": "s" + "k-" + "ant-" + "api03-" + "Z" * 30,
}
PATTERN_OF = {
    "private-key": "private-key",
    "github-token": "github-token",
    "github-pat": "github-token",
    "aws-access-key": "aws-access-key",
    "aws-session-key": "aws-access-key",
    "slack-token": "slack-token",
    "api-key": "api-key",
    "anthropic-key": "api-key",
}


def patch(path: str, *lines: str) -> bytes:
    body = "".join(f"{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,2 @@\n{body}"
    ).encode()


def test_the_table_covers_the_five_families() -> None:
    assert {name for name, _ in PATTERNS} == {
        "private-key", "github-token", "aws-access-key", "slack-token", "api-key"
    }


@pytest.mark.parametrize("kind", sorted(TOKENS))
def test_an_added_secret_is_found_with_its_path_and_pattern_name(kind: str) -> None:
    diff = patch("src/config.py", " context", f"+KEY = '{TOKENS[kind]}'")

    result = scan_added(diff)

    assert result == ScanResult("found", (SecretHit("src/config.py", PATTERN_OF[kind]),))
    assert TOKENS[kind] not in repr(result)


def test_a_clean_diff_is_clean() -> None:
    diff = patch("app.py", "-x = 1", "+x = 2", " y = 3") + patch("b.py", "+print('hi')")

    assert scan_added(diff) == ScanResult("clean", ())


@pytest.mark.parametrize("prefix", ["-", " "], ids=["removed", "context"])
def test_a_secret_on_a_removed_or_context_line_is_clean(prefix: str) -> None:
    diff = patch("app.py", f"{prefix}KEY = '{TOKENS['github-token']}'", "+x = 2")

    assert scan_added(diff) == ScanResult("clean", ())


def test_hits_name_the_file_each_secret_was_added_in() -> None:
    diff = patch("a.py", f"+{TOKENS['slack-token']}") + patch("b/c.env", f"+{TOKENS['aws-access-key']}")

    assert scan_added(diff).hits == (
        SecretHit("a.py", "slack-token"),
        SecretHit("b/c.env", "aws-access-key"),
    )


def test_a_diff_over_the_limit_is_too_large_and_not_read() -> None:
    diff = patch("big.txt", "+" + "a" * MAX_DIFF_BYTES)

    assert scan_added(diff) == ScanResult("too-large", ())
