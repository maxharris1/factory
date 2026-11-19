#!/usr/bin/env bash
# #711 overflow: keep the repo Actions variable FACTORY_LINUX_RUNNER pointed at the
# self-hosted factory-linux pool while any of it is idle, and at ubuntu-latest only
# when every online pool runner has been busy (or none is online) for 2 checks in a row.
# Workflows read it as:
#   runs-on: ${{ fromJSON(vars.FACTORY_LINUX_RUNNER || '["self-hosted","Linux","X64","factory-linux"]') }}
# Runs on the orch host (the PC) as the `factory` user, from the deployed checkout, reading runners
# with that user's own gh (repo admin). `runner-pool-sync.sh ensure` from that user's crontab starts
# the loop unless a running loop holds the lock. Hold: touch ~/.config/factory/runner-pool-sync-hold
set -uo pipefail
usage() {
  cat <<'EOF'
usage: runner-pool-sync.sh ensure|loop
Points FACTORY_LINUX_RUNNER at the factory-linux pool, or at ubuntu-latest while the pool is full.
Runs on the orch host (the PC) as the `factory` user, started by `ensure` from that user's crontab.
  ensure  start the loop in its own session unless a running loop holds the lock
  loop    check the runners every $EVERY seconds (default 30) until the hold file exists
Hold: touch ~/.config/factory/runner-pool-sync-hold
EOF
}
case ${1:-} in
  ensure | loop) ;;
  -h | --help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
REPO=${REPO:-maxharris1/factory}
VAR=FACTORY_LINUX_RUNNER
POOL='["self-hosted","Linux","X64","factory-linux"]'
HOSTED='"ubuntu-latest"'
EVERY=${EVERY:-30}
LOG=${LOG:-$HOME/.local/state/factory/runner-pool-sync.log}
LOCK=$HOME/.local/state/factory/runner-pool-sync.lock
mkdir -p "$(dirname "$LOG")" "$(dirname "$LOCK")"
if [ "${1:-}" = ensure ]; then
  [ -e "$HOME/.config/factory/runner-pool-sync-hold" ] && exit 0
  flock -n "$LOCK" true || exit 0
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$0" loop >>"$LOG" 2>&1 </dev/null &
  else
    nohup "$0" loop >>"$LOG" 2>&1 </dev/null &
  fi
  for _ in $(seq 500); do
    flock -n "$LOCK" true || exit 0
    kill -0 $! 2>/dev/null || exit 0
    sleep 0.01
  done
  echo "runner-pool-sync: the loop did not take $LOCK" >&2
  exit 1
fi
exec 9>>"$LOCK"
flock -w 2 9 || exit 0
gh_() { env -u GH_TOKEN gh "$@" 9>&-; }
set_var() {
  if gh_ api "repos/$REPO/actions/variables/$VAR" >/dev/null 2>&1; then
    gh_ api -X PATCH "repos/$REPO/actions/variables/$VAR" -f name="$VAR" -f value="$1" >/dev/null
  else
    gh_ api -X POST "repos/$REPO/actions/variables" -f name="$VAR" -f value="$1" >/dev/null
  fi
}
cur=$(exec 9>&-; gh_ api "repos/$REPO/actions/variables/$VAR" --jq .value 2>/dev/null || echo "")
full=0
while :; do
  [ -e "$HOME/.config/factory/runner-pool-sync-hold" ] && exit 0
  if counts=$(exec 9>&-; gh_ api "repos/$REPO/actions/runners?per_page=100" --jq '[.runners[] | select(any(.labels[]; .name=="factory-linux")) | select(.status=="online")] | "\(length) \(map(select(.busy|not))|length)"' 2>/dev/null); then
    read -r online idle <<<"$counts"
    if [ "$idle" -gt 0 ]; then full=0; want=$POOL; else full=$((full+1)); want=$cur; [ $full -ge 2 ] && want=$HOSTED; fi
    [ -z "$want" ] && want=$POOL
    if [ "$want" != "$cur" ]; then
      set_var "$want" && { printf '%(%FT%T%z)T online=%s idle=%s -> %s\n' -1 "$online" "$idle" "$want"; cur=$want; }
    fi
  else
    printf '%(%FT%T%z)T runner list failed; leaving %s=%s\n' -1 "$VAR" "$cur"
  fi
  sleep "$EVERY" 9>&-
done
