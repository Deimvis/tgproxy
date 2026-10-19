set -euo pipefail

listen_address='@listen_address@'
gost_upstream_url='@gost_upstream_url@'
tunnel_ssh_host='@tunnel_ssh_host@'
tunnel_target_address='@tunnel_target_address@'
tunnel_local_port='@tunnel_local_port@'
tunnel_readiness_attempt_limit=40

tunnel_process_id=''
gost_process_id=''

stop_children() {
    local process_id
    for process_id in "$gost_process_id" "$tunnel_process_id"; do
        if [ -n "$process_id" ]; then
            kill "$process_id" 2>/dev/null || true
            wait "$process_id" 2>/dev/null || true
        fi
    done
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
            echo 'tgproxy agent: ssh tunnel exited before listening' >&2
            return 1
        fi
        if tunnel_is_listening; then
            return 0
        fi
        @sleep@ 0.25
    done
    echo 'tgproxy agent: ssh tunnel did not start listening' >&2
    return 1
}

start_tunnel() {
    @ssh@ -N \
        -o BatchMode=yes \
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

wait_for_any_child() {
    if [ -n "$tunnel_process_id" ]; then
        wait -n "$tunnel_process_id" "$gost_process_id" || true
    else
        wait "$gost_process_id" || true
    fi
}

trap stop_children EXIT

if [ -n "$tunnel_ssh_host" ]; then
    start_tunnel
fi

@gost@ -L "socks5://$listen_address" -F "$gost_upstream_url" &
gost_process_id=$!

wait_for_any_child
echo 'tgproxy agent: child process exited, restarting' >&2
stop_children
exit 1
