#!/usr/bin/env bash
# Cut 8090 over to factory-dashboard-http.py, check it is live, restart it.
#
#   dashboard-http-service.sh install   copy the app and unit, write the
#                                       BindPaths drop-in, move 8090 from
#                                       factory-dashboard.service to the HTTP
#                                       unit; revert if it does not go live
#   dashboard-http-service.sh status    is factory-dashboard-http.py the live
#                                       8090 server
#   dashboard-http-service.sh restart   restart the HTTP unit and wait for
#                                       status; never a cutover
#   dashboard-http-service.sh rollback  move 8090 back to
#                                       factory-dashboard.service and check
#                                       python3 -m http.server answers
#
# Exit codes: 0 live, or for rollback the static server answers; 1 cutover
# failed and reverted, a systemctl call failed, or rollback refused; 3 not
# live, or for rollback the units switched but the static server is not
# confirmed; 78 the env file is incomplete; 2 usage.
#
# Live means factory-dashboard-http.service is active, factory-dashboard.service
# is not, and the tailnet address answers with Server: factory-dashboard.
# The env file is parsed as systemd 255 parses an EnvironmentFile. Only
# TAILNET_IP, DASHBOARD_PORT and the path keys are read from it, and only the
# address and the paths (in the drop-in) leave it. install refuses (exit 78)
# before changing anything if a required key is missing or a path is unsafe.
# restart refuses (exit 3) unless the HTTP unit is enabled and
# factory-dashboard.service is inactive or failed.
# Overrides: DASHBOARD_SYSTEMCTL, DASHBOARD_ENV_FILE, DASHBOARD_PROBE_ADDR,
# DASHBOARD_START_WAIT (seconds, default 15), DASHBOARD_UNIT_DIR,
# DASHBOARD_LIB_DIR.
set -euo pipefail

HTTP_UNIT=factory-dashboard-http.service
STATIC_UNIT=factory-dashboard.service
SYSTEMCTL="${DASHBOARD_SYSTEMCTL:-systemctl}"
ENV_FILE="${DASHBOARD_ENV_FILE:-/etc/factory-dashboard/http.env}"
START_WAIT="${DASHBOARD_START_WAIT:-15}"
UNIT_DIR="${DASHBOARD_UNIT_DIR:-/etc/systemd/system}"
LIB_DIR="${DASHBOARD_LIB_DIR:-/usr/local/lib/factory-dashboard}"
SOURCE_DIR="$(dirname "$0")"
APP=factory-dashboard-http.py

