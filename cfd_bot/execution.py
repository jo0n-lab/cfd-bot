"""Resolve launch resources from existing case settings without executing them."""
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


def openfoam_environment(bashrc, environment):
    """Load the configured OpenFOAM environment without a login shell or eval."""
    ui = load_ui()
    env = dict(environment)
    if not bashrc:
        return env
    path = Path(bashrc).expanduser().resolve()
    if not path.is_file():
        raise ValueError(ui.text('scenarios.diagnostics.execution.env_missing', path=path))
    # Keep the source output separate from the NUL-delimited environment. The
    # script path is an argv item, so spaces and shell characters stay literal.
    script = ('source "$1" >/dev/null || exit 120\n'
              'command -v foamDictionary >/dev/null || exit 121\n'
              'exec /usr/bin/env -0')
    try:
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', script,
                                 'cfd-openfoam-env', str(path)], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30)
    except subprocess.TimeoutExpired:
        raise ValueError(ui.text('scenarios.diagnostics.execution.env_timeout')) from None
    if result.returncode:
        detail = (ui.text('scenarios.diagnostics.execution.dictionary_missing') if result.returncode == 121
                  else ui.text('scenarios.diagnostics.execution.exit_code', code=result.returncode))
        # Never expose captured environment variables or source output in alerts.
        raise ValueError(ui.text('scenarios.diagnostics.execution.env_failed', detail=detail, path=path))
    loaded = {}
    for item in result.stdout.split(b'\0'):
        if b'=' in item:
            key, value = item.split(b'=', 1)
            loaded[os.fsdecode(key)] = os.fsdecode(value)
    if not loaded.get('PATH'):
        raise ValueError(ui.text('scenarios.diagnostics.execution.path_missing'))
    validate_openfoam_environment(loaded)
    return loaded


def validate_openfoam_environment(env):
    """Check the full launch toolchain before any case settings are changed."""
    ui = load_ui()
    tools = {name: shutil.which(name, path=env.get('PATH', '')) for name in
             ('foamDictionary', 'foamRun', 'decomposePar', 'wmake', 'mpicc', 'mpirun')}
    missing = [name for name, path in tools.items() if not path]
    if missing:
        raise ValueError(ui.text('scenarios.diagnostics.execution.tools_missing', tools=', '.join(missing)))
    try:
        version = subprocess.run([tools['mpirun'], '--version'], env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=10)
    except subprocess.TimeoutExpired:
        raise ValueError(ui.text('scenarios.diagnostics.execution.mpi_timeout')) from None
    if version.returncode:
        raise ValueError(ui.text('scenarios.diagnostics.execution.mpi_failed', code=version.returncode))
    uses_openmpi = ('openmpi' in env.get('FOAM_MPI', '').lower()
                    or env.get('WM_MPLIB') in ('SYSTEMOPENMPI', 'OPENMPI'))
    if uses_openmpi and b'open mpi' not in (version.stdout + version.stderr).lower():
        raise ValueError(ui.text('scenarios.diagnostics.execution.mpi_mismatch', path=tools['mpirun']))
    return tools


def clean_service_environment():
    """A diagnostic baseline without the interactive shell's MPI/OpenFOAM paths."""
    env = {key: os.environ[key] for key in ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR')
           if key in os.environ}
    env['PATH'] = '/usr/local/bin:/usr/bin:/bin'
    return env


def execution_case(case):
    ui = load_ui(case.get('_ui_dir'))
    result = deepcopy(case)
    root = Path(case['_root'])
    allrun = root / 'Allrun'
    script = allrun.read_text(encoding='utf-8', errors='replace') if allrun.is_file() else ''
    settings = {}
    sources = [allrun] + sorted((root / 'config').glob('*Run'))
    for source in sources:
        if not source.is_file():
            continue
        for line in source.read_text(encoding='utf-8', errors='replace').splitlines():
            match = re.fullmatch(r'\s*(?:export\s+)?(NP|CPU_SET)\s*=\s*(.*?)\s*', line)
            if not match:
                continue
            parts = shlex.split(match[2], comments=True)
            if len(parts) == 1 and not any(c in parts[0] for c in '$`();'):
                settings[match[1]] = parts[0]
    # Allrun can reset affinity itself: its actual resource settings take priority.
    common = case.get('resource_source') == 'macro'
    automatic = case.get('cpu_policy') == 'auto'
    cpus = case.get('cpu_set') if common else settings.get('CPU_SET', case.get('cpu_set'))
    if not cpus and not automatic:
        raise ValueError(ui.text('scenarios.diagnostics.execution.cpu_set_missing'))
    selected = cpu_set(cpus) if cpus and not automatic else set()
    ranks = int(case['cores'] if common else settings.get('NP', case.get('cores', len(selected) or 1)))
    if ranks < 1 or (not automatic and ranks > len(selected)):
        raise ValueError(ui.text('scenarios.diagnostics.execution.rank_mismatch'))
    command = list(case.get('command') or ['./Allrun'])
    runs_allrun = (Path(command[0]).name == 'Allrun' or
                  (Path(command[0]).name in ('bash', 'sh') and len(command) > 1
                   and Path(command[1]).name == 'Allrun'))
    if runs_allrun:
        if not script:
            raise ValueError(ui.text('scenarios.diagnostics.execution.allrun_missing'))
        if '--foreground' in script and '--foreground' not in command:
            command.append('--foreground')
        elif re.search(r'\bnohup\b|\bsetsid\b', script) and '--foreground' not in command:
            raise ValueError(ui.text('scenarios.diagnostics.execution.detached_allrun'))
    result.update(cores=ranks, command=command)
    if automatic:
        # Tickets hold allocation intent; never reuse a previous run's CPU IDs.
        result.pop('cpu_set', None)
        result['allow_cross_socket'] = True
    else:
        result['cpu_set'] = cpus
    return result


def apply_execution_settings(case, job_folder):
    """Make known Allrun assignments agree with the admitted macro allocation.

    Keep exact source backups in the job folder. Only NP/CPU_SET assignments
    are replaced; shell snippets are never evaluated to discover settings.
    """
    ui = load_ui(case.get('_ui_dir'))
    if case.get('resource_source') != 'macro' and case.get('cpu_policy') != 'auto':
        return
    root = Path(case['_root'])
    sources = [root / 'Allrun'] + sorted((root / 'config').glob('*Run'))
    values = {'NP': str(case['cores']), 'CPU_SET': shlex.quote(case['cpu_set'])}
    assignment = re.compile(r'^(\s*(?:export\s+)?)(NP|CPU_SET)\s*=.*$', re.M)
    for source in sources:
        if not source.is_file():
            continue
        if not source.resolve().is_relative_to(root):
            raise ValueError(ui.text('scenarios.diagnostics.execution.settings_escape', path=source))
        before = source.read_text(encoding='utf-8')
        after = assignment.sub(lambda m: f'{m[1]}{m[2]}={values[m[2]]}', before)
        if after == before:
            continue
        backup = Path(job_folder) / 'case-settings-before' / source.relative_to(root)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
        with tempfile.NamedTemporaryFile('w', dir=source.parent, prefix='.', delete=False,
                                         encoding='utf-8') as stream:
            temporary = Path(stream.name)
            stream.write(after)
        try:
            temporary.chmod(source.stat().st_mode & 0o777)
            temporary.replace(source)
        finally:
            temporary.unlink(missing_ok=True)
