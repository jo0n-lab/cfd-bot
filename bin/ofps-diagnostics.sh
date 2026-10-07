# Sourced by ofps. Only diagnostic output; never enable xtrace or change errexit.
# Values deliberately exclude command text, /proc argv and process environments.
if [[ ! ${CFD_BOT_DIAGNOSTICS+x} && ${CFD_BOT_OFPS_MANAGED:-0} == 1 ]]; then
    export CFD_BOT_DIAGNOSTICS=0
fi
if [[ ${CFD_BOT_OFPS_MANAGED:-0} == 1 && ! ${CFD_BOT_DIAGNOSTICS_LEVEL+x} ]]; then
    export CFD_BOT_DIAGNOSTICS_LEVEL=basic
fi
if [[ ! ${CFD_BOT_DIAGNOSTICS+x} || ! ${CFD_BOT_DIAGNOSTICS_LEVEL+x} ]]; then
    mapfile -t _cfd_diagnostic_options < <("$bot_python" - "$bot_config" <<'PY'
import json, os, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    config = json.loads(p.read_text())
except (OSError, ValueError):
    config = {}
options = config.get('diagnostic_logging') or {}
if not isinstance(options, dict):
    options = {}
directory = options.get('directory') or os.environ.get('CFD_BOT_DIAGNOSTICS_DIR')
directory = Path(directory or str(Path(config.get('state_dir', 'state')) / 'diagnostics'))
if not directory.is_absolute():
    directory = p.parent / directory
print('1' if options.get('enabled', False) else '0')
print(directory)
print(os.environ.get('CFD_BOT_DIAGNOSTICS_LEVEL', options.get('level', 'basic')))
PY
    )
    export CFD_BOT_DIAGNOSTICS=${CFD_BOT_DIAGNOSTICS-${_cfd_diagnostic_options[0]:-0}}
    export CFD_BOT_DIAGNOSTICS_DIR=${_cfd_diagnostic_options[1]:-}
    export CFD_BOT_DIAGNOSTICS_LEVEL=${_cfd_diagnostic_options[2]:-basic}
fi

# Same collection level as the parent. Unknown values retain the basic default.
[[ ${CFD_BOT_DIAGNOSTICS_LEVEL:-basic} == detailed ]] || CFD_BOT_DIAGNOSTICS_LEVEL=basic
export CFD_BOT_DIAGNOSTICS_LEVEL

declare -A _cfd_diag_basic_functions=([run_bot_command]=1 [scan_once]=1 [scan_and_sync]=1
    [scan_supervisors]=1 [sync_ticket_state]=1 [check_cpu_set]=1 [cleanup]=1)

_cfd_diag_write()
{
    [[ ${CFD_BOT_DIAGNOSTICS:-0} == 1 && ${_cfd_diag_fd:-} ]] || return 0
    local event=$1 status=${2:-0} detail=${3:-} function=${4:-${FUNCNAME[1]:-main}}
    if [[ $event == shell.call || ( $event == shell.return && $status == 0 ) ]]; then
        if [[ $CFD_BOT_DIAGNOSTICS_LEVEL != detailed && ${_cfd_diag_basic_functions[$function]:-0} != 1 ]]; then
            return 0
        fi
    fi
    detail=${detail//\\/\\\\}; detail=${detail//\"/\\\"}
    detail=${detail//$'\n'/\\n}; detail=${detail//$'\r'/\\r}; detail=${detail//$'\t'/\\t}
    local code=${_cfd_diag_event_codes[$event]:-\"$event\"}
    local function_code=${_cfd_diag_function_codes[$function]:-\"$function\"}
    printf '{"event":"shell.record.v2","v":[%s,"%s",%s,"%s","%s",%s,%s,%s,"%s"]}\n' \
        "$code" "${EPOCHREALTIME:-0}" "$BASHPID" "${_cfd_diag_parent:-${CFD_BOT_PARENT_CALL_ID:-}}" \
        "${_cfd_diag_call:-$BASHPID}" "$function_code" "${5:-${BASH_LINENO[0]:-0}}" "$status" "$detail" >&"$_cfd_diag_fd" || :

}


_cfd_diag_header()
{
    printf '{"event":"shell.file","schema_version":2,"codebook_sha256":"%s","event_codes":%s,"function_codes":%s,"trace_id":"%s"}\n' \
        "$_cfd_diag_codebook_sha" "$_cfd_diag_event_json" "$_cfd_diag_function_json" "${CFD_BOT_TRACE_ID:-$BASHPID}" >&"$_cfd_diag_fd" || :
}

_cfd_diag_return()
{
    local result=$1 function=$2 line=${3:-0}
    case "$function" in _cfd_diag_*) return "$result";; esac
    _cfd_diag_write shell.return "$result" '' "$function" "$line"
    return "$result"
}

_cfd_diag_error()
{
    local result=$1 line=$2 pipeline=$3
    _cfd_diag_write shell.error "$result" "line=$line PIPESTATUS=$pipeline" "${FUNCNAME[1]:-main}"
    return "$result"
}

_cfd_diag_exit()
{
    local result=$1
    _cfd_diag_write shell.exit "$result" '' main
    return "$result"
}

_cfd_diag_rotate()
{
    local size n
    size=$(stat -c %s -- "$_cfd_diag_file" 2>/dev/null) || return 0
    if (( size >= 20971520 )); then
        for n in 2 1; do
            if [[ -f $_cfd_diag_file.$n ]]; then mv -f -- "$_cfd_diag_file.$n" "$_cfd_diag_file.$((n+1))" || return 0; fi
        done
        mv -f -- "$_cfd_diag_file" "$_cfd_diag_file.1" || return 0
        exec {_cfd_diag_fd}>&-
        (umask 077; : > "$_cfd_diag_file") || return 0
        if exec {_cfd_diag_fd}>>"$_cfd_diag_file"; then
            _cfd_diag_header
            _cfd_diag_write log.rotation 0 'previous=.1 max_bytes=20971520 backups=3'
        else
            CFD_BOT_DIAGNOSTICS=0
        fi
    fi
}

case ${CFD_BOT_DIAGNOSTICS:-0} in
    0|off|false) ;;
    *)
        _cfd_diag_dir=${CFD_BOT_DIAGNOSTICS_DIR:-$project_root/state/diagnostics}
        if mkdir -p -m 700 -- "$_cfd_diag_dir"; then
            _cfd_diag_old_umask=$(umask)
            umask 077
            _cfd_diag_file="$_cfd_diag_dir/ofps-$BASHPID-${EPOCHREALTIME//./}.jsonl"
            if exec {_cfd_diag_fd}>>"$_cfd_diag_file"; then
                export CFD_BOT_DIAGNOSTICS=1 CFD_BOT_DIAGNOSTICS_DIR="$_cfd_diag_dir"
                source "${BASH_SOURCE[0]%/*}/ofps-diagnostic-codes.sh"
                _cfd_diag_header
                _cfd_diag_write shell.start 0 "schema=2 level=$CFD_BOT_DIAGNOSTICS_LEVEL"
                set -ET
                trap '_cfd_diag_error "$?" "$LINENO" "${PIPESTATUS[*]}"' ERR
                trap '_cfd_diag_return "${_cfd_diag_result:-$?}" "${FUNCNAME[0]:-main}" "$LINENO"' RETURN
            else
                CFD_BOT_DIAGNOSTICS=0
            fi
            umask "$_cfd_diag_old_umask"
        else
            CFD_BOT_DIAGNOSTICS=0
        fi
        ;;
esac
