set -euo pipefail

listen_address='@listen_address@'
state_path='@state_path@'
service_label=org.nix-community.home.tgproxy
user_domain="gui/$(@id@ -u)"
service_target="$user_domain/$service_label"
configuration_path="$HOME/.config/tgproxy/agent.plist"
log_path="$HOME/Library/Logs/tgproxy.log"
usage='usage: tgproxy [on|off|restart|refresh|status|logs [--follow]]'
start_timeout_s=90
stop_timeout_s=30
outdated_agent_message='the running agent is outdated (loaded before the last configuration switch)'

if [ -t 1 ]; then
    is_interactive=1
else
    is_interactive=0
fi

service_is_loaded() {
    @launchctl@ print "$service_target" >/dev/null 2>&1
}

service_process_id() {
    @launchctl@ print "$service_target" 2>/dev/null |
        @sed@ -n 's/^[[:space:]]*pid = \([0-9][0-9]*\)$/\1/p'
}

loaded_program_path() {
    @launchctl@ print "$service_target" 2>/dev/null |
        @sed@ -n '/^[[:space:]]*program = /{s/^[[:space:]]*program = //p;q;}'
}

configured_program_path() {
    @plutil@ -extract ProgramArguments.0 raw -o - "$configuration_path" 2>/dev/null
}

service_is_outdated() {
    local loaded_path configured_path
    loaded_path="$(loaded_program_path)"
    configured_path="$(configured_program_path)" || return 1
    [ -n "$loaded_path" ] && [ -n "$configured_path" ] && [ "$loaded_path" != "$configured_path" ]
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

client_connection_count() {
    @lsof@ -nP -a -p "$(service_process_ids "$1")" -iTCP@"$listen_address" -sTCP:ESTABLISHED -F n 2>/dev/null | @grep@ -c '^n' || true
}

agent_state_value() {
    [ -f "$state_path" ] || return 0
    @sed@ -n "s/^$1=//p" "$state_path" | @tail@ -n 1
}

current_epoch_s() {
    printf '%(%s)T' -1
}

formatted_duration() {
    local seconds=$1
    if [ "$seconds" -lt 0 ]; then
        seconds=0
    fi
    if [ "$seconds" -ge 3600 ]; then
        printf '%dh %02dm' $((seconds / 3600)) $((seconds % 3600 / 60))
    elif [ "$seconds" -ge 60 ]; then
        printf '%dm %02ds' $((seconds / 60)) $((seconds % 60))
    else
        printf '%ds' "$seconds"
    fi
}

painted() {
    local color=$1 text=$2
    if [ "$is_interactive" = 1 ] && [ -n "$color" ]; then
        printf '\033[%sm%s\033[0m' "$color" "$text"
    else
        printf '%s' "$text"
    fi
}

event_color() {
    case "$1" in
        success) echo 32 ;;
        warning) echo 33 ;;
        failure) echo 31 ;;
        *) echo '' ;;
    esac
}

event_marker() {
    case "$1" in
        success) echo '✓' ;;
        warning) echo '!' ;;
        failure) echo '✗' ;;
        *) echo '·' ;;
    esac
}

last_step_line() {
    [ -f "$log_path" ] || return 0
    @tail@ -n 400 "$log_path" | @grep@ ' tgproxy agent: ' | @tail@ -n 1 || true
}

describe_upstream() {
    local mode ssh_host upstream_address next_tunnel_retry_ts
    mode="$(agent_state_value mode)"
    ssh_host="$(agent_state_value ssh_host)"
    upstream_address="$(agent_state_value upstream_address)"
    next_tunnel_retry_ts="$(agent_state_value next_tunnel_retry_ts)"
    if [ "$mode" = tunnelled ]; then
        echo "through the ssh tunnel via $ssh_host to $upstream_address"
    elif [ -n "$ssh_host" ]; then
        local retry_description=''
        if [ -n "$next_tunnel_retry_ts" ]; then
            retry_description="; retrying the tunnel in $(formatted_duration $((next_tunnel_retry_ts - $(current_epoch_s))))"
        fi
        echo "directly to $upstream_address, not wrapped in ssh (ssh tunnel via $ssh_host is unavailable$retry_description)"
    else
        echo "directly to $upstream_address"
    fi
}

service_status() {
    local status_exit_code=0
    service_status_body || status_exit_code=$?
    if service_is_outdated; then
        echo "  ! $outdated_agent_message; restart it with: tgproxy restart"
    fi
    return "$status_exit_code"
}

