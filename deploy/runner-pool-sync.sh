#!/usr/bin/env bash
# #711: publishes the online factory-linux runner count as the repo Actions variable FACTORY_LINUX_POOL
# ({"online":N}), writing only when a changed count holds for two checks in a row.
# Runs on the orch host (the PC) as the `factory` user, from the deployed checkout, reading runners
# with that user's own gh (repo admin). `runner-pool-sync.sh ensure` from that user's crontab starts
# the loop unless a running loop holds the lock. Hold: touch ~/.config/factory/runner-pool-sync-hold
set -uo pipefail
usage() {
  cat <<'EOF'
usage: runner-pool-sync.sh ensure|loop
Publishes the online factory-linux runner count as the repo Actions variable FACTORY_LINUX_POOL
({"online":N}), writing only when a changed count holds for two checks in a row.
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
VAR=FACTORY_LINUX_POOL
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
cur=$(exec 9>&-; gh_ api "repos/$REPO/actions/variables/$VAR" --jq .value 2>/dev/null) || cur=
prev=
while :; do
  [ -e "$HOME/.config/factory/runner-pool-sync-hold" ] && exit 0
  if online=$(exec 9>&-; gh_ api "repos/$REPO/actions/runners?per_page=100" --jq '[.runners[] | select(any(.labels[]; .name=="factory-linux")) | select(.status=="online")] | length' 2>/dev/null) && [[ $online =~ ^[0-9]+$ ]]; then
    want="{\"online\":$online}"
    if [ "$want" != "$cur" ] && { [ -z "$cur" ] || [ "$online" = "$prev" ]; }; then
      set_var "$want" && { printf '%(%FT%T%z)T online=%s -> %s\n' -1 "$online" "$want"; cur=$want; }
    fi
    prev=$online
  else
    printf '%(%FT%T%z)T runner list failed; leaving %s=%s\n' -1 "$VAR" "$cur"
    prev=
  fi
  sleep "$EVERY" 9>&-
done
