#!/usr/bin/env bash
# #711 overflow: keep the repo Actions variable FACTORY_LINUX_RUNNER pointed at the
# self-hosted factory-linux pool while any of it is idle, and at ubuntu-latest only
# when every online pool runner has been busy (or none is online) for 2 checks in a row.
# Workflows read it as:
#   runs-on: ${{ fromJSON(vars.FACTORY_LINUX_RUNNER || '["self-hosted","Linux","X64","factory-linux"]') }}
# Runs on the box as the orch login (needs repo admin to read runners). Hold: touch ~/.config/factory/runner-pool-sync-hold
set -uo pipefail
REPO=${REPO:-maxharris1/factory}
VAR=FACTORY_LINUX_RUNNER
POOL='["self-hosted","Linux","X64","factory-linux"]'
HOSTED='"ubuntu-latest"'
EVERY=${EVERY:-30}
LOG=${LOG:-$HOME/.local/state/factory/runner-pool-sync.log}
PIDF=$HOME/.local/state/factory/runner-pool-sync.pid
mkdir -p "$(dirname "$LOG")"
if [ "${1:-}" = ensure ]; then
  [ -e "$HOME/.config/factory/runner-pool-sync-hold" ] && exit 0
  [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null && exit 0
  nohup "$0" loop >>"$LOG" 2>&1 </dev/null & echo $! > "$PIDF"; exit 0
fi
gh_() { env -u GH_TOKEN gh "$@"; }
set_var() {
  if gh_ api "repos/$REPO/actions/variables/$VAR" >/dev/null 2>&1; then
    gh_ api -X PATCH "repos/$REPO/actions/variables/$VAR" -f name="$VAR" -f value="$1" >/dev/null
  else
    gh_ api -X POST "repos/$REPO/actions/variables" -f name="$VAR" -f value="$1" >/dev/null
  fi
}
cur=$(gh_ api "repos/$REPO/actions/variables/$VAR" --jq .value 2>/dev/null || echo "")
full=0
while :; do
  [ -e "$HOME/.config/factory/runner-pool-sync-hold" ] && exit 0
  if counts=$(gh_ api "repos/$REPO/actions/runners?per_page=100" --jq '[.runners[] | select(any(.labels[]; .name=="factory-linux")) | select(.status=="online")] | "\(length) \(map(select(.busy|not))|length)"' 2>/dev/null); then
    read -r online idle <<<"$counts"
    if [ "$idle" -gt 0 ]; then full=0; want=$POOL; else full=$((full+1)); want=$cur; [ $full -ge 2 ] && want=$HOSTED; fi
    [ -z "$want" ] && want=$POOL
    if [ "$want" != "$cur" ]; then
      set_var "$want" && { echo "$(date -Is) online=$online idle=$idle -> $want"; cur=$want; }
    fi
  else
    echo "$(date -Is) runner list failed; leaving $VAR=$cur"
  fi
  sleep "$EVERY"
done
