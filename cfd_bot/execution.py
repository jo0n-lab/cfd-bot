"""Resolve launch resources from existing case settings without executing them."""
from . import diagnostics as _diagnostics
from copy import deepcopy
from pathlib import Path
import os
import re
import shlex
import shutil
import subprocess
import tempfile

from .config import cpu_set
from .ui import load_ui


EXECUTION_DEFAULTS = dict(resource_source='case', cores=1, command=None,
                          cpu_policy='manual', cpu_set=None, allow_cross_socket=False,
                          monitoring=None)
PROCESS_CORE = '.process-core'


@_diagnostics.trace
def _literal_setting(line):
    """Read a safe NP/CPU_SET assignment without evaluating shell syntax."""
    match = re.fullmatch(r'\s*(?:export\s+)?(NP|CPU_SET)\s*=\s*(.*?)\s*', line)
    if not match:
        if _diagnostics.enabled: _diagnostics.step('execution._literal_setting:L24:then')
        return None
    name, value = match.groups()
    trailing_export = re.search(rf'\s*;\s*export\s+{name}\s*$', value)
    if trailing_export:
        if _diagnostics.enabled: _diagnostics.step('execution._literal_setting:L28:then')
        value = value[:trailing_export.start()].rstrip()
    try:
        parts = shlex.split(value, comments=True)
    except ValueError:
        if _diagnostics.enabled: _diagnostics.step('execution._literal_setting:L32:except')
        return None
    if len(parts) != 1 or any(char in parts[0] for char in '$`();'):
        if _diagnostics.enabled: _diagnostics.step('execution._literal_setting:L34:then')
        return None
    return name, parts[0]


@_diagnostics.trace
def _process_core(root, ui):
    path = root / PROCESS_CORE
    if path.is_symlink() or (path.exists() and not path.is_file()):
        if _diagnostics.enabled: _diagnostics.step('execution._process_core:L41:then')
        raise ValueError(ui.text('scenarios.diagnostics.execution.process_core_invalid', path=path))
    return path


@_diagnostics.trace
def _process_core_text(case):
    cpus = case['cpu_set']
    cpu_set(cpus)
    return (f'NP={int(case["cores"])}\n'
            f'CPU_SET="{cpus}"\n\n'
            'export NP CPU_SET\n')


@_diagnostics.trace
def _atomic_write(path, text, mode):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.', delete=False,
                                     encoding='utf-8') as stream:
        temporary = Path(stream.name)
        stream.write(text)
    try:
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@_diagnostics.trace
def _replace_settings(source, values, root, job_folder, ui):
    if not source.is_file():
        if _diagnostics.enabled: _diagnostics.step('execution._replace_settings:L67:then')
        return
    if not source.resolve().is_relative_to(root):
        if _diagnostics.enabled: _diagnostics.step('execution._replace_settings:L69:then')
        raise ValueError(ui.text('scenarios.diagnostics.execution.settings_escape', path=source))
    before = source.read_text(encoding='utf-8')
    assignment = re.compile(r'^(\s*(?:export\s+)?)(NP|CPU_SET)\s*=.*$', re.M)

    @_diagnostics.trace
    def replace(match):
        name = match[2]
        if name not in values:
            if _diagnostics.enabled: _diagnostics.step('execution._replace_settings.replace:L76:then')
            return match[0]
        suffix = re.search(rf'(\s*;\s*export\s+{name}\s*)$', match[0])
        return f'{match[1]}{name}={values[name]}{suffix[1] if suffix else ""}'

    after = assignment.sub(replace, before)
    if after == before:
        if _diagnostics.enabled: _diagnostics.step('execution._replace_settings:L82:then')
        return
    backup = Path(job_folder) / 'case-settings-before' / source.relative_to(root)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    _atomic_write(source, after, source.stat().st_mode & 0o777)


@_diagnostics.trace
def execution_settings(case):
    """Comparable execution intent, excluding runtime allocation state."""
    return {key: deepcopy(case.get(key, default)) for key, default in EXECUTION_DEFAULTS.items()}


