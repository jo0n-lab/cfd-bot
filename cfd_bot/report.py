from . import diagnostics as _diagnostics
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .logs import estimate
from .texts import load_text
from .ui import load_ui
from .queueing import job_queue_id


@_diagnostics.trace
def _catalog(run=None, ui=None):
    if ui is not None:
        if _diagnostics.enabled: _diagnostics.step('report._catalog:L12:then')
        return ui
    root = (run or {}).get('case', {}).get('_ui_dir')
    return load_ui(root)


@_diagnostics.trace
def duration(seconds, ui=None):
    ui = ui or load_ui()
    if seconds is None:
        if _diagnostics.enabled: _diagnostics.step('report.duration:L20:then')
        return ui.text('strings.common.unknown')
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return (ui.text('strings.common.duration_hours', hours=h, minutes=m) if h else
            ui.text('strings.common.duration_minutes', minutes=m, seconds=s))


@_diagnostics.trace
def timestamp(value, ui=None):
    ui = ui or load_ui()
    if value is None:
        if _diagnostics.enabled: _diagnostics.step('report.timestamp:L31:then')
        return ui.text('strings.common.unknown')
    return datetime.fromtimestamp(value, ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S KST')


@_diagnostics.trace
def _context(run, ui=None):
    ui = _catalog(run, ui)
    case = run['case']
    t = run.get('telemetry', {})
    elapsed = max(0, run.get('finished', time.time()) - run.get('started', time.time()))
    log_path = t.get('log_path') or run.get('log_path')
    eta = estimate(case, t, elapsed, run.get('runtime_history'))
    eta_text = duration(eta['remaining_seconds'], ui)
    if eta['basis'] == 'configured_seconds':
        if _diagnostics.enabled: _diagnostics.step('report._context:L44:then')
        eta_text = ui.text('scenarios.run.eta.configured', duration=eta_text)
    elif eta['basis'] == 'configured_overrun':
        if _diagnostics.enabled: _diagnostics.step('report._context:L46:then')
        eta_text = ui.text('scenarios.run.eta.configured_overrun')
    elif eta['basis'] == 'recent_log_rate':
        if _diagnostics.enabled: _diagnostics.step('report._context:L48:then')
        source = ui.text('scenarios.run.eta.ticket_source') if eta.get('target_source') == 'ticket' else ''
        eta_text = ui.text('scenarios.run.eta.recent_log', duration=eta_text,
                           intervals=eta['intervals'], source=source, target=f"{eta['target']:g}")
    elif eta['basis'] == 'target_reached':
        if _diagnostics.enabled: _diagnostics.step('report._context:L52:then')
        eta_text = ui.text('scenarios.run.eta.target_reached')
    elif eta['basis'] == 'stop_requested':
        if _diagnostics.enabled: _diagnostics.step('report._context:L54:then')
        eta_text = ui.text('scenarios.run.eta.stop_requested')
    elif eta['basis'] == 'recent_successes':
        if _diagnostics.enabled: _diagnostics.step('report._context:L56:then')
        eta_text = ui.text('scenarios.run.eta.history', duration=eta_text, samples=eta['samples'])
    elif eta['basis'] == 'history_overrun':
        if _diagnostics.enabled: _diagnostics.step('report._context:L58:then')
        eta_text = ui.text('scenarios.run.eta.history_overrun')
    else:
        if _diagnostics.enabled: _diagnostics.step('report._context:L58:else')
        eta_text = ui.text('scenarios.run.eta.insufficient')
    if run['status'] in ('succeeded', 'failed', 'interrupted', 'cancelled'):
        if _diagnostics.enabled: _diagnostics.step('report._context:L62:then')
        eta_text = ui.text('strings.common.terminal')
    errors = '\n'.join(t.get('errors', [])[-5:])
    tail = '\n'.join(t.get('tail', [])[-8:]) if run['status'] in ('failed', 'interrupted') and not errors else ''
    post_errors = '\n'.join(run.get('postprocess_errors', []))
    monitor_errors = '\n'.join(run.get('monitor_errors', []))
    return {
        'case_name': case['name'],
        'status': (ui.text('strings.status.' + run['status'])
                   if ui.has('strings.status.' + run['status']) else run['status']),
        'case_root': case['_root'],
        'run_id': run['id'],
        'owner': run.get('owner', ui.text('strings.common.bot_queue') if not run.get('external')
                         else ui.text('strings.common.unavailable')),
        'cores': str(run.get('actual_cores') or ui.text('strings.common.awaiting_observation')),
        'cpu_list': (run.get('actual_cpu_list') or ui.text('strings.common.awaiting_observation')).replace('-', '~'),
        'started_at': timestamp(run.get('started'), ui),
        'finished_at': timestamp(run.get('finished'), ui),
        'elapsed': duration(elapsed, ui),
        'simulation_time': (ui.text('scenarios.run.simulation_time', time=f"{t['time']:g}")
                            if 'time' in t else ui.text('strings.common.unavailable')),
        'clock_time': duration(t.get('clock'), ui),
        'eta': eta_text,
        'progress': f"{eta['progress']:.1%}" if eta['progress'] is not None else ui.text('strings.common.unknown'),
        'return_code': str(run.get('returncode', ui.text('strings.common.not_collected'))),
        'reason': run.get('reason') or ui.text('strings.common.none'),
        'observed': run.get('observed') or ui.text('strings.common.none'),
        'log_path': str(log_path or ui.text('strings.common.unavailable')),
        'residuals': case.get('residual_pattern') or ui.text('strings.common.unset'),
        'errors': ui.text('scenarios.run.errors', content=errors) if errors else '',
        'tail': ui.text('scenarios.run.tail', content=tail) if tail else '',
        'postprocess_errors': ui.text('scenarios.run.postprocess_errors', content=post_errors) if post_errors else '',
        'monitor_errors': ui.text('scenarios.run.monitor_errors', content=monitor_errors) if monitor_errors else '',
    }


@_diagnostics.trace
def render_run(run, templates=None, kind='detail', ui=None):
    ui = _catalog(run, ui)
    template = (templates or load_text(ui=ui))[kind]
    text = template.format_map(_context(run, ui)).strip()
    while '\n\n\n' in text:
        if _diagnostics.enabled: _diagnostics.step('report.render_run:L102:loop')
        text = text.replace('\n\n\n', '\n\n')
    return text


@_diagnostics.trace
def summary(run):
    return render_run(run)


@_diagnostics.trace
def compact_status(run, ui=None):
    ui = _catalog(run, ui)
    context = _context(run, ui)
    owner = context['owner']
    if run['status'] not in ('starting', 'running', 'postprocessing'):
        if _diagnostics.enabled: _diagnostics.step('report.compact_status:L115:then')
        owner = f"{context['status']} · {owner}"
    return ui.text('scenarios.status.compact', case_name=context['case_name'], owner=owner,
                   cores=context['cores'], cpu_list=context['cpu_list'],
                   started_at=context['started_at'], elapsed=context['elapsed'])


@_diagnostics.trace
def compact_unobserved(case, ui=None):
    ui = ui or load_ui(case.get('_ui_dir'))
    return ui.text('scenarios.status.unobserved', case_name=case['name'])


@_diagnostics.trace
def queue_text(store, enabled=True, ui=None):
    ui = ui or load_ui()
    jobs = store.jobs(('queued', 'starting', 'running', 'postprocessing'))
    lines = [ui.text('menus.queue.title') + ('' if enabled else ui.text('menus.queue.paused_suffix'))]
    if not jobs:
        if _diagnostics.enabled: _diagnostics.step('report.queue_text:L131:then')
        return lines[0] + '\n' + ui.text('menus.queue.empty')

    @_diagnostics.trace
    def detail(job, wait=None):
        case = job['case']
        status = ui.text('strings.status.' + job['status'])
        text = ui.text('menus.queue.job', job_id=job['id'], case_name=case['name'], status=status)
        if case.get('role') == 'child':
            if _diagnostics.enabled: _diagnostics.step('report.queue_text.detail:L138:then')
            text += ui.text('menus.queue.child', macro=case.get('macro_ticket', ''),
                            index=job.get('batch_index', 0) + 1)
        if job['status'] == 'queued' and case.get('cpu_policy') == 'auto':
            if _diagnostics.enabled: _diagnostics.step('report.queue_text.detail:L141:then')
            text += ui.text('menus.queue.separator',
                            value=ui.text('menus.queue.auto_cpu', cores=case['cores']))
        elif case.get('cpu_set'):
            if _diagnostics.enabled: _diagnostics.step('report.queue_text.detail:L144:then')
            text += ui.text('menus.queue.separator', value=ui.text(
                'menus.queue.manual_cpu', cores=case['cores'], cpu_set=case['cpu_set']))
        if job['status'] == 'queued':
            if _diagnostics.enabled: _diagnostics.step('report.queue_text.detail:L147:then')
            expected = estimate(case, {}, 0, store.runtime_history(case)).get('expected_seconds')
            text += ui.text('menus.queue.separator', value=ui.text(
                'menus.queue.timing', wait=duration(wait, ui), expected=duration(expected, ui)))
        if job.get('reason'):
            if _diagnostics.enabled: _diagnostics.step('report.queue_text.detail:L151:then')
            text += ui.text('menus.queue.reason', reason=job['reason'][-500:])
        return text

    active = [job for job in jobs if job['status'] != 'queued']
    if active:
        if _diagnostics.enabled: _diagnostics.step('report.queue_text:L156:then')
        lines += ['', ui.text('menus.queue.active_title')]
        lines.extend(detail(job) for job in active)
    queue_ids = sorted({job_queue_id(job) for job in jobs if job['status'] == 'queued'})
    for queue_id in queue_ids:
        if _diagnostics.enabled: _diagnostics.step('report.queue_text:L160:loop', queue_id=queue_id)
        lane_jobs = [job for job in jobs if job['status'] == 'queued'
                     and job_queue_id(job) == queue_id]
        lines += ['', ui.text('menus.queue.lane_title', queue=queue_id, count=len(lane_jobs))]
        running = next((job for job in active if job.get('priority') != 'run'
                        and job_queue_id(job) == queue_id), None)
        wait = 0
        if running:
            if _diagnostics.enabled: _diagnostics.step('report.queue_text:L167:then')
            eta = estimate(running['case'], running.get('telemetry', {}),
                           time.time() - running.get('started', time.time()),
                           store.runtime_history(running['case'], running.get('actual_cores')))
            wait = eta['remaining_seconds']
        for job in lane_jobs:
            if _diagnostics.enabled: _diagnostics.step('report.queue_text:L172:loop', job=job)
            if job.get('reason') or not enabled:
                if _diagnostics.enabled: _diagnostics.step('report.queue_text:L173:then')
                wait = None
            lines.append(detail(job, wait))
            expected = estimate(job['case'], {}, 0,
                                store.runtime_history(job['case'])).get('expected_seconds')
            wait = wait + expected if wait is not None and expected is not None else None
    lines.append(ui.text('menus.queue.estimate_notice'))
    return '\n'.join(lines)


@_diagnostics.trace
def macro_queue_text(macros, ui=None):
    """Render live macro summaries produced by the shared run view helper."""
    ui = ui or load_ui()
    if not macros:
        if _diagnostics.enabled: _diagnostics.step('report.macro_queue_text:L186:then')
        return ''
    lines = ['', ui.text('menus.queue.macro_title')]
    for macro in macros:
        if _diagnostics.enabled: _diagnostics.step('report.macro_queue_text:L189:loop', macro=macro)
        lines.append(ui.text(
            'menus.queue.macro_line', name=macro['name'], completed=macro['completed'],
            target=macro['target'], elapsed=duration(macro['elapsed_seconds'], ui),
            remaining=duration(macro['remaining_seconds'], ui),
            progress=f"{macro['progress']:.0%}",
            active=macro.get('active_case') or ui.text('strings.common.unavailable')))
    return '\n'.join(lines)
