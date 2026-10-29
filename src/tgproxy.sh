set -euo pipefail

listen_address='@listen_address@'
service_label=org.nix-community.home.tgproxy
user_domain="gui/$(@id@ -u)"
service_target="$user_domain/$service_label"
configuration_path="$HOME/.config/tgproxy/agent.plist"
log_path="$HOME/Library/Logs/tgproxy.log"

service_is_loaded() {
    @launchctl@ print "$service_target" >/dev/null 2>&1
}

service_process_id() {
    @launchctl@ print "$service_target" 2>/dev/null |
        @sed@ -n 's/^[[:space:]]*pid = \([0-9][0-9]*\)$/\1/p'
}

service_process_ids() {
    local process_id=$1 child_process_ids
    child_process_ids="$(@pgrep@ -P "$process_id" 2>/dev/null | tr '\n' ',')"
    printf '%s' "$process_id,${child_process_ids}" | @sed@ 's/,*$//'
}

service_is_listening() {
    local process_id
    process_id="$(service_process_id)" || return 1
    [ -n "$process_id" ] || return 1
    @lsof@ -nP -a -p "$(service_process_ids "$process_id")" -iTCP@"$listen_address" -sTCP:LISTEN -t >/dev/null 2>&1
}

service_status() {
    if ! service_is_loaded; then
        echo 'tgproxy: stopped'
        return 1
    fi
    if service_is_listening; then
        echo "tgproxy: running (PID $(service_process_id), socks5://$listen_address)"
        return 0
    fi
    echo 'tgproxy: starting or restarting; use tgproxy logs'
    return 1
}

service_start() {
    if ! service_is_loaded; then
        @mkdir@ -p "$HOME/Library/Logs"
        @launchctl@ bootstrap "$user_domain" "$configuration_path"
    fi
    local _attempt
    for _attempt in {1..20}; do
        if service_is_listening; then
            service_status
            return 0
        fi
        @sleep@ 0.25
    done
    service_status
}

service_stop() {
    if service_is_loaded; then
        if ! @launchctl@ bootout "$service_target"; then
            if service_is_loaded; then
                echo 'tgproxy: failed to stop' >&2
                return 1
            fi
        fi
    fi
    echo 'tgproxy: stopped'
}

service_logs() {
    if [ -f "$log_path" ]; then
        @tail@ -n 50 "$log_path"
    else
        echo 'tgproxy: no logs yet'
    fi
}

if [ "$#" -gt 1 ]; then
    echo 'usage: tgproxy [on|off|status|logs]' >&2
    exit 2
fi

case "${1:-status}" in
    on) service_start ;;
    off) service_stop ;;
    status) service_status ;;
    logs) service_logs ;;
    -h|--help) echo 'usage: tgproxy [on|off|status|logs]' ;;
    *) echo 'usage: tgproxy [on|off|status|logs]' >&2; exit 2 ;;
esac
