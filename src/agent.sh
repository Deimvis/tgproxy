set -euo pipefail

listen_address='@listen_address@'
gost_upstream_url='@gost_upstream_url@'
direct_upstream_url='@direct_upstream_url@'
tunnel_ssh_host='@tunnel_ssh_host@'
tunnel_target_address='@tunnel_target_address@'
tunnel_local_port='@tunnel_local_port@'
state_path='@state_path@'
gost_readiness_attempt_limit=50
gost_readiness_poll_interval_s=0.2
tunnel_readiness_attempt_limit=60
tunnel_retry_initial_delay_s=5
tunnel_retry_max_delay_s=300
tunnel_retry_poll_interval_s=5

tunnel_process_id=''
gost_process_id=''
pause_process_id=''

log() {
    printf '%(%Y-%m-%dT%H:%M:%S%z)T tgproxy agent: %s\n' -1 "$1" >&2
}

current_epoch_s() {
    printf '%(%s)T' -1
}

write_agent_state() {
    local mode=$1 next_tunnel_retry_ts=${2:-}
    @mkdir@ -p "${state_path%/*}"
    printf 'process_id=%s\nmode=%s\nmode_start_ts=%s\nssh_host=%s\nupstream_address=%s\nnext_tunnel_retry_ts=%s\n' \
        "$$" "$mode" "$(current_epoch_s)" "$tunnel_ssh_host" "$tunnel_target_address" "$next_tunnel_retry_ts" > "$state_path.tmp"
    @mv@ "$state_path.tmp" "$state_path"
}

update_next_tunnel_retry() {
    if [ -f "$state_path" ]; then
        @sed@ "s/^next_tunnel_retry_ts=.*/next_tunnel_retry_ts=$1/" "$state_path" > "$state_path.tmp"
        @mv@ "$state_path.tmp" "$state_path"
    fi
}

pause() {
    @sleep@ "$1" &
    pause_process_id=$!
    wait "$pause_process_id" || true
    pause_process_id=''
}

stop_process() {
    if [ -n "$1" ]; then
        kill "$1" 2>/dev/null || true
        wait "$1" 2>/dev/null || true
    fi
}

stop_children() {
    stop_process "$pause_process_id"
    stop_process "$gost_process_id"
    stop_process "$tunnel_process_id"
    pause_process_id=''
    gost_process_id=''
    tunnel_process_id=''
}

handle_stop_signal() {
    log "stop requested ($1); stopping gost and the ssh tunnel"
    trap - EXIT
    stop_children
    @rm@ -f "$state_path"
    log 'stopped'
    exit 0
}

upstream_description() {
    if [ "$1" = tunnelled ]; then
        printf 'through the ssh tunnel via %s to %s' "$tunnel_ssh_host" "$tunnel_target_address"
    elif [ -n "$tunnel_ssh_host" ]; then
        printf 'directly to %s, not wrapped in ssh' "$tunnel_target_address"
    else
        printf 'directly to %s' "$tunnel_target_address"
    fi
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
        pause 1
    done
    log 'ssh tunnel did not start listening'
    return 1
}

start_tunnel() {
    log "starting ssh tunnel via $tunnel_ssh_host"
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
        log "ssh tunnel listening on 127.0.0.1:$tunnel_local_port"
        return 0
    fi
    stop_process "$tunnel_process_id"
    tunnel_process_id=''
    return 1
}

gost_is_listening() {
    @lsof@ -nP -a -p "$gost_process_id" -iTCP@"$listen_address" -sTCP:LISTEN -t >/dev/null 2>&1
}

wait_for_gost() {
    local _attempt
    for _attempt in $(seq "$gost_readiness_attempt_limit"); do
        exit_if_gost_died
        if gost_is_listening; then
            return 0
        fi
        pause "$gost_readiness_poll_interval_s"
    done
    return 1
}

start_gost() {
    local mode=$1 upstream_url
    if [ "$mode" = tunnelled ]; then
        upstream_url=$gost_upstream_url
    else
        upstream_url=$direct_upstream_url
    fi
    @gost@ -L "socks5://$listen_address" -F "$upstream_url" &
    gost_process_id=$!
    if wait_for_gost; then
        write_agent_state "$mode"
        log "socks5 endpoint listening on socks5://$listen_address ($(upstream_description "$mode"))"
    else
        write_agent_state "$mode"
        log "socks5 endpoint is not listening yet on socks5://$listen_address ($(upstream_description "$mode"))"
    fi
}

restart_after_child_exit() {
    log 'child process exited, restarting'
    stop_children
    exit 1
}

exit_if_gost_died() {
    if ! gost_is_alive; then
        restart_after_child_exit
    fi
}

sleep_while_gost_alive() {
    local remaining_s=$1
    while [ "$remaining_s" -gt 0 ]; do
        exit_if_gost_died
        pause "$tunnel_retry_poll_interval_s"
        remaining_s=$((remaining_s - tunnel_retry_poll_interval_s))
    done
    exit_if_gost_died
}

run_direct_until_tunnel_returns() {
    local delay_s="$tunnel_retry_initial_delay_s"
    log "ssh tunnel unavailable; forwarding directly to the upstream proxy until the tunnel is back (traffic is not wrapped in ssh)"
    start_gost direct
    while :; do
        log "retrying ssh tunnel in ${delay_s}s"
        update_next_tunnel_retry "$(( $(current_epoch_s) + delay_s ))"
        sleep_while_gost_alive "$delay_s"
        if start_tunnel; then
            log 'ssh tunnel is back; switching to the tunnelled upstream'
            stop_process "$gost_process_id"
            start_gost tunnelled
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
trap 'handle_stop_signal SIGTERM' TERM
trap 'handle_stop_signal SIGINT' INT

if [ -z "$tunnel_ssh_host" ]; then
    log "starting (forwarding directly to $tunnel_target_address)"
    start_gost direct
else
    log "starting (forwarding through an ssh tunnel via $tunnel_ssh_host to $tunnel_target_address)"
    if start_tunnel; then
        start_gost tunnelled
    else
        run_direct_until_tunnel_returns
    fi
fi

while wait_for_any_child; do
    restart_after_child_exit
done
