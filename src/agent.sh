set -euo pipefail

listen_address='@listen_address@'
gost_upstream_url='@gost_upstream_url@'
tunnel_ssh_host='@tunnel_ssh_host@'
tunnel_target_address='@tunnel_target_address@'
tunnel_local_port='@tunnel_local_port@'
tunnel_readiness_attempt_limit=60
tunnel_retry_initial_delay_s=5
tunnel_retry_max_delay_s=300

tunnel_process_id=''
gost_process_id=''

log() {
    printf '%(%Y-%m-%dT%H:%M:%S%z)T tgproxy agent: %s\n' -1 "$1" >&2
}

stop_process() {
    if [ -n "$1" ]; then
        kill "$1" 2>/dev/null || true
        wait "$1" 2>/dev/null || true
    fi
}

stop_children() {
    stop_process "$gost_process_id"
    stop_process "$tunnel_process_id"
}

tunnel_is_alive() {
    kill -0 "$tunnel_process_id" 2>/dev/null
}

tunnel_is_listening() {
    @lsof@ -nP -a -p "$tunnel_process_id" -iTCP@127.0.0.1:"$tunnel_local_port" -sTCP:LISTEN -t >/dev/null 2>&1
}

wait_for_tunnel() {
    local _attempt
    for _attempt in $(seq "$tunnel_readiness_attempt_limit"); do
        if ! tunnel_is_alive; then
            log 'ssh tunnel exited before listening'
            return 1
        fi
        if tunnel_is_listening; then
            return 0
        fi
        @sleep@ 1
    done
    log 'ssh tunnel did not start listening'
    return 1
}

start_tunnel() {
    @ssh@ -N \
        -o BatchMode=yes \
        -o ConnectTimeout=15 \
        -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=15 \
        -o ServerAliveCountMax=3 \
        -o ControlMaster=no \
        -o ControlPath=none \
        -L "127.0.0.1:$tunnel_local_port:$tunnel_target_address" \
        "$tunnel_ssh_host" &
    tunnel_process_id=$!
    wait_for_tunnel
}

start_tunnel_with_backoff() {
    local delay_s="$tunnel_retry_initial_delay_s"
    until start_tunnel; do
        stop_process "$tunnel_process_id"
        tunnel_process_id=''
        log "retrying ssh tunnel in ${delay_s}s"
        @sleep@ "$delay_s"
        delay_s=$((delay_s * 2))
        if [ "$delay_s" -gt "$tunnel_retry_max_delay_s" ]; then
            delay_s="$tunnel_retry_max_delay_s"
        fi
    done
}

wait_for_any_child() {
    if [ -n "$tunnel_process_id" ]; then
        wait -n "$tunnel_process_id" "$gost_process_id" || true
    else
        wait "$gost_process_id" || true
    fi
}

trap stop_children EXIT

if [ -n "$tunnel_ssh_host" ]; then
    start_tunnel_with_backoff
fi

@gost@ -L "socks5://$listen_address" -F "$gost_upstream_url" &
gost_process_id=$!

wait_for_any_child
log 'child process exited, restarting'
stop_children
exit 1
