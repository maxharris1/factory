#!/usr/bin/env bash
# Is factory-dashboard-http.py the live 8090 server, and restart it safely.
#
#   dashboard-http-service.sh status    exit 0 if factory-dashboard-http.py is
#                                       the live 8090 server, 3 if not
#   dashboard-http-service.sh restart   restart the HTTP unit and wait for
#                                       status; never a cutover
#
# Live means factory-dashboard-http.service is active, factory-dashboard.service
# is not, and the tailnet address answers with Server: factory-dashboard.
# The address is TAILNET_IP:DASHBOARD_PORT from the env file, parsed as systemd
# 255 parses an EnvironmentFile; nothing else from it is printed. restart
# refuses (exit 3) unless the HTTP unit is enabled and factory-dashboard.service
# is inactive or failed, and exits 1 if systemctl restart fails.
# Overrides: DASHBOARD_SYSTEMCTL, DASHBOARD_ENV_FILE, DASHBOARD_PROBE_ADDR,
# DASHBOARD_START_WAIT (seconds, default 15).
set -euo pipefail

HTTP_UNIT=factory-dashboard-http.service
STATIC_UNIT=factory-dashboard.service
SYSTEMCTL="${DASHBOARD_SYSTEMCTL:-systemctl}"
ENV_FILE="${DASHBOARD_ENV_FILE:-/etc/factory-dashboard/http.env}"
START_WAIT="${DASHBOARD_START_WAIT:-15}"

