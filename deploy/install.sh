#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: deploy/install.sh [--autostart] [--update]" >&2
    echo "  --autostart  also run deploy/poller-service.sh install-autostart, and" >&2
    echo "               deploy/linear-webhook-service.sh install-autostart when" >&2
    echo "               orch.yml (or orch.local.yml) has linear_webhook" >&2
    echo "  --update     self-deploy's re-run (#219): install requirements into the" >&2
    echo "               existing venv instead of recreating it, which running" >&2
    echo "               processes still use" >&2
}
AUTOSTART=0
UPDATE=0
for arg in "$@"; do
    case "$arg" in
        --autostart) AUTOSTART=1 ;;
        --update) UPDATE=1 ;;
        -h | --help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${HOME}/.local/share/factory/venv"
LINEAR_SERVICE="${HOME}/.local/share/factory/linear-webhook-service.sh"
HOST_PIDNS="${HOME}/.local/share/factory/host-pidns.sh"
HOST_PIDNS_RECORD="${HOME}/.config/factory/host-pidns"
DB="${HOME}/.local/state/factory/orch.db"
STATE="$(dirname "$DB")"
ORCH_CONFIG="${ORCH_CONFIG:-${ROOT}/orch.yml}"
LEGACY_DIR="${HOME}/.local/share/factory"
LEGACY=("${LEGACY_DIR}/run-poller.sh" "${LEGACY_DIR}/linear-webhook-service.sh")
PROC="${ORCH_PROC_DIR:-/proc}"
CRONTAB="${ORCH_CRONTAB:-crontab}"