@_diagnostics.trace
def openfoam_environment(bashrc, environment):
    """Load the configured OpenFOAM environment without a login shell or eval."""
    ui = load_ui()
    env = dict(environment)
    if not bashrc:
        if _diagnostics.enabled: _diagnostics.step('execution.openfoam_environment:L99:then')
        return env
    path = Path(bashrc).expanduser().resolve()
    if not path.is_file():
        if _diagnostics.enabled: _diagnostics.step('execution.openfoam_environment:L102:then')
        raise ValueError(ui.text('scenarios.diagnostics.execution.env_missing', path=path))
    # Keep the source output separate from the NUL-delimited environment. The
    # script path is an argv item, so spaces and shell characters stay literal.
    script = ('source "$1" >/dev/null || exit 120\n'
              'command -v foamDictionary >/dev/null || exit 121\n'
              'exec /usr/bin/env -0')
    try:
        result = _diagnostics.run_process(['/bin/bash', '--noprofile', '--norc', '-c', script,
                                 'cfd-openfoam-env', str(path)], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30)
    except subprocess.TimeoutExpired:
        if _diagnostics.enabled: _diagnostics.step('execution.openfoam_environment:L114:except')
        raise ValueError(ui.text('scenarios.diagnostics.execution.env_timeout')) from None
    if result.returncode:
        if _diagnostics.enabled: _diagnostics.step('execution.openfoam_environment:L116:then')
        detail = (ui.text('scenarios.diagnostics.execution.dictionary_missing') if result.returncode == 121
                  else ui.text('scenarios.diagnostics.execution.exit_code', code=result.returncode))
        # Never expose captured environment variables or source output in alerts.
        raise ValueError(ui.text('scenarios.diagnostics.execution.env_failed', detail=detail, path=path))
    loaded = {}
    for item in result.stdout.split(b'\0'):
        if _diagnostics.enabled: _diagnostics.step('execution.openfoam_environment:L122:loop', item=item)
        if b'=' in item:
            if _diagnostics.enabled: _diagnostics.step('execution.openfoam_environment:L123:then')
            key, value = item.split(b'=', 1)
            loaded[os.fsdecode(key)] = os.fsdecode(value)
    if not loaded.get('PATH'):
        if _diagnostics.enabled: _diagnostics.step('execution.openfoam_environment:L126:then')
        raise ValueError(ui.text('scenarios.diagnostics.execution.path_missing'))
    validate_openfoam_environment(loaded)
    return loaded


@_diagnostics.trace
def validate_openfoam_environment(env):
    """Check the full launch toolchain before any case settings are changed."""
    ui = load_ui()
    tools = {name: shutil.which(name, path=env.get('PATH', '')) for name in
             ('foamDictionary', 'foamRun', 'decomposePar', 'wmake', 'mpicc', 'mpirun')}
    missing = [name for name, path in tools.items() if not path]
    if missing:
        if _diagnostics.enabled: _diagnostics.step('execution.validate_openfoam_environment:L138:then')
        raise ValueError(ui.text('scenarios.diagnostics.execution.tools_missing', tools=', '.join(missing)))
    try:
        version = _diagnostics.run_process([tools['mpirun'], '--version'], env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=10)
    except subprocess.TimeoutExpired:
        if _diagnostics.enabled: _diagnostics.step('execution.validate_openfoam_environment:L144:except')
        raise ValueError(ui.text('scenarios.diagnostics.execution.mpi_timeout')) from None
    if version.returncode:
        if _diagnostics.enabled: _diagnostics.step('execution.validate_openfoam_environment:L146:then')
        raise ValueError(ui.text('scenarios.diagnostics.execution.mpi_failed', code=version.returncode))
    uses_openmpi = ('openmpi' in env.get('FOAM_MPI', '').lower()
                    or env.get('WM_MPLIB') in ('SYSTEMOPENMPI', 'OPENMPI'))
    if uses_openmpi and b'open mpi' not in (version.stdout + version.stderr).lower():
        if _diagnostics.enabled: _diagnostics.step('execution.validate_openfoam_environment:L150:then')
        raise ValueError(ui.text('scenarios.diagnostics.execution.mpi_mismatch', path=tools['mpirun']))
    return tools


@_diagnostics.trace
def clean_service_environment():
    """A diagnostic baseline without the interactive shell's MPI/OpenFOAM paths."""
    env = {key: os.environ[key] for key in ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR')
           if key in os.environ}
    env['PATH'] = '/usr/local/bin:/usr/bin:/bin'
    return env


