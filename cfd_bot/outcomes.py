"""OpenFOAM completion and failure decisions."""

import math

from .config import inside
from .control import control_times
from .ui import load_ui


def wants_event(case, event):
    return event in case.get('notifications', {}).get(
        'events', ['started', 'succeeded', 'failed', 'interrupted'])


def _fresh(case, relative, started):
    path = inside(case['_root'], relative)
    try:
        return path.is_file() and path.stat().st_mtime >= started
    except OSError:
        return False


def decide(case, telemetry, started, *, returncode=None, external=False,
           log_fresh=True):
    """Return (status, reason) after a run disappears or exits.

    A run succeeds only when the newest log progress reaches the case's
    ``system/controlDict`` endTime.  Non-zero exit codes and explicit failure
    evidence take priority. File evidence must be updated during this run.
    """
    ui = load_ui(case.get('_ui_dir'))
    if returncode is not None and returncode != 0:
        context = telemetry.get('errors', [])[-2:] if log_fresh else []
        detail = ' · ' + '; '.join(line[-500:] for line in context) if context else ''
        return 'failed', ui.text('scenarios.outcomes.nonzero_exit', returncode=returncode, detail=detail)

    watcher = case.get('watcher', {})
    failure = watcher.get('failure', {})
    for relative in failure.get('updated_files', []):
        if _fresh(case, relative, started):
            return 'failed', ui.text('scenarios.outcomes.failure_file', path=relative)

    if telemetry.get('errors') and log_fresh:
        return 'failed', ui.text('scenarios.outcomes.failure_log', error=telemetry['errors'][-1])

    if not log_fresh:
        return 'failed', ui.text('scenarios.outcomes.stale_log')

    control = control_times(case)
    if control is None:
        return 'failed', ui.text('scenarios.outcomes.control_missing')
    if control['stop_at'] != 'endTime':
        return 'failed', ui.text('scenarios.outcomes.stop_mode', stop_at=control['stop_at'])

    target = control['end']
    target_label = ui.text('scenarios.outcomes.ticket_target' if case.get('end_time') is not None
                           else 'scenarios.outcomes.control_target')
    current = telemetry.get('time')
    if (not isinstance(current, (int, float)) or isinstance(current, bool)
            or not math.isfinite(current)):
        return 'failed', ui.text('scenarios.outcomes.time_missing', target_label=target_label,
                                 target=f'{target:g}')

    if current > target or math.isclose(current, target, rel_tol=1e-9,
                                        abs_tol=1e-12):
        source = ui.text('scenarios.outcomes.external_source') if external else ''
        return 'succeeded', ui.text('scenarios.outcomes.success', source=source,
                                    target_label=target_label, target=f'{target:g}', current=f'{current:g}')

    return 'failed', ui.text('scenarios.outcomes.incomplete', target_label=target_label,
                             target=f'{target:g}', current=f'{current:g}')
