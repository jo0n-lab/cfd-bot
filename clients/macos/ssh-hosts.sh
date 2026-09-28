#!/bin/bash
# Enumerate names only. OpenSSH still interprets all connection settings.
ssh_config_hosts() {
    local file=$1 depth=${2:-0} kind value pattern included
    [ "$depth" -lt 16 ] && [ -f "$file" ] || return 0
    file="$(cd "$(dirname "$file")" && pwd)/$(basename "$file")"
    case "$seen_configs" in *"|$file|"*) return 0;; esac
    seen_configs="$seen_configs|$file|"
    while IFS=$'\t' read -r kind value; do
        if [ "$kind" = host ]; then
            printf '%s\n' "$value"
        else
            pattern=$value
            case "$pattern" in
                '~/'*) pattern="$HOME/${pattern:2}";;
                /*) ;;
                *) pattern="$ssh_config_root/$pattern";;
            esac
            while IFS= read -r included; do
                ssh_config_hosts "$included" "$((depth+1))"
            done < <(compgen -G "$pattern" || true)
        fi
    done < <(awk '
        function emit(value) {
            if (value == "") return
            if (key == "include") print key "\t" value
            else if (value ~ /^[A-Za-z0-9_][A-Za-z0-9_.:-]*$/) print key "\t" value
        }
        {
            if (!match($0, /^[ \t]*[A-Za-z]+/)) next
            key=tolower(substr($0,1,RLENGTH)); gsub(/^[ \t]+/,"",key)
            if (key != "host" && key != "include") next
            line=substr($0,RLENGTH+1); sub(/^[ \t]*=?[ \t]*/,"",line)
            token=""; quoted=0
            for (i=1; i<=length(line); i++) {
                c=substr(line,i,1)
                if (c == "\"") { quoted=!quoted; continue }
                if (!quoted && c == "#") break
                if (!quoted && c ~ /[ \t]/) { emit(token); token="" }
                else token=token c
            }
            emit(token)
        }' "$file")
}
seen_configs=''
ssh_config_root="$HOME/.ssh"
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    ssh_config_hosts "$HOME/.ssh/config" | sort -u
fi