say() { printf '%s dashboard-http-service: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

unit_state() {
    local state
    state="$("$SYSTEMCTL" is-active "$1" 2>/dev/null || true)"
    printf '%s\n' "${state:-unknown}"
}

# A port of parse_env_file_internal and load_env_file_push from systemd 255
# src/basic/env-file.c, so the value is the one the unit really gets.
env_value() {
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
if env.get(sys.argv[2]):
    sys.stdout.buffer.write(f"{env[sys.argv[2]]}\n".encode())
' "$ENV_FILE" "$1" 2>/dev/null
}

env_addr() {
    local ip port
    ip="$(env_value TAILNET_IP)" && port="$(env_value DASHBOARD_PORT)" || return 1
    if [[ -n "$ip" && -n "$port" ]]; then
        printf '%s:%s\n' "$ip" "$port"
    fi
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

wait_live() {
    local deadline=$((SECONDS + START_WAIT))
    while ((SECONDS < deadline)) && [[ -n "$(live_reason)" ]]; do
        sleep 0.5
    done
}

response_status() {
    python3 -I -c '
import http.client, sys
try:
    conn = http.client.HTTPConnection(sys.argv[1], timeout=3)
    conn.request("GET", sys.argv[2])
    print(conn.getresponse().status)
except (OSError, http.client.HTTPException, ValueError):
    sys.exit(1)
' "$1" "$2" 2>/dev/null
}

static_reason() {
    local state addr header product
    state="$(unit_state "$STATIC_UNIT")"
    if [[ "$state" != active ]]; then
        echo "${STATIC_UNIT} is ${state}"
        return
    fi
    if [[ "$(unit_state "$HTTP_UNIT")" == active ]]; then
        echo "${HTTP_UNIT} is active"
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
    if [[ "${product%%/*}" != SimpleHTTP ]]; then
        echo "${addr} answers Server: ${header:-(none)}, not SimpleHTTP"
        return
    fi
    [[ "$(response_status "$addr" /api/session)" != 200 ]] ||
        echo "${addr} answers 200 on /api/session, so Settings is not read-only"
}

wait_static() {
    local deadline=$((SECONDS + START_WAIT))
    while ((SECONDS < deadline)) && [[ -n "$(static_reason)" ]]; do
        sleep 0.5
    done
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
    local enabled static
    enabled="$("$SYSTEMCTL" is-enabled "$HTTP_UNIT" 2>/dev/null || true)"
    if [[ "$enabled" != enabled ]]; then
        echo "dashboard-http-service: refusing to restart ${HTTP_UNIT}: it is ${enabled:-not enabled}. Starting it would stop ${STATIC_UNIT} through Conflicts= and take over 8090, which is a cutover; run dashboard-http-service.sh install with Max's go instead." >&2
        return 3
    fi
    static="$(unit_state "$STATIC_UNIT")"
    if [[ "$static" != inactive && "$static" != failed ]]; then
        echo "dashboard-http-service: refusing to restart ${HTTP_UNIT}: ${STATIC_UNIT} is ${static}. Restarting would stop it through Conflicts= and take over 8090, which is a cutover; run dashboard-http-service.sh install with Max's go instead." >&2
        return 3
    fi
    say "restarting ${HTTP_UNIT}"
    if ! "$SYSTEMCTL" restart "$HTTP_UNIT"; then
        say "systemctl restart ${HTTP_UNIT} failed" >&2
        status || true
        return 1
    fi
    wait_live
    status
}

refuse() {
    echo "dashboard-http-service: refusing to install: $*" >&2
}

placeholder() {
    case "$1" in
        TAILNET_IP) echo '<tailscale ip -4>' ;;
        DASHBOARD_DIR) echo '<pushed dashboard directory>' ;;
        DASHBOARD_PORT) echo 8090 ;;
    esac
}