service_status_body() {
    if ! service_is_loaded; then
        echo 'tgproxy: stopped'
        return 1
    fi
    local process_id
    process_id="$(service_process_id)"
    if [ -z "$process_id" ]; then
        echo 'tgproxy: starting (agent loaded, process not running yet)'
        return 1
    fi
    if ! service_is_listening; then
        painted 33 "tgproxy: starting or restarting (PID $process_id)"
        echo
        local step_line
        step_line="$(last_step_line)"
        if [ -n "$step_line" ]; then
            echo "  last step  ${step_line:11:8} ${step_line#* tgproxy agent: }"
        fi
        echo '  follow progress: tgproxy logs --follow'
        return 1
    fi
    painted 32 'tgproxy: running'
    echo
    echo "  endpoint socks5://$listen_address"
    if [ "$(agent_state_value process_id)" = "$process_id" ]; then
        local mode_start_ts
        mode_start_ts="$(agent_state_value mode_start_ts)"
        echo "  upstream $(describe_upstream)"
        if [ -n "$mode_start_ts" ]; then
            echo "  up       $(formatted_duration $(($(current_epoch_s) - mode_start_ts))) (since $(printf '%(%H:%M:%S)T' "$mode_start_ts"))"
        fi
    fi
    echo "  clients  $(client_connection_count "$process_id") connections"
    echo "  PID      $process_id"
}

log_size() {
    if [ -f "$log_path" ]; then
        @wc@ -c < "$log_path" | tr -d ' '
    else
        echo 0
    fi
}

elapsed_tenths() {
    local now_us=${EPOCHREALTIME//[.,]/}
    echo $(((now_us - watch_start_us) / 100000))
}

formatted_elapsed() {
    local tenths
    tenths="$(elapsed_tenths)"
    printf '%5d.%ds' $((tenths / 10)) $((tenths % 10))
}

clear_spinner() {
    if [ "$is_interactive" = 1 ] && [ "$spinner_is_shown" = 1 ]; then
        printf '\r\033[K'
        spinner_is_shown=0
    fi
}

print_event() {
    local kind=$1 text=$2
    clear_spinner
    printf '  %s %s\n' "$(formatted_elapsed)" "$(painted "$(event_color "$kind")" "$(event_marker "$kind") $text")"
}

show_spinner() {
    [ "$is_interactive" = 1 ] || return 0
    local frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    printf '\r\033[K  %s %s %s...' "$(formatted_elapsed)" "${frames:spinner_index % 10:1}" "$current_activity"
    spinner_index=$((spinner_index + 1))
    spinner_is_shown=1
}

classify_log_line() {
    local line=$1 message
    event_kind=''
    event_text=''
    case "$line" in
        '{'*)
            case "$line" in
                *'"level":"error"'*|*'"level":"fatal"'*)
                    event_kind=warning
                    event_text="gost: $(printf '%s' "$line" | @sed@ -n 's/.*"msg":"\([^"]*\)".*/\1/p')"
                    ;;
            esac
            return 0
            ;;
    esac
    if [[ "$line" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}[+-][0-9]{4}\ tgproxy\ agent:\ (.*)$ ]]; then
        message=${BASH_REMATCH[1]}
        event_text=$message
        case "$message" in
            'socks5 endpoint listening on '*|stopped) event_kind=success ;;
            *exited*|*unavailable*|'retrying '*|*'did not start'*|*'not listening yet'*|*restarting*) event_kind=warning ;;
            *) event_kind=step ;;
        esac
    elif [ -n "$line" ]; then
        event_kind=warning
        event_text="ssh: $line"
    fi
}

