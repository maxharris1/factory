#!/usr/bin/env bash
# Token broker (issue #171): keep each bot's GitHub App installation token
# fresh, on a box timer, for that bot's harness user and remote devices.
#
#   app-token-service.sh once               one pass of lib/github_app_token.py refresh
#   app-token-service.sh install-autostart  run `once` every 5 minutes (box user)
#   app-token-service.sh remove-autostart   turn that off
#   app-token-service.sh install-harness DIR
#                                           as a harness user: route that user's
#                                           gh and git through the tokens in DIR
#
# Run once, install-autostart and remove-autostart as the box user, which
# holds the App keys. `once` mints a token per App and installation only
# when the stored one is missing or within 15 minutes of expiry, writes it to
# ~/.local/state/factory/app-tokens/<bot>/<owner>.json readable by that
# bot's box harness user alone, and sends each new token to every enabled
# remote device in machines.yml that runs the bot's harness. It prints bot,
# account and expiry; never a token. Log: app-token-service.log in the state
# dir.
#
# Autostart: where `systemctl --user` works, install-autostart writes
# deploy/factory-app-token.service and .timer to ~/.config/systemd/user and
# enables the timer. Otherwise it writes ~/.config/factory/app-token-autostart,
# and each self-heal pass (deploy/self-heal.sh, every 2 minutes) runs `once`.
#
# install-harness DIR (the box: ~box/.local/state/factory/app-tokens/<bot>;
# a remote device: ~/.config/factory/app-tokens) writes ~/.local/bin/gh,
# which runs the real gh with GH_TOKEN set from DIR for that one call, and
# sets this user's git credential helpers for https://github.com to that one
# helper reading DIR (an empty entry first drops any helper set before it).
# It refuses to replace a ~/.local/bin/gh it did not write.
set -euo pipefail

COMMAND="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${HERE}/$(basename "${BASH_SOURCE[0]}")"
CHECKOUT="$(dirname "$HERE")"
STATE="${ORCH_STATE_DIR:-${HOME}/.local/state/factory}"
CONFIG="${HOME}/.config/factory"
LOG="${STATE}/app-token-service.log"
RUN_LOCK="${STATE}/app-token.lock"
AUTOSTART="${CONFIG}/app-token-autostart"
VENV_PYTHON="${ORCH_PYTHON:-${HOME}/.local/share/factory/venv/bin/python}"
ORCH_CONFIG="${ORCH_CONFIG:-${CHECKOUT}/orch.yml}"
MACHINES="${ORCH_MACHINES:-${CHECKOUT}/machines.yml}"
TOKEN_ROOT="${ORCH_APP_TOKEN_ROOT:-${STATE}/app-tokens}"
SYSTEMCTL="${ORCH_SYSTEMCTL:-systemctl}"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
TIMER="factory-app-token.timer"
MARKER="# factory-app-token-gh"

usage() {
    echo "usage: $(basename "$0") {once|install-autostart|remove-autostart|install-harness DIR}" >&2
    exit 2
}

have_systemd_user() {
    command -v "$SYSTEMCTL" >/dev/null 2>&1 && "$SYSTEMCTL" --user show-environment >/dev/null 2>&1
}

once() {
    mkdir -p "$STATE"
    if command -v flock >/dev/null 2>&1; then
        exec 8>>"$RUN_LOCK"
        flock -n 8 || return 0
    fi
    cd "$CHECKOUT"
    printf '%s app-token pass\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$LOG"
    "$VENV_PYTHON" -m lib.github_app_token refresh --orch "$ORCH_CONFIG" \
        --machines "$MACHINES" --token-root "$TOKEN_ROOT" >>"$LOG" 2>&1 8>&-
}

install_autostart() {
    local unit
    mkdir -p "$CONFIG" "$STATE"
    if have_systemd_user; then
        mkdir -p "$UNIT_DIR"
        for unit in factory-app-token.service factory-app-token.timer; do
            sed "s|%h/factory/|${CHECKOUT}/|g" "${HERE}/${unit}" >"${UNIT_DIR}/${unit}"
        done
        "$SYSTEMCTL" --user daemon-reload
        "$SYSTEMCTL" --user enable --now "$TIMER"
        rm -f "$AUTOSTART"
        echo "token broker on: systemd user timer ${TIMER} runs '${SELF} once' every 5 minutes"
        return 0
    fi
    : >"$AUTOSTART"
    echo "token broker on: each self-heal pass runs '${SELF} once' (${AUTOSTART})"
}

remove_autostart() {
    if have_systemd_user; then
        "$SYSTEMCTL" --user disable --now "$TIMER" >/dev/null 2>&1 || true
        echo "disabled ${TIMER}"
    fi
    rm -f "$AUTOSTART"
    echo "token broker off"
}

plain_path() {
    [[ "$1" =~ ^/[A-Za-z0-9/._-]+$ ]] || {
        echo "app-token: refusing to write ${1} into a wrapper" >&2
        exit 1
    }
}

install_harness() {
    local dir="${1:-}" wrapper="${HOME}/.local/bin/gh" real helper
    [[ -n "$dir" ]] || usage
    plain_path "$dir"
    plain_path "$CHECKOUT"
    if [[ -e "$wrapper" ]] && ! grep -qF -- "$MARKER" "$wrapper"; then
        echo "refusing to overwrite non-factory ${wrapper}" >&2
        exit 1
    fi
    real="${ORCH_REAL_GH:-}"
    if [[ -z "$real" ]]; then
        real="$(PATH="$(sed "s|${HOME}/.local/bin:||g; s|:${HOME}/.local/bin||g" <<<"$PATH")" command -v gh || true)"
    fi
    [[ -n "$real" && -x "$real" ]] || {
        echo "app-token: no gh found outside ${HOME}/.local/bin" >&2
        exit 1
    }
    plain_path "$real"
    mkdir -p "${HOME}/.local/bin"
    cat >"$wrapper" <<EOF
#!/bin/sh
${MARKER}
exec python3 -I ${CHECKOUT}/lib/github_app_token.py gh --dir ${dir} --gh ${real} -- "\$@"
EOF
    chmod 0755 "$wrapper"
    helper="!python3 -I ${CHECKOUT}/lib/github_app_token.py credential --dir ${dir}"
    git config --global --unset-all credential.https://github.com.helper >/dev/null 2>&1 || true
    git config --global --add credential.https://github.com.helper ""
    git config --global --add credential.https://github.com.helper "$helper"
    git config --global credential.https://github.com.useHttpPath true
    echo "gh (${wrapper}) and git now read App tokens from ${dir}"
}

case "$COMMAND" in
    once) once ;;
    install-autostart) install_autostart ;;
    remove-autostart) remove_autostart ;;
    install-harness) install_harness "${2:-}" ;;
    *) usage ;;
esac
