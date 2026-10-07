#!/bin/bash
# Choose a Host alias; ssh applies its User, Port, keys and jump host.

# Launcher diagnostics use the same ON/OFF environment variable as the server.
client_log() {
    case ${CFD_BOT_DIAGNOSTICS:-0} in 0|off|false) return 0;; esac
    client_log_dir=${CFD_BOT_DIAGNOSTICS_DIR:-${TMPDIR:-/tmp}/cfd-client-logs}
    (umask 077; mkdir -p "$client_log_dir" &&
        printf '{"event":"%s","pid":%s,"detail":"%s"}\n' "$1" "$$" "${2:-}" >> "$client_log_dir/launcher-$$.jsonl") 2>/dev/null || :
}
client_log launcher.start

client_dir=$(cd "$(dirname "$0")" && pwd)
source "$client_dir/ssh-hosts.sh"
hosts=()
while IFS= read -r host; do hosts+=("$host"); done < <(ssh_config_hosts "$HOME/.ssh/config" | sort -u)
if [ "${#hosts[@]}" -eq 0 ]; then
    client_log ssh.config.empty
    read -r -p 'No named Host entries found in ~/.ssh/config. Press Enter to close. ' unused
    exit 1
fi
printf 'SSH hosts from ~/.ssh/config:\n'
for index in "${!hosts[@]}"; do printf '%d) %s\n' "$((index+1))" "${hosts[$index]}"; done
read -r -p 'Select host number: ' selection
if [[ ! "$selection" =~ ^[1-9][0-9]*$ ]] || [ "${#selection}" -gt 6 ] || [ "$selection" -gt "${#hosts[@]}" ]; then
    client_log ui.selection.invalid
    printf 'Invalid selection.\n'; exit 1
fi
client_log ui.host.selected "selection=$selection"
SERVER=${hosts[$((selection-1))]}
PORT=8766
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -L "127.0.0.1:$PORT:127.0.0.1:$PORT" "$SERVER" &
connection=$!
client_log ssh.started "pid=$connection port=$PORT"
trap 'client_log ssh.cleanup "pid=$connection"; kill "$connection" 2>/dev/null || true; wait "$connection" 2>/dev/null || true' EXIT
trap 'exit 130' INT TERM HUP
client_log tunnel.wait
until nc -z 127.0.0.1 "$PORT"; do
    if ! kill -0 "$connection" 2>/dev/null; then
        client_log ssh.exited "pid=$connection"
        read -r -p 'SSH connection failed. Press Enter to close. ' unused
        exit 1
    fi
    sleep 1
done
client_log tunnel.ready "port=$PORT"
client_log browser.open "port=$PORT"
open "http://127.0.0.1:$PORT"
read -r -p 'Connected. Keep this window open. Enter to disconnect. ' unused