# Systemd reads ':' in a Bind*Paths= value as src:dst and '%' as a specifier,
# and a newline would start a new unit line. LC_ALL=C so [A-Za-z] matches only
# ASCII letters.
safe_path() {
    local LC_ALL=C
    [[ "$1" =~ ^(/[A-Za-z0-9._@+-]+)+$ && "$1/" != */./* && "$1/" != */../* ]]
}

paths_conf() {
    local key value bind missing=()
    if [[ ! -e "$ENV_FILE" ]]; then
        refuse "${ENV_FILE} does not exist. Create it (owner root, group factory-dash, mode 0640) with:"
        for key in TAILNET_IP DASHBOARD_DIR DASHBOARD_PORT; do
            echo "  ${key}=$(placeholder "$key")" >&2
        done
        return 78
    fi
    if [[ ! -r "$ENV_FILE" ]]; then
        refuse "cannot read ${ENV_FILE}; run as root"
        return 78
    fi
    for key in TAILNET_IP DASHBOARD_DIR DASHBOARD_PORT; do
        if ! value="$(env_value "$key")"; then
            refuse "cannot parse ${ENV_FILE} as a systemd EnvironmentFile"
            return 78
        fi
        [[ -n "$value" ]] || missing+=("$key")
    done
    if ((${#missing[@]})); then
        refuse "${ENV_FILE} lacks these lines:"
        for key in "${missing[@]}"; do
            echo "  ${key}=$(placeholder "$key")" >&2
        done
        return 78
    fi
    echo "[Service]"
    for key in DASHBOARD_DIR DASHBOARD_SETTINGS_SPOOL DASHBOARD_SIGNIN_RELAY DASHBOARD_ACTIONS_AUDIT DASHBOARD_SESSION_STORE; do
        value="$(env_value "$key")"
        [[ -n "$value" ]] || continue
        bind="$value"
        [[ "$key" != DASHBOARD_ACTIONS_AUDIT ]] || bind="$(dirname "$value")"
        if ! safe_path "$value" || ! safe_path "$bind"; then
            refuse "${key} in ${ENV_FILE} must be an absolute path below / of only letters, digits and . _ @ + - with no . or .. part"
            return 78
        fi
        case "$key" in
            DASHBOARD_DIR)
                if [[ ! -d "$value" ]]; then
                    refuse "${key} in ${ENV_FILE} is not a directory"
                    return 78
                fi
                echo "BindReadOnlyPaths=${bind}"
                ;;
            *) echo "BindPaths=${bind}" ;;
        esac
    done
}

install_service() {
    local conf dashboard_dir drop_in tmp reason
    conf="$(paths_conf)" || return
    dashboard_dir="$(env_value DASHBOARD_DIR)"

    if [[ -z "$(getent passwd factory-dash || true)" ]]; then
        say "adding system user factory-dash"
        useradd --system --no-create-home --shell /usr/sbin/nologin factory-dash
    fi
    chmod g+rx "$dashboard_dir"

    drop_in="${UNIT_DIR}/${HTTP_UNIT}.d"
    mkdir -p "$LIB_DIR" "$drop_in"
    install -m 0644 "${SOURCE_DIR}/${APP}" "${LIB_DIR}/${APP}"
    install -m 0644 "${SOURCE_DIR}/${HTTP_UNIT}" "${UNIT_DIR}/${HTTP_UNIT}"
    tmp="$(mktemp "${drop_in}/.paths.conf.XXXXXX")"
    printf '%s\n' "$conf" >"$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "${drop_in}/paths.conf"

    say "moving 8090 from ${STATIC_UNIT} to ${HTTP_UNIT}"
    if ! "$SYSTEMCTL" daemon-reload; then
        reason="systemctl daemon-reload failed"
    elif ! "$SYSTEMCTL" disable "$STATIC_UNIT"; then
        reason="systemctl disable ${STATIC_UNIT} failed"
    elif ! "$SYSTEMCTL" enable "$HTTP_UNIT"; then
        reason="systemctl enable ${HTTP_UNIT} failed"
    elif ! "$SYSTEMCTL" restart "$HTTP_UNIT"; then
        reason="systemctl restart ${HTTP_UNIT} failed"
    else
        wait_live
        reason="$(live_reason)"
    fi
    if [[ -z "$reason" ]]; then
        status
        return
    fi
    say "cutover failed: ${reason}; reverting to ${STATIC_UNIT}" >&2
    "$SYSTEMCTL" disable --now "$HTTP_UNIT" || true
    "$SYSTEMCTL" enable --now "$STATIC_UNIT" ||
        say "systemctl enable --now ${STATIC_UNIT} failed; 8090 may be down" >&2
    if command -v journalctl >/dev/null; then
        journalctl -u "$HTTP_UNIT" -n 50 --no-pager >&2 || true
    fi
    return 1
}

rollback() {
    local reason
    if [[ ! -e "${UNIT_DIR}/${STATIC_UNIT}" ]]; then
        echo "dashboard-http-service: refusing to roll back: ${UNIT_DIR}/${STATIC_UNIT} does not exist, so nothing would serve 8090" >&2
        return 1
    fi
    if ! "$SYSTEMCTL" enable --now "$STATIC_UNIT"; then
        say "systemctl enable --now ${STATIC_UNIT} failed; ${HTTP_UNIT} still serves 8090" >&2
        return 1
    fi
    if ! "$SYSTEMCTL" disable --now "$HTTP_UNIT"; then
        say "systemctl disable --now ${HTTP_UNIT} failed; ${STATIC_UNIT} is up but ${HTTP_UNIT} is still enabled" >&2
        return 1
    fi
    wait_static
    reason="$(static_reason)"
    if [[ -n "$reason" ]]; then
        echo "not rolled back: ${reason}"
        return 3
    fi
    echo "rolled back: python3 -m http.server serves $(probe_addr); Settings is read-only"
}

case "${1:-}" in
    install) install_service ;;
    status) status ;;
    restart) restart ;;
    rollback) rollback ;;
    *)
        echo "usage: $(basename "$0") {install|status|restart|rollback}" >&2
        exit 2
        ;;
esac