say() { printf '%s dashboard-http-service: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

unit_state() {
    local state
    state="$("$SYSTEMCTL" is-active "$1" 2>/dev/null || true)"
    printf '%s\n' "${state:-unknown}"
}

# A port of parse_env_file_internal and load_env_file_push from systemd 255
# src/basic/env-file.c, so the address is the one the unit really gets.
env_addr() {
    python3 -I -c '
import sys

NEWLINE, WHITESPACE, SHELL_NEED_ESCAPE = "\n\r", " \t\n\r", "\"\\`$"
VALUE_STATES = {"PRE_VALUE", "VALUE", "VALUE_ESCAPE", "SINGLE_QUOTE_VALUE",
                "DOUBLE_QUOTE_VALUE", "DOUBLE_QUOTE_VALUE_ESCAPE"}


def utf8(raw):
    text = raw.encode("latin-1").decode("utf-8")
    if any(0xFDD0 <= ord(c) <= 0xFDEF or ord(c) & 0xFFFE == 0xFFFE for c in text):
        raise ValueError("noncharacter")
    return text


def parse(data):
    env = {}
    state, key, value, key_ws, value_ws = "PRE_KEY", "", "", None, None

    def push(chomp):
        k = key if key_ws is None else key[:key_ws]
        v = value if not chomp or value_ws is None else value[:value_ws]
        env[utf8(k)] = utf8(v)

    if b"\0" in data:
        raise ValueError("NUL byte")
    for c in data.decode("latin-1"):
        if state == "PRE_KEY":
            if c in "#;":
                state = "COMMENT"
            elif c not in WHITESPACE:
                state, key, key_ws = "KEY", c, None
        elif state == "KEY":
            if c in NEWLINE:
                state, key = "PRE_KEY", ""
            elif c == "=":
                state, value, value_ws = "PRE_VALUE", "", None
            else:
                if c not in WHITESPACE:
                    key_ws = None
                elif key_ws is None:
                    key_ws = len(key)
                key += c
        elif state == "PRE_VALUE":
            if c in NEWLINE:
                push(False)
                state = "PRE_KEY"
            elif c == "\x27":
                state = "SINGLE_QUOTE_VALUE"
            elif c == "\"":
                state = "DOUBLE_QUOTE_VALUE"
            elif c == "\\":
                state = "VALUE_ESCAPE"
            elif c not in WHITESPACE:
                state, value = "VALUE", value + c
        elif state == "VALUE":
            if c in NEWLINE:
                push(True)
                state = "PRE_KEY"
            elif c == "\\":
                state, value_ws = "VALUE_ESCAPE", None
            else:
                if c not in WHITESPACE:
                    value_ws = None
                elif value_ws is None:
                    value_ws = len(value)
                value += c
        elif state == "VALUE_ESCAPE":
            state = "VALUE"
            if c not in NEWLINE:
                value += c
        elif state == "SINGLE_QUOTE_VALUE":
            if c == "\x27":
                state = "PRE_VALUE"
            else:
                value += c
        elif state == "DOUBLE_QUOTE_VALUE":
            if c == "\"":
                state = "PRE_VALUE"
            elif c == "\\":
                state = "DOUBLE_QUOTE_VALUE_ESCAPE"
            else:
                value += c
        elif state == "DOUBLE_QUOTE_VALUE_ESCAPE":
            state = "DOUBLE_QUOTE_VALUE"
            if c in SHELL_NEED_ESCAPE:
                value += c
            elif c != "\n":
                value += "\\" + c
        elif state == "COMMENT":
            if c == "\\":
                state = "COMMENT_ESCAPE"
            elif c in NEWLINE:
                state = "PRE_KEY"
        elif state == "COMMENT_ESCAPE":
            state = "PRE_KEY" if c in NEWLINE else "COMMENT"
    if state in VALUE_STATES:
        push(state == "VALUE")
    return env


try:
    with open(sys.argv[1], "rb") as f:
        env = parse(f.read())
except (OSError, ValueError):
    sys.exit(1)
ip, port = env.get("TAILNET_IP"), env.get("DASHBOARD_PORT")
if ip and port:
    sys.stdout.buffer.write(f"{ip}:{port}\n".encode())
' "$ENV_FILE" 2>/dev/null
}

probe_addr() {
    if [[ -n "${DASHBOARD_PROBE_ADDR:-}" ]]; then
        printf '%s\n' "$DASHBOARD_PROBE_ADDR"
        return
    fi
    env_addr || true
}

# http.client, not urllib, so a redirect or error status still counts.
server_header() {
    python3 -I -c '
import http.client, sys
try:
    conn = http.client.HTTPConnection(sys.argv[1], timeout=3)
    conn.request("GET", "/")
    print(conn.getresponse().getheader("Server", ""))
except (OSError, http.client.HTTPException, ValueError):
    sys.exit(1)
' "$1" 2>/dev/null
}

live_reason() {
    local state addr header product
    state="$(unit_state "$HTTP_UNIT")"
    if [[ "$state" != active ]]; then
        echo "${HTTP_UNIT} is ${state}"
        return
    fi
    if [[ "$(unit_state "$STATIC_UNIT")" == active ]]; then
        echo "${STATIC_UNIT} is active"
        return
    fi
    addr="$(probe_addr)"
    if [[ -z "$addr" ]]; then
        echo "cannot read TAILNET_IP and DASHBOARD_PORT from ${ENV_FILE}; run as root or set DASHBOARD_PROBE_ADDR"
        return
    fi
    if ! header="$(server_header "$addr")"; then
        echo "nothing answers at ${addr}"
        return
    fi
    read -r product _ <<<"$header" || true
    [[ "${product:-}" == factory-dashboard ]] ||
        echo "${addr} answers Server: ${header:-(none)}, not factory-dashboard"
}

status() {
    local reason
    reason="$(live_reason)"
    if [[ -n "$reason" ]]; then
        echo "not live: ${reason}"
        return 3
    fi
    echo "live: factory-dashboard-http.py serves $(probe_addr)"
}

restart() {
    local enabled static deadline
    enabled="$("$SYSTEMCTL" is-enabled "$HTTP_UNIT" 2>/dev/null || true)"
    if [[ "$enabled" != enabled ]]; then
        echo "dashboard-http-service: refusing to restart ${HTTP_UNIT}: it is ${enabled:-not enabled}. Starting it would stop ${STATIC_UNIT} through Conflicts= and take over 8090, which is a cutover; run the install step (#1503) with Max's go instead." >&2
        return 3
    fi
    static="$(unit_state "$STATIC_UNIT")"
    if [[ "$static" != inactive && "$static" != failed ]]; then
        echo "dashboard-http-service: refusing to restart ${HTTP_UNIT}: ${STATIC_UNIT} is ${static}. Restarting would stop it through Conflicts= and take over 8090, which is a cutover; run the install step (#1503) with Max's go instead." >&2
        return 3
    fi
    say "restarting ${HTTP_UNIT}"
    if ! "$SYSTEMCTL" restart "$HTTP_UNIT"; then
        say "systemctl restart ${HTTP_UNIT} failed" >&2
        status || true
        return 1
    fi
    deadline=$((SECONDS + START_WAIT))
    while ((SECONDS < deadline)) && [[ -n "$(live_reason)" ]]; do
        sleep 0.5
    done
    status
}

case "${1:-}" in
    status) status ;;
    restart) restart ;;
    *)
        echo "usage: $(basename "$0") {status|restart}" >&2
        exit 2
        ;;
esac