process_new_log_lines() {
    local LC_ALL=C size chunk complete line
    size="$(log_size)"
    if [ "$size" -lt "$watch_offset" ]; then
        watch_offset=0
    fi
    [ "$size" -gt "$watch_offset" ] || return 0
    chunk=''
    IFS= read -r -d '' chunk < <(@tail@ -c "+$((watch_offset + 1))" "$log_path" | head -c "$((size - watch_offset))") || true
    case "$chunk" in
        *$'\n'*) complete="${chunk%$'\n'*}"$'\n' ;;
        *) return 0 ;;
    esac
    watch_offset=$((watch_offset + ${#complete}))
    while IFS= read -r line; do
        classify_log_line "$line"
        [ -n "$event_kind" ] || continue
        print_event "$event_kind" "$event_text"
        if [ "$event_kind" != success ]; then
            current_activity=$event_text
        elif [ "$watch_goal" = listening ] && [[ "$event_text" == 'socks5 endpoint listening on '* ]]; then
            is_goal_reached=1
        fi
    done <<< "${complete%$'\n'}"
}

stop_watching() {
    clear_spinner
    painted 33 "tgproxy: stopped watching; the agent continues in the background (tgproxy status, tgproxy off)"
    echo
    exit 130
}

finish_watching() {
    clear_spinner
    trap - INT
    return "$1"
}

watch_progress() {
    local timeout_s=$3 command_output_path='' command_process_id='' command_exit_code poll_index=0
    watch_goal=$1
    watch_offset=$2
    shift 3
    watch_start_us=${EPOCHREALTIME//[.,]/}
    spinner_index=0
    spinner_is_shown=0
    is_goal_reached=0
    if [ "$watch_goal" = listening ]; then
        current_activity='starting'
    else
        current_activity='stopping'
    fi
    trap 'stop_watching' INT
    if [ "$#" -gt 0 ]; then
        command_output_path="$(mktemp -t tgproxy-command.XXXXXX)"
        "$@" > "$command_output_path" 2>&1 &
        command_process_id=$!
    fi
    while :; do
        process_new_log_lines
        if [ -n "$command_process_id" ] && ! kill -0 "$command_process_id" 2>/dev/null; then
            command_exit_code=0
            wait "$command_process_id" || command_exit_code=$?
            command_process_id=''
            if [ "$command_exit_code" -ne 0 ]; then
                clear_spinner
                painted 31 "tgproxy: ${1##*/} $2 failed with exit code $command_exit_code: $(tr '\n' ' ' < "$command_output_path")"
                echo
                rm -f "$command_output_path"
                finish_watching 1
                return
            fi
            rm -f "$command_output_path"
            if [ "$watch_goal" = stopped ]; then
                process_new_log_lines
                finish_watching 0
                return
            fi
        fi
        if [ "$watch_goal" = listening ] && [ "$is_goal_reached" = 0 ] && [ $((poll_index % 5)) -eq 4 ] && service_is_listening; then
            print_event success "socks5 endpoint listening on socks5://$listen_address"
            is_goal_reached=1
        fi
        if [ "$is_goal_reached" = 1 ] && [ -z "$command_process_id" ]; then
            finish_watching 0
            return
        fi
        if [ "$(elapsed_tenths)" -ge $((timeout_s * 10)) ]; then
            clear_spinner
            painted 33 "tgproxy: still $current_activity after $(formatted_duration "$timeout_s"); it continues in the background (tgproxy status, tgproxy logs --follow)"
            echo
            finish_watching 2
            return
        fi
        show_spinner
        poll_index=$((poll_index + 1))
        @sleep@ 0.1
    done
}

service_start() {
    if service_is_outdated; then
        echo "tgproxy: $outdated_agent_message; restarting it"
        service_restart
        return
    fi
    if service_is_listening; then
        service_status
        return 0
    fi
    @mkdir@ -p "$HOME/Library/Logs"
    local offset
    offset="$(log_size)"
    if service_is_loaded; then
        echo 'tgproxy: already starting; following progress'
        watch_progress listening "$offset" "$start_timeout_s"
        return
    fi
    echo 'tgproxy: starting'
    watch_progress listening "$offset" "$start_timeout_s" @launchctl@ bootstrap "$user_domain" "$configuration_path"
}

service_stop() {
    if ! service_is_loaded; then
        echo 'tgproxy: already stopped'
        return 0
    fi
    echo 'tgproxy: stopping'
    if ! watch_progress stopped "$(log_size)" "$stop_timeout_s" @launchctl@ bootout "$service_target"; then
        if service_is_loaded; then
            echo 'tgproxy: failed to stop' >&2
            return 1
        fi
    fi
    echo 'tgproxy: stopped'
}

service_restart() {
    if service_is_loaded; then
        service_stop || return 1
    fi
    service_start
}

service_refresh() {
    if ! service_is_loaded; then
        echo 'tgproxy: not running; nothing to refresh'
        return 0
    fi
    if ! service_is_outdated; then
        echo 'tgproxy: agent is up to date'
        return 0
    fi
    echo "tgproxy: $outdated_agent_message; restarting it"
    service_restart
}

agent_log_lines() {
    @grep@ -v '^{.*"level":"info"' "$log_path" || true
}

service_logs() {
    if [ ! -f "$log_path" ]; then
        echo 'tgproxy: no logs yet'
        return 0
    fi
    if [ "${2:-}" = "--follow" ]; then
        @tail@ -n 50 -F "$log_path"
        return
    fi
    agent_log_lines | @tail@ -n 50
    echo "--- now $(printf '%(%Y-%m-%dT%H:%M:%S%z)T' -1) ---"
    service_status || true
}

if [ "$#" -gt 1 ] && { [ "$#" -ne 2 ] || [ "$1" != "logs" ] || [ "$2" != "--follow" ]; }; then
    echo "$usage" >&2
    exit 2
fi

case "${1:-status}" in
    on) service_start ;;
    off) service_stop ;;
    restart) service_restart ;;
    refresh) service_refresh ;;
    status) service_status ;;
    logs) service_logs "$@" ;;
    -h|--help) echo "$usage" ;;
    *) echo "$usage" >&2; exit 2 ;;
esac