@_diagnostics.trace
def execution_case(case):
    ui = load_ui(case.get('_ui_dir'))
    result = deepcopy(case)
    root = Path(case['_root'])
    allrun = root / 'Allrun'
    script = allrun.read_text(encoding='utf-8', errors='replace') if allrun.is_file() else ''
    settings = {}
    process_core = _process_core(root, ui)
    # The per-case contract wins over legacy assignments when it exists.
    sources = [allrun] + sorted((root / 'config').glob('*Run')) + [process_core]
    for source in sources:
        if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L173:loop', source=source)
        if not source.is_file():
            if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L174:then')
            continue
        for line in source.read_text(encoding='utf-8', errors='replace').splitlines():
            if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L176:loop', line=line)
            setting = _literal_setting(line)
            if setting:
                if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L178:then')
                settings[setting[0]] = setting[1]
    # Explicit ticket/macro intent takes priority and is applied before launch.
    common = case.get('resource_source') in ('macro', 'ticket')
    automatic = case.get('cpu_policy') == 'auto'
    cpus = case.get('cpu_set') if common else settings.get('CPU_SET', case.get('cpu_set'))
    if not cpus and not automatic:
        if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L184:then')
        raise ValueError(ui.text('scenarios.diagnostics.execution.cpu_set_missing'))
    selected = cpu_set(cpus) if cpus and not automatic else set()
    ranks = int(case['cores'] if common else settings.get('NP', case.get('cores', len(selected) or 1)))
    if ranks < 1 or (not automatic and ranks > len(selected)):
        if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L188:then')
        raise ValueError(ui.text('scenarios.diagnostics.execution.rank_mismatch'))
    command = list(case.get('command') or ['./Allrun'])
    runs_allrun = (Path(command[0]).name == 'Allrun' or
                  (Path(command[0]).name in ('bash', 'sh') and len(command) > 1
                   and Path(command[1]).name == 'Allrun'))
    if runs_allrun:
        if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L194:then')
        if not script:
            if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L195:then')
            raise ValueError(ui.text('scenarios.diagnostics.execution.allrun_missing'))
        if '--foreground' in script and '--foreground' not in command:
            if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L197:then')
            command.append('--foreground')
        elif re.search(r'\bnohup\b|\bsetsid\b', script) and '--foreground' not in command:
            if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L199:then')
            raise ValueError(ui.text('scenarios.diagnostics.execution.detached_allrun'))
    result.update(cores=ranks, command=command)
    if automatic:
        # Tickets hold allocation intent; never reuse a previous run's CPU IDs.
        if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L202:then')
        result.pop('cpu_set', None)
        result['allow_cross_socket'] = True
    else:
        if _diagnostics.enabled: _diagnostics.step('execution.execution_case:L202:else')
        result['cpu_set'] = cpus
    return result


@_diagnostics.trace
def apply_execution_settings(case, job_folder):
    """Make known Allrun assignments agree with the admitted allocation.

    Keep exact source backups in the job folder. Only NP/CPU_SET assignments
    are replaced; shell snippets are never evaluated to discover settings.
    """
    ui = load_ui(case.get('_ui_dir'))
    explicit = case.get('resource_source') in ('macro', 'ticket')
    root = Path(case['_root'])
    process_core = _process_core(root, ui)
    process_text = _process_core_text(case)
    if not process_core.exists():
        if _diagnostics.enabled: _diagnostics.step('execution.apply_execution_settings:L222:then')
        _atomic_write(process_core, process_text, 0o644)
    elif explicit or case.get('cpu_policy') == 'auto':
        if _diagnostics.enabled: _diagnostics.step('execution.apply_execution_settings:L224:then')
        before = process_core.read_text(encoding='utf-8')
        if before != process_text:
            if _diagnostics.enabled: _diagnostics.step('execution.apply_execution_settings:L226:then')
            backup = Path(job_folder) / 'case-settings-before' / PROCESS_CORE
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(process_core, backup)
            _atomic_write(process_core, process_text, process_core.stat().st_mode & 0o777)

    if not explicit and case.get('cpu_policy') != 'auto':
        if _diagnostics.enabled: _diagnostics.step('execution.apply_execution_settings:L232:then')
        return
    sources = [root / 'Allrun'] + sorted((root / 'config').glob('*Run'))
    values = {'CPU_SET': shlex.quote(case['cpu_set'])}
    if explicit:
        if _diagnostics.enabled: _diagnostics.step('execution.apply_execution_settings:L236:then')
        values['NP'] = str(case['cores'])
    for source in sources:
        if _diagnostics.enabled: _diagnostics.step('execution.apply_execution_settings:L238:loop', source=source)
        _replace_settings(source, values, root, job_folder, ui)