# Print the pids whose argv runs exactly $1: as argv[0], or as argv[1] after
# bash or sh. Never a pattern match.
exact_pids() {
    local dir
    local -a argv
    [[ -d "$PROC" ]] || return 0
    for dir in "$PROC"/[0-9]*; do
        [[ -r "${dir}/cmdline" ]] || continue
        mapfile -d '' -t argv <"${dir}/cmdline" 2>/dev/null || continue
        ((${#argv[@]})) || continue
        if [[ "${argv[0]}" == "$1" ]] ||
            [[ "${argv[0]}" =~ (^|/)(ba)?sh$ && "${argv[1]:-}" == "$1" ]]; then
            printf '%s\n' "${dir##*/}"
        fi
    done
}

# Print the direct children of pid $1.
child_pids() {
    local dir stat ppid
    for dir in "$PROC"/[0-9]*; do
        stat="$(cat "${dir}/stat" 2>/dev/null)" || continue
        read -r _ ppid _ <<<"${stat##*) }"
        [[ "$ppid" == "$1" ]] && printf '%s\n' "${dir##*/}"
    done
    return 0
}

# Retire the launchers that lived outside the repo (#107): stop each legacy
# script's processes (exact argv only, then their direct children, so a
# parent never sees its child die and runs its next step), drop the
# crontab lines that name it, delete it. Changes nothing when the crontab
# cannot be read.
retire_legacy() {
    local path pid child children current="" kept errors status=0
    local -a pids
    if command -v "$CRONTAB" >/dev/null 2>&1; then
        errors="$(mktemp "${TMPDIR:-/tmp}/install.XXXXXX")"
        current="$("$CRONTAB" -l 2>"$errors")" || status=$?
        if ((status != 0)) && ! grep -q "no crontab for" "$errors"; then
            echo "install: cannot read the crontab (${CRONTAB} -l exited ${status}); legacy launchers left in place:" >&2
            cat "$errors" >&2
            rm -f "$errors"
            return 1
        fi
        rm -f "$errors"
        ((status == 0)) || current=""
    fi
    for path in "${LEGACY[@]}"; do
        [[ -e "$path" ]] || continue
        # Since #134 the listener path holds the committed service script's
        # installed copy; only a launcher that is not that copy is legacy.
        if [[ "$path" == */linear-webhook-service.sh ]] && grep -q -- '_listener-loop' "$path" 2>/dev/null; then
            continue
        fi
        if [[ "$path" == */linear-webhook-service.sh && -x "$path" ]]; then
            timeout 90 "$path" stop >/dev/null 2>&1 || true
        fi
        mapfile -t pids < <(exact_pids "$path")
        for pid in "${pids[@]}"; do
            children="$(child_pids "$pid")"
            kill -TERM "$pid" 2>/dev/null && echo "stopped legacy ${path} (pid ${pid})"
            for child in $children; do
                kill -TERM "$child" 2>/dev/null || true
            done
        done
        rm -f "$path"
        echo "removed legacy launcher ${path}"
    done
    kept="$current"
    for path in "${LEGACY[@]}"; do
        kept="$(grep -vF -- "$path" <<<"$kept" || true)"
    done
    if [[ "$kept" != "$current" ]]; then
        if [[ -n "$kept" ]]; then
            printf '%s\n' "$kept" | "$CRONTAB" -
        else
            "$CRONTAB" - </dev/null
        fi
        echo "removed crontab lines naming the legacy launchers"
    fi
}

mkdir -p "${HOME}/.local/bin" "${HOME}/.config/factory" "$STATE" "$(dirname "$LINEAR_SERVICE")"
if [[ "$ROOT" != "${HOME}/factory" ]]; then
    echo "warning: expected the production checkout at ${HOME}/factory; using ${ROOT}" >&2
fi
if git -C "$ROOT" symbolic-ref --quiet --short HEAD >/dev/null 2>&1 &&
    [[ "$(git -C "$ROOT" symbolic-ref --quiet --short HEAD)" != "main" ]]; then
    echo "warning: production installs should use the main branch" >&2
fi
WRAPPER="${HOME}/.local/bin/orch"
if [[ -e "$WRAPPER" ]] && ! grep -q '^# factory-orch-wrapper$' "$WRAPPER"; then
    echo "refusing to overwrite non-factory wrapper at ${WRAPPER}" >&2
    exit 1
fi
record_host_pidns() {
    # shellcheck source=deploy/host-pidns.sh
    . "${ROOT}/deploy/host-pidns.sh"
    [[ -r "${PROC}/self/status" ]] || return 0
    if ! factory_may_autostart /dev/null; then
        echo "warning: not recording the host PID namespace from here; kept ${HOST_PIDNS_RECORD}" >&2
        return 0
    fi
    readlink "${PROC}/self/ns/pid" >"$HOST_PIDNS_RECORD"
}
KEEP_VENV=0
[[ "$UPDATE" == 1 && -x "${VENV}/bin/python" ]] && KEEP_VENV=1
if command -v uv >/dev/null 2>&1; then
    [[ "$KEEP_VENV" == 1 ]] || UV_VENV_CLEAR=1 uv venv "$VENV"
    uv pip install --python "${VENV}/bin/python" -r "${ROOT}/requirements.txt"
else
    [[ "$KEEP_VENV" == 1 ]] || python3 -m venv --clear "$VENV"
    "${VENV}/bin/python" -m pip install -r "${ROOT}/requirements.txt"
fi
cat >"$WRAPPER" <<EOF
#!/usr/bin/env bash
# factory-orch-wrapper
exec "${VENV}/bin/python" "${ROOT}/bin/orch" "\$@"
EOF
chmod 0755 "$WRAPPER"
# Retire legacy launchers first: the listener's legacy path is where the
# service script's copy goes.
retire_legacy
# A copy, not the checkout's file: a git pull must not rewrite a script that
# a running loop is still reading.
install -m 0755 "${ROOT}/deploy/linear-webhook-service.sh" "$LINEAR_SERVICE"
install -m 0644 "${ROOT}/deploy/host-pidns.sh" "$HOST_PIDNS"
record_host_pidns
install -m 0600 "${ROOT}/deploy/webhook.env.example" "${HOME}/.config/factory/webhook.env.example"
cat >"${HOME}/.config/factory/profile.sh" <<EOF
# Sourced once per shell: a second source (.profile and .bashrc) is a no-op.
if [ -z "\${FACTORY_PROFILE_LOADED:-}" ]; then
FACTORY_PROFILE_LOADED=1
export ORCH_DB="${DB}"
export PATH="\${HOME}/.local/bin:\${PATH}"
mkdir -p "${STATE}"
# Poller autostart (deploy/poller-service.sh install-autostart): with no
# systemd user session, each bot shell makes sure the poller is up.
# The state directory can be lost (#66); make it before logging into it.
mkdir -p "$(dirname "$DB")"
if [ -r "${HOST_PIDNS}" ] && . "${HOST_PIDNS}" && factory_may_autostart 2>/dev/null; then
if [ -f "\${HOME}/.config/factory/poller-autostart" ] && [ -x "${ROOT}/deploy/poller-service.sh" ]; then
    ("${ROOT}/deploy/poller-service.sh" ensure >>"${STATE}/poller-service.log" 2>&1 &)
fi
# Waiter autostart (deploy/waiter-service.sh <bot> install-autostart, #80):
# each bot shell makes sure that bot's waiter supervisor is up.
for factory_waiter_flag in "\${HOME}/.config/factory"/waiter-autostart-*; do
    [ -f "\$factory_waiter_flag" ] || continue
    factory_waiter_bot="\${factory_waiter_flag##*/waiter-autostart-}"
    case "\$factory_waiter_bot" in claude-bot | codex-bot | cursor-bot) ;; *) continue ;; esac
    [ -x "${ROOT}/deploy/waiter-service.sh" ] || continue
    ("${ROOT}/deploy/waiter-service.sh" "\$factory_waiter_bot" ensure >>"${STATE}/waiter-\${factory_waiter_bot}-service.log" 2>&1 &)
done
unset factory_waiter_flag factory_waiter_bot
# Linear webhook autostart (linear-webhook-service.sh install-autostart, #134):
# each bot shell makes sure the listener and tunnel loops are up.
if [ -f "\${HOME}/.config/factory/linear-webhook-autostart" ] && [ -x "${LINEAR_SERVICE}" ]; then
    ("${LINEAR_SERVICE}" ensure >>"${STATE}/linear-webhook-service.log" 2>&1 &)
fi
# Self-heal loop (deploy/self-heal.sh install-autostart, #107), where no
# systemd user timer runs it.
if [ -f "\${HOME}/.config/factory/self-heal-autostart" ] && [ -x "${ROOT}/deploy/self-heal.sh" ]; then
    ("${ROOT}/deploy/self-heal.sh" ensure >>"${STATE}/self-heal-service.log" 2>&1 &)
fi
fi
fi
EOF
ORCH_DB="$DB" "$WRAPPER" init-db --orch "$ORCH_CONFIG"
if [[ "$AUTOSTART" == 1 ]]; then
    "${ROOT}/deploy/poller-service.sh" install-autostart
    if grep -qE '^linear_webhook:' "$ORCH_CONFIG" "${ORCH_CONFIG%.yml}.local.yml" 2>/dev/null; then
        "${LINEAR_SERVICE}" install-autostart
    fi
fi
"${ROOT}/deploy/self-heal.sh" install-autostart
echo "Source ${HOME}/.config/factory/profile.sh in every bot shell."
echo "Then run: orch doctor-runtime"
