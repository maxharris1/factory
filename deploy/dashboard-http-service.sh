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
# The address is TAILNET_IP:DASHBOARD_PORT, read from the env file as plain
# lines; nothing else in it is read. restart refuses unless the HTTP unit is
# enabled.
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

env_value() {
    grep -m 1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true
}

probe_addr() {
    local ip port
    if [[ -n "${DASHBOARD_PROBE_ADDR:-}" ]]; then
        printf '%s\n' "$DASHBOARD_PROBE_ADDR"
        return
    fi
    ip="$(env_value TAILNET_IP)"
    port="$(env_value DASHBOARD_PORT)"
    [[ -n "$ip" && -n "$port" ]] && printf '%s:%s\n' "$ip" "$port"
    return 0
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
    local enabled deadline
    enabled="$("$SYSTEMCTL" is-enabled "$HTTP_UNIT" 2>/dev/null || true)"
    if [[ "$enabled" != enabled ]]; then
        echo "dashboard-http-service: refusing to restart ${HTTP_UNIT}: it is ${enabled:-not enabled}. Starting it would stop ${STATIC_UNIT} through Conflicts= and take over 8090, which is a cutover; run the install step (#1503) with Max's go instead." >&2
        return 3
    fi
    say "restarting ${HTTP_UNIT}"
    if "$SYSTEMCTL" restart "$HTTP_UNIT"; then
        deadline=$((SECONDS + START_WAIT))
        while ((SECONDS < deadline)) && [[ -n "$(live_reason)" ]]; do
            sleep 0.5
        done
    else
        say "systemctl restart ${HTTP_UNIT} failed" >&2
    fi
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
