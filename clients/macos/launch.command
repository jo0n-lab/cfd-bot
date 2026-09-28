#!/bin/bash
# Choose a Host alias; ssh applies its User, Port, keys and jump host.
client_dir=$(cd "$(dirname "$0")" && pwd)
source "$client_dir/ssh-hosts.sh"
hosts=()
while IFS= read -r host; do hosts+=("$host"); done < <(ssh_config_hosts "$HOME/.ssh/config" | sort -u)
if [ "${#hosts[@]}" -eq 0 ]; then
    read -r -p 'No named Host entries found in ~/.ssh/config. Press Enter to close. ' unused
    exit 1
fi
printf 'SSH hosts from ~/.ssh/config:\n'
for index in "${!hosts[@]}"; do printf '%d) %s\n' "$((index+1))" "${hosts[$index]}"; done
read -r -p 'Select host number: ' selection
if [[ ! "$selection" =~ ^[1-9][0-9]*$ ]] || [ "${#selection}" -gt 6 ] || [ "$selection" -gt "${#hosts[@]}" ]; then
    printf 'Invalid selection.\n'; exit 1
fi
SERVER=${hosts[$((selection-1))]}
PORT=8766
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -L "127.0.0.1:$PORT:127.0.0.1:$PORT" "$SERVER" &
connection=$!
trap 'kill "$connection" 2>/dev/null || true; wait "$connection" 2>/dev/null || true' EXIT
trap 'exit 130' INT TERM HUP
until nc -z 127.0.0.1 "$PORT"; do
    if ! kill -0 "$connection" 2>/dev/null; then
        read -r -p 'SSH connection failed. Press Enter to close. ' unused
        exit 1
    fi
    sleep 1
done
open "http://127.0.0.1:$PORT"
read -r -p 'Connected. Keep this window open. Enter to disconnect. ' unused
