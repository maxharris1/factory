#!/bin/sh
# /statechart check for the section 4 ticket loop.
# Phase 1 and the T9-enabled model must pass TLC. The drawing configs must
# match the section 4.1 state names.
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
spec=$root/model/Orchestrator.tla
view=$root/model/view.mjs
pin=de8ee15dd1194627d21eb5fcd41cb1873208dabb
jar_sha=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88

# GitHub sometimes answers 429 or 5xx. Try each download a few times.
retry() {
  tries=1
  until "$@"; do
    [ "$tries" -lt 5 ] || return 1
    sleep $((tries * 15))
    tries=$((tries + 1))
  done
}

if [ -z "${TLA2TOOLS:-}" ]; then
  dest=${XDG_CACHE_HOME:-$HOME/.cache}/factory-tlc/tla2tools-1.7.4.jar
  mkdir -p "$(dirname "$dest")"
  if [ ! -f "$dest" ] || ! echo "$jar_sha  $dest" | sha256sum -c - >/dev/null 2>&1; then
    tmp=$(mktemp)
    retry curl -fsSL -o "$tmp" "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
    echo "$jar_sha  $tmp" | sha256sum -c -
    mv "$tmp" "$dest"
  fi
  TLA2TOOLS=$dest
  export TLA2TOOLS
fi

if [ -z "${STATECHART:-}" ]; then
  bundled=$HOME/.cursor/plugins/cache/cursor-public/9717366/$pin/skills/statechart/scripts/statechart
  if [ -f "$bundled" ]; then
    STATECHART=$bundled
  else
    cache=${XDG_CACHE_HOME:-$HOME/.cache}/factory-statechart/$pin
    STATECHART=$cache/pstack/skills/statechart/scripts/statechart
    if [ ! -f "$STATECHART" ]; then
      rm -rf "$cache"
      mkdir -p "$cache"
      git -C "$cache" init -q
      git -C "$cache" remote add origin https://github.com/cursor/plugins.git
      git -C "$cache" sparse-checkout init --cone
      git -C "$cache" sparse-checkout set pstack/skills/statechart
      retry git -C "$cache" fetch --depth 1 origin "$pin"
      git -C "$cache" checkout -q --detach FETCH_HEAD
    fi
  fi
  export STATECHART
fi

if [ ! -f "$STATECHART" ]; then
  echo "statechart launcher not found at $STATECHART" >&2
  exit 1
fi

section41="abandoned
awaiting-ok
blocked
building
capped
done
fixing
handed-off
handed-off-externally
intake
merge-queue
merged
plan-check
planning
post-merge
questions
refused
review
ship
stuck
updating
waiting-external
waiting-max"

phase1_states=$(printf '%s\n' "$section41" | grep -vx 'post-merge')

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

state_labels() {
  sed -n 's/^  s[0-9][0-9]*: //p' "$1" | sort
}

# statechart drops a step that stays on one label. Section 4.1 still has
# Building --> Building and Fixing --> Fixing for CI red. Put those arrows on
# the drawing. The injected loops are checked against section 4.1, not the
# model.
add_ci_red_self_loops() {
  awk '
    $1 ~ /^s[0-9]+:$/ && ($2 == "building" || $2 == "fixing") {
      id = $1
      sub(/:$/, "", id)
      ids[++n] = id
    }
    $2 == "-->" && $3 == $1 ":" { has[$1] = 1 }
    /^  class s/ && !inserted {
      for (i = 1; i <= n; i++) {
        if (!has[ids[i]]) print "  " ids[i] " --> " ids[i] ": CI red, fix"
      }
      inserted = 1
    }
    { print }
  ' "$1"
}

# The state the shortest path under "## <rule> breaks" ends in.
break_end() {
  awk -v head="## $1 breaks" '
    $0 == head { on = 1; next }
    on && /^## / { exit }
    on && /^[0-9]+\. / { last = $0 }
    END {
      n = split(last, parts, ": ")
      sub(/^[0-9]+\. /, "", parts[n])
      print parts[n]
    }
  ' "$2"
}

expect_break() {
  name=$1
  rule=$2
  end=$3
  grep -q "^## $rule breaks\$" "$RUN_OUT/report.md" || fail "$name did not break $rule"
  [ "$(break_end "$rule" "$RUN_OUT/report.md")" = "$end" ] ||
    fail "$name counterexample did not end in $end"
}

# Logs stay outside --out. statechart refuses a folder that is not empty.
run_statechart() {
  cfg=$1
  RUN_OUT=$(mktemp -d)
  RUN_LOG=$(mktemp)
  RUN_ERR=$(mktemp)
  set +e
  "$STATECHART" "$spec" --config "$cfg" --view "$view" --out "$RUN_OUT" >"$RUN_LOG" 2>"$RUN_ERR"
  RUN_CODE=$?
  set -e
}

show_run() {
  echo "--- stdout ---" >&2
  cat "$RUN_LOG" >&2
  echo "--- stderr ---" >&2
  cat "$RUN_ERR" >&2
}

expect_clean() {
  name=$1
  expected=$2
  if [ "$RUN_CODE" -ne "$expected" ]; then
    show_run
    fail "$name exited $RUN_CODE, expected $expected"
  fi
  if [ -s "$RUN_ERR" ]; then
    show_run
    fail "$name wrote to stderr"
  fi
}

