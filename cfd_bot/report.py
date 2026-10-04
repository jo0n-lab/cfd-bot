import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .logs import estimate
from .texts import load_text
from .ui import load_ui


def _catalog(run=None, ui=None):
    if ui is not None:
        return ui
    root = (run or {}).get('case', {}).get('_ui_dir')
    return load_ui(root)


def duration(seconds, ui=None):
    ui = ui or load_ui()
    if seconds is None:
        return ui.text('strings.common.unknown')
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return (ui.text('strings.common.duration_hours', hours=h, minutes=m) if h else
            ui.text('strings.common.duration_minutes', minutes=m, seconds=s))


def timestamp(value, ui=None):
    ui = ui or load_ui()
    if value is None:
        return ui.text('strings.common.unknown')
    return datetime.fromtimestamp(value, ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S KST')


def _context(run, ui=None):
    ui = _catalog(run, ui)
    case = run['case']
    t = run.get('telemetry', {})
    elapsed = max(0, run.get('finished', time.time()) - run.get('started', time.time()))
    log_path = t.get('log_path') or run.get('log_path')
    eta = estimate(case, t, elapsed, run.get('runtime_history'))
    eta_text = duration(eta['remaining_seconds'], ui)
    if eta['basis'] == 'configured_seconds':
        eta_text = ui.text('scenarios.run.eta.configured', duration=eta_text)
    elif eta['basis'] == 'configured_overrun':
        eta_text = ui.text('scenarios.run.eta.configured_overrun')
    elif eta['basis'] == 'recent_log_rate':
        source = ui.text('scenarios.run.eta.ticket_source') if eta.get('target_source') == 'ticket' else ''
        eta_text = ui.text('scenarios.run.eta.recent_log', duration=eta_text,
                           intervals=eta['intervals'], source=source, target=f"{eta['target']:g}")
    elif eta['basis'] == 'target_reached':
        eta_text = ui.text('scenarios.run.eta.target_reached')
    elif eta['basis'] == 'stop_requested':
        eta_text = ui.text('scenarios.run.eta.stop_requested')
    elif eta['basis'] == 'recent_successes':
        eta_text = ui.text('scenarios.run.eta.history', duration=eta_text, samples=eta['samples'])
    elif eta['basis'] == 'history_overrun':
        eta_text = ui.text('scenarios.run.eta.history_overrun')
    else:
        eta_text = ui.text('scenarios.run.eta.insufficient')
    if run['status'] in ('succeeded', 'failed', 'interrupted', 'cancelled'):
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


def render_run(run, templates=None, kind='detail', ui=None):
    ui = _catalog(run, ui)
    template = (templates or load_text(ui=ui))[kind]
    text = template.format_map(_context(run, ui)).strip()
    while '\n\n\n' in text:
        text = text.replace('\n\n\n', '\n\n')
    return text


def summary(run):
    return render_run(run)


def compact_status(run, ui=None):
    ui = _catalog(run, ui)
    context = _context(run, ui)
    owner = context['owner']
    if run['status'] not in ('starting', 'running', 'postprocessing'):
        owner = f"{context['status']} · {owner}"
    return ui.text('scenarios.status.compact', case_name=context['case_name'], owner=owner,
                   cores=context['cores'], cpu_list=context['cpu_list'],
                   started_at=context['started_at'], elapsed=context['elapsed'])


def compact_unobserved(case, ui=None):
    ui = ui or load_ui(case.get('_ui_dir'))
    return ui.text('scenarios.status.unobserved', case_name=case['name'])


def queue_text(store, enabled=True, ui=None):
    ui = ui or load_ui()
    jobs = store.jobs(('queued', 'starting', 'running', 'postprocessing'))
    lines = [ui.text('menus.queue.title') + ('' if enabled else ui.text('menus.queue.paused_suffix'))]
    if not jobs:
        return lines[0] + '\n' + ui.text('menus.queue.empty')
    # Conservative FIFO estimate: sum all active remaining time, then queued durations.
    wait = 0
    for job in jobs:
        if job['status'] != 'queued':
            eta = estimate(job['case'], job.get('telemetry', {}), time.time() - job.get('started', time.time()),
                           store.runtime_history(job['case'], job.get('actual_cores')))
            remaining = eta['remaining_seconds']
            wait = wait + remaining if wait is not None and remaining is not None else None
    for job in jobs:
        case = job['case']
        status = ui.text('strings.status.' + job['status'])
        detail = ui.text('menus.queue.job', job_id=job['id'], case_name=case['name'], status=status)
        if case.get('role') == 'child':
            detail += ui.text('menus.queue.child', macro=case.get('macro_ticket', ''),
                              index=job.get('batch_index', 0) + 1)
        if job['status'] == 'queued' and case.get('cpu_policy') == 'auto':
            detail += ui.text('menus.queue.separator',
                              value=ui.text('menus.queue.auto_cpu', cores=case['cores']))
        elif case.get('cpu_set'):
            detail += ui.text('menus.queue.separator', value=ui.text(
                'menus.queue.manual_cpu', cores=case['cores'], cpu_set=case['cpu_set']))
        if job['status'] == 'queued':
            if job.get('reason') or not enabled:
                wait = None
            expected = estimate(case, {}, 0, store.runtime_history(case)).get('expected_seconds')
            detail += ui.text('menus.queue.separator', value=ui.text(
                'menus.queue.timing', wait=duration(wait, ui), expected=duration(expected, ui)))
            wait = wait + expected if wait is not None and expected is not None else None
        if job.get('reason'):
            detail += ui.text('menus.queue.reason', reason=job['reason'][-500:])
        lines.append(detail)
    lines.append(ui.text('menus.queue.estimate_notice'))
    return '\n'.join(lines)


def macro_queue_text(macros, ui=None):
    """Render live macro summaries produced by the shared run view helper."""
    ui = ui or load_ui()
    if not macros:
        return ''
    lines = ['', ui.text('menus.queue.macro_title')]
    for macro in macros:
        lines.append(ui.text(
            'menus.queue.macro_line', name=macro['name'], completed=macro['completed'],
            target=macro['target'], elapsed=duration(macro['elapsed_seconds'], ui),
            remaining=duration(macro['remaining_seconds'], ui),
            progress=f"{macro['progress']:.0%}",
            active=macro.get('active_case') or ui.text('strings.common.unavailable')))
    return '\n'.join(lines)
