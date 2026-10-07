"""Shared read models for queue/history tracking and live macro progress."""
from . import diagnostics as _diagnostics
import hashlib
import math
import time
from pathlib import Path
from statistics import median

from .logs import estimate, recent_case_log


ACTIVE = ('queued', 'starting', 'running', 'postprocessing')


@_diagnostics.trace
def case_id_for_root(root):
    return hashlib.sha256(str(root).encode()).hexdigest()[:12]


@_diagnostics.trace
def tracking_registry(cases):
    """Index only currently loadable ticket JSONs by their exact case root."""
    registry = {}
    for case in cases:
        if _diagnostics.enabled: _diagnostics.step('run_views.tracking_registry:L21:loop', case=case)
        config = Path(case.get('_config', ''))
        if not config.is_file():
            if _diagnostics.enabled: _diagnostics.step('run_views.tracking_registry:L23:then')
            continue
        registry[case['_root']] = dict(case=case, case_id=case_id_for_root(case['_root']),
                                       ticket=config.name)
    return registry


@_diagnostics.trace
def job_view(job, registry):
    """Return a UI-safe job projection with current ticket traceability."""
    tracked = registry.get(job['case_root'])
    return dict(id=job['id'], name=job['case']['name'], case_dir=job['case_root'],
                trackable=tracked is not None,
                case_id=tracked['case_id'] if tracked else None,
                ticket=tracked['ticket'] if tracked else None,
                **{key: job.get(key) for key in ('status', 'created', 'started', 'finished',
                                                'reason', 'actual_cores', 'actual_cpu_list',
                                                'priority', 'queue_lane', 'queue_id',
                                                'queue_cpu_set', 'dynamic_cores')})


@_diagnostics.trace
def _remaining(case, job, store, now):
    status = job.get('status')
    telemetry = job.get('telemetry', {})
    if status == 'running':
        if _diagnostics.enabled: _diagnostics.step('run_views._remaining:L46:then')
        try:
            telemetry, _ = recent_case_log(case)
        except (OSError, ValueError):
            if _diagnostics.enabled: _diagnostics.step('run_views._remaining:L49:except')
            pass
    elapsed = max(0, now - job.get('started', now)) if status != 'queued' else 0
    prediction = estimate(case, telemetry, elapsed)
    if prediction.get('basis') == 'unknown':
        if _diagnostics.enabled: _diagnostics.step('run_views._remaining:L53:then')
        prediction = estimate(case, telemetry, elapsed,
                              store.runtime_history(case, job.get('actual_cores')))
    remaining = prediction.get('remaining_seconds')
    if remaining is None and prediction.get('basis') == 'target_reached':
        if _diagnostics.enabled: _diagnostics.step('run_views._remaining:L57:then')
        remaining = 0
    return prediction, remaining


@_diagnostics.trace
def running_macro_views(macros, cases, jobs, store, now=None):
    """Aggregate each currently running macro from its current child job IDs."""
    now = time.time() if now is None else now
    by_root = {case['_root']: case for case in cases}
    by_id = {job['id']: job for job in jobs}
    views = []
    for macro in macros:
        if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L68:loop', macro=macro)
        if macro.get('task_type') != 'macro' or macro.get('queue', {}).get('state') != 'running':
            if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L69:then')
            continue
        rows = macro.get('cases', [])
        current_jobs = [by_id[row['job_id']] for row in rows if row.get('job_id') in by_id]
        if not current_jobs:
            if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L73:then')
            continue
        completed = sum(1 for row in rows if row.get('state') == 'finished')
        failed = sum(1 for row in rows if row.get('state') == 'finished'
                     and row.get('result') not in (None, 'succeeded'))
        active_progress = 0.0
        completed_durations = []
        for row in rows:
            if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L80:loop', row=row)
            job = by_id.get(row.get('job_id'))
            if row.get('result') != 'succeeded' or job is None:
                if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L82:then')
                continue
            started = job.get('started')
            finished = job.get('solver_finished', job.get('finished'))
            if (isinstance(started, (int, float)) and isinstance(finished, (int, float))
                    and math.isfinite(started) and math.isfinite(finished) and finished > started):
                if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L86:then')
                completed_durations.append(finished - started)
        batch_expected = median(completed_durations[-5:]) if completed_durations else None
        remaining_total = 0.0
        remaining_known = True
        active_name = None
        for row in rows:
            if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L93:loop', row=row)
            job = by_id.get(row.get('job_id'))
            if row.get('state') == 'finished':
                if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L95:then')
                continue
            if job is None or job.get('status') not in ACTIVE:
                if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L97:then')
                remaining_known = False
                continue
            case = by_root.get(row['case_dir'])
            if case is None:
                if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L101:then')
                remaining_known = False
                continue
            if not remaining_known and job['status'] == 'queued':
                if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L104:then')
                continue
            prediction, remaining = _remaining(case, job, store, now)
            if job['status'] in ('starting', 'running', 'postprocessing'):
                if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L107:then')
                active_name = case['name']
                progress = prediction.get('progress')
                if isinstance(progress, (int, float)) and math.isfinite(progress):
                    if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L110:then')
                    active_progress += max(0.0, min(1.0, progress))
            if remaining is None and batch_expected is not None:
                if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L112:then')
                elapsed = max(0, now - job.get('started', now)) if job['status'] != 'queued' else 0
                remaining = max(0, batch_expected - elapsed)
            if remaining is None or not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
                if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L115:then')
                remaining_known = False
            else:
                if _diagnostics.enabled: _diagnostics.step('run_views.running_macro_views:L115:else')
                remaining_total += max(0.0, remaining)
        total = len(rows)
        progress = min(1.0, (completed + active_progress) / total) if total else 0.0
        created = [job.get('created') for job in current_jobs
                   if isinstance(job.get('created'), (int, float))]
        views.append(dict(
            name=macro['name'], ticket=Path(macro['_config']).name,
            completed=completed, failed=failed, target=total, active_case=active_name,
            elapsed_seconds=max(0, now - min(created)) if created else 0,
            remaining_seconds=remaining_total if remaining_known else None,
            progress=progress, status='running'))
    return views