echo "1/9 phase 1 model (T9 off)"
run_statechart "$root/model/Orchestrator.cfg"
expect_clean "phase 1" 0
grep -q '^## Rules that hold$' "$RUN_OUT/report.md" || fail "phase 1 report has no holding rules"
grep -q 'T1KindGo' "$RUN_OUT/report.md" || fail "phase 1 report did not check T1KindGo"
grep -q 'T1PDecided' "$RUN_OUT/report.md" || fail "phase 1 report did not check T1PDecided"
grep -q 'Phase1T9Off' "$RUN_OUT/report.md" || fail "phase 1 report did not check Phase1T9Off"
grep -q 'HandoffCountsApprovedRound' "$RUN_OUT/report.md" || fail "phase 1 report did not check HandoffCountsApprovedRound"
grep -q 'ReopenHoldOnlyAtWork' "$RUN_OUT/report.md" || fail "phase 1 report did not check ReopenHoldOnlyAtWork"
grep -q 'ShipNeedsCoveredQa' "$RUN_OUT/report.md" || fail "phase 1 report did not check ShipNeedsCoveredQa"
grep -q 'QaResultFromRun' "$RUN_OUT/report.md" || fail "phase 1 report did not check QaResultFromRun"
grep -q 'RebaseDecided' "$RUN_OUT/report.md" || fail "phase 1 report did not check RebaseDecided"
grep -q 'RebaseSpendsNoRound' "$RUN_OUT/report.md" || fail "phase 1 report did not check RebaseSpendsNoRound"
if grep -q ' breaks$' "$RUN_OUT/report.md"; then
  fail "phase 1 report contains a broken rule"
fi

echo "2/9 model with T9 enabled"
run_statechart "$root/model/OrchestratorAll.cfg"
expect_clean "T9-enabled" 0
grep -q 'T9Decided' "$RUN_OUT/report.md" || fail "T9-enabled report did not check T9Decided"
grep -q 'RebaseDecided' "$RUN_OUT/report.md" || fail "T9-enabled report did not check RebaseDecided"
grep -q 'RebaseSpendsNoRound' "$RUN_OUT/report.md" || fail "T9-enabled report did not check RebaseSpendsNoRound"
if grep -q ' breaks$' "$RUN_OUT/report.md"; then
  fail "T9-enabled report contains a broken rule"
fi

echo "3/9 phase 1 chart"
run_statechart "$root/model/chart.cfg"
expect_clean "phase 1 chart" 0
got=$(state_labels "$RUN_OUT/chart.mmd")
want=$(printf '%s\n' "$phase1_states" | sort)
[ "$got" = "$want" ] || fail "phase 1 chart states are not section 4.1 without post-merge"
if state_labels "$RUN_OUT/chart.mmd" | grep -qx 'post-merge'; then
  fail "phase 1 chart reached post-merge"
fi
grep -q 'kind=go' "$RUN_OUT/chart.mmd" || fail "phase 1 chart has no kind=go handoff"

echo "4/9 section 4.1 chart"
run_statechart "$root/model/chart-all.cfg"
expect_clean "section 4.1 chart" 0
got=$(state_labels "$RUN_OUT/chart.mmd")
want=$(printf '%s\n' "$section41" | sort)
[ "$got" = "$want" ] || fail "section 4.1 chart states do not match"
grep -q 'kind=go, queue build' "$RUN_OUT/chart.mmd" || fail "chart is missing kind=go build"
grep -q 'kind=go, queue plan' "$RUN_OUT/chart.mmd" || fail "chart is missing kind=go plan"
grep -q 'trusted builder posts kind=plan' "$RUN_OUT/chart.mmd" || fail "chart is missing kind=plan"
grep -q 'project has a post-merge check' "$RUN_OUT/chart.mmd" || fail "chart is missing T9"
grep -q 'Max hands it off' "$RUN_OUT/chart.mmd" || fail "chart is missing Max's hand-off"
grep -q 'plan over the size line with a split, file the parts' "$RUN_OUT/chart.mmd" || fail "chart is missing the plan auto-split"
grep -q 'trusted kind=rebase, rebase-only fix' "$RUN_OUT/chart.mmd" || fail "chart is missing kind=rebase"
drawn=$(mktemp)
add_ci_red_self_loops "$RUN_OUT/chart.mmd" >"$drawn"
diff -u "$root/model/chart.mmd" "$drawn" || fail "model/chart.mmd is not the section 4.1 chart"

echo "5/9 fixture omitting T1"
run_statechart "$root/model/fixtures/omit-t1.cfg"
expect_clean "omit-t1" 1
expect_break "omit-t1" T1KindGo questions

echo "6/9 fixture omitting T1P"
run_statechart "$root/model/fixtures/omit-t1p.cfg"
expect_clean "omit-t1p" 1
expect_break "omit-t1p" T1PDecided planning

echo "7/9 fixture dropping the covered guard on ship = merge"
run_statechart "$root/model/fixtures/omit-ship-guard.cfg"
expect_clean "omit-ship-guard" 1
expect_break "omit-ship-guard" ShipPinnedToCoveredHead merged

echo "8/9 fixture dropping the QA guard on ship = merge"
run_statechart "$root/model/fixtures/omit-qa-guard.cfg"
expect_clean "omit-qa-guard" 1
expect_break "omit-qa-guard" ShipNeedsCoveredQa merged

echo "9/9 fixture omitting TRB"
run_statechart "$root/model/fixtures/omit-trb.cfg"
expect_clean "omit-trb" 1
expect_break "omit-trb" RebaseDecided review

echo "statechart ok"
