set -euo pipefail

listen_address='@listen_address@'
gost_upstream_url='@gost_upstream_url@'
direct_upstream_url='@direct_upstream_url@'
tunnel_ssh_host='@tunnel_ssh_host@'
tunnel_target_address='@tunnel_target_address@'
tunnel_local_port='@tunnel_local_port@'
tunnel_readiness_attempt_limit=60
tunnel_retry_initial_delay_s=5
tunnel_retry_max_delay_s=300
tunnel_retry_poll_interval_s=5

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

gost_is_alive() {
    kill -0 "$gost_process_id" 2>/dev/null
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
    if wait_for_tunnel; then
        return 0
    fi
    stop_process "$tunnel_process_id"
    tunnel_process_id=''
    return 1
}

start_gost() {
    @gost@ -L "socks5://$listen_address" -F "$1" &
    gost_process_id=$!
}

exit_if_gost_died() {
    if ! gost_is_alive; then
        log 'child process exited, restarting'
        stop_children
        exit 1
    fi
}

sleep_while_gost_alive() {
    local remaining_s=$1
    while [ "$remaining_s" -gt 0 ]; do
        exit_if_gost_died
        @sleep@ "$tunnel_retry_poll_interval_s"
        remaining_s=$((remaining_s - tunnel_retry_poll_interval_s))
    done
    exit_if_gost_died
}

run_direct_until_tunnel_returns() {
    local delay_s="$tunnel_retry_initial_delay_s"
    log "ssh tunnel unavailable; forwarding directly to the upstream proxy until the tunnel is back (traffic is not wrapped in ssh)"
    start_gost "$direct_upstream_url"
    while :; do
        log "retrying ssh tunnel in ${delay_s}s"
        sleep_while_gost_alive "$delay_s"
        if start_tunnel; then
            log 'ssh tunnel is back; switching to the tunnelled upstream'
            stop_process "$gost_process_id"
            start_gost "$gost_upstream_url"
            return 0
        fi
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

if [ -z "$tunnel_ssh_host" ]; then
    start_gost "$gost_upstream_url"
elif start_tunnel; then
    start_gost "$gost_upstream_url"
else
    run_direct_until_tunnel_returns
fi

wait_for_any_child
log 'child process exited, restarting'
stop_children
exit 1
