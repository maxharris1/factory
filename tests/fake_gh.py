#!/usr/bin/env python3
"""A stand-in `gh` for tests: `gh api [-X GET] <path>` answers from the JSON
file $FAKE_GH_STORE, which maps an API path to its response. A path the
store lacks is a 404. With no store it refuses, like the suite's ssh guard,
so no test reaches GitHub."""

import json
import os
import sys


def main(argv: list[str]) -> int:
    store = os.environ.get("FAKE_GH_STORE")
    if not store:
        print("tests: real gh is disabled", file=sys.stderr)
        return 1
    rest = [part for part in argv if part not in ("-X", "GET")]
    if rest[-2:-1] == ["-f"] and rest[-1].startswith("ref="):
        rest = [*rest[:-3], f"{rest[-3]}?{rest[-1]}"]
    if rest[:1] != ["api"] or len(rest) != 2:
        print(f"fake gh: unsupported {argv}", file=sys.stderr)
        return 2
    with open(store, encoding="utf-8") as handle:
        answers = json.load(handle)
    if rest[1] not in answers:
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        return 1
    print(json.dumps(answers[rest[1]]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
