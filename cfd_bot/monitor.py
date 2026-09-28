import os
import time
import uuid
from pathlib import Path

from .config import cases_for
from .jobs import Scheduler, terminal_event
from .logs import recent_case_log
from .outcomes import decide, wants_event
from .processes import snapshot
from .storage import LIVE
from .tickets import accept_submissions, sync_ticket_states
from .ui import load_ui


def process_started(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        ticks = float(stat[stat.rfind(')') + 2:].split()[19])
        uptime = float(Path('/proc/uptime').read_text().split()[0])
        return time.time() - uptime + ticks / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError):
        return time.time()


def observed_text(record, ui=None):
    ui = ui or load_ui()
    processes = record.get('processes', [])
    cpus = sorted({p['cpu_list'] for p in processes})
    modes = sorted({p['mode'] for p in processes})
    return ui.text('strings.common.process_observation', count=len(processes),
                   modes=','.join(modes), cpus=','.join(cpus))


def observation(record, previous=None, ui=None):
    """Keep the last solver allocation while only a wrapper remains visible."""
    ui = ui or load_ui()
    previous = previous or {}
    cores = record.get('actual_cores') or 0
    cpu_list = record.get('actual_cpu_list')
    return dict(
        observed=observed_text(record, ui),
        owner=record.get('owner') or previous.get('owner', ui.text('strings.common.unavailable')),
        actual_cores=cores or previous.get('actual_cores') or 0,
        actual_cpu_list=(cpu_list if cpu_list and cpu_list != ui.text('strings.common.unspecified')
                         else previous.get('actual_cpu_list', ui.text('strings.common.unspecified'))),
    )


def automatic_case(root, previous=None, ui=None):
    """Build a minimal watcher for an ofps case without a JSON ticket."""
    ui = ui or load_ui()
    root = Path(root).resolve()
    candidates = []
    try:
        for path in root.glob('log*'):
            try:
                if path.is_file() and not path.is_symlink():
                    candidates.append((path.stat().st_mtime_ns, path.name))
            except OSError:
                continue
    except OSError:
        pass
    logs = [name for _, name in sorted(candidates, reverse=True)]
    if not logs and previous:
        logs = previous.get('watcher', {}).get('logs', [])
    if not logs:
        logs = ['log.solver']
    return {
        'version': 1,
        'case_dir': str(root),
        '_root': str(root),
        'name': ui.text('strings.common.unregistered_watch', name=root.name),
        '_ui_dir': str(ui.root),
        'log': logs[0],
        'watcher': {
            'logs': logs,
            'failure': {
                'patterns': [],
                'include_openfoam_defaults': True,
                'updated_files': [],
            },
        },
        'notifications': {
            'events': ['started', 'succeeded', 'failed', 'interrupted'],
        },
        'exports': [],
        'preprocess': [],
        'postprocess': [],
        'residual_pattern': '',
        'command': [],
        'auto_detected': True,
    }


class Monitor:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.ui = load_ui(config.get('_ui_dir'))
        self.scheduler = Scheduler(config, store)

    def tick(self):
        cases = cases_for(self.config)
        registered = {case['_root']: case for case in cases}
        # Failed scans must never be interpreted as disappearance of all solvers.
        current = snapshot(self.config['ofps_command'])
        current['at'] = time.time()
        self.store.put('snapshot', current)
        self.store.put('monitor_error', None)
        accept_submissions(self.config, self.store)
        self.scheduler.recover()
        live_jobs = self.store.jobs(LIVE)
        managed = {j['case_root'] for j in live_jobs}
        for job in live_jobs:
            record = current['cases'].get(job['case_root'])
            if record:
                self.store.update_job(job['id'], expected=LIVE, **observation(record, job, self.ui))
        for case in cases:
            if case['_root'] in managed:
                continue
            self.observe(case, current['cases'].get(case['_root']))
        # /stat already shows every ofps CASE. Apply the same scope to automatic
        # start/finish notifications, including paths without a JSON ticket.
        tracked = set(self.store.get('auto_observed_roots', []))
        automatic = ((set(current['cases']) - set(registered) - managed) | tracked)
        keep = set()
        for root in sorted(automatic):
            if root in registered or root in managed:
                continue
            previous = self.store.get('observed:' + root)
            case = automatic_case(root, previous.get('case') if previous else None, self.ui)
            self.observe(case, current['cases'].get(root))
            state = self.store.get('observed:' + root)
            if root in current['cases'] or (state and state.get('status') in LIVE):
                keep.add(root)
        self.store.put('auto_observed_roots', sorted(keep))
        self.scheduler.tick(current['cases'])
        sync_ticket_states(self.config, self.store, current)

    def observe(self, case, record):
        key = 'observed:' + case['_root']
        previous = self.store.get(key)
        if previous and previous['status'] == 'succeeded':
            self.store.remember_run(previous)
        now = time.time()
        if record:
            if previous is None or previous['status'] not in LIVE:
                members = record.get('supervisors', []) + record.get('processes', [])
                started = min([process_started(p['pid']) for p in members] or [now])
                previous = dict(id=uuid.uuid4().hex[:12], case=case, case_root=case['_root'], created=now, started=started,
                                status='running', telemetry={}, missing=0, external=True)
                if wants_event(case, 'started'):
                    self.store.event(previous['id'] + ':start', self.config['telegram']['chat_ids'],
                                     dict(kind='text', text=self.ui.text(
                                         'scenarios.notifications.external_started', case_name=case['name'],
                                         owner=record.get('owner', self.ui.text('strings.common.unavailable')),
                                         observation=observed_text(record, self.ui))))
            previous.update(missing=0, **observation(record, previous, self.ui))
            previous['case'] = case
            previous['telemetry'], logfile = recent_case_log(
                case, previous.get('telemetry') or None)
            previous['log_path'] = str(logfile)
            self.store.put(key, previous)
            return
        if previous is None:
            return
        previous['case'] = case
        if previous['status'] not in LIVE:
            terminal_event(self.store, previous, self.config['telegram']['chat_ids'], self.ui)
            return
        previous['missing'] = previous.get('missing', 0) + 1
        if previous['missing'] < case['watcher'].get(
                'missing_polls', self.config['missing_polls']):
            self.store.put(key, previous)
            return
        telemetry, logfile = recent_case_log(
            case, previous.get('telemetry') or None, final=True)
        recent = logfile.exists() and logfile.stat().st_mtime >= previous['started']
        status, reason = decide(case, telemetry, previous['started'],
                                external=True, log_fresh=recent)
        previous.update(status=status, reason=reason, telemetry=telemetry, finished=now)
        self.store.put(key, previous)
        terminal_event(self.store, previous, self.config['telegram']['chat_ids'], self.ui)

    def run_once(self):
        try:
            self.tick()
        except Exception as exc:
            message = self.ui.text('strings.common.technical_error',
                                   type=type(exc).__name__, error=exc)
            self.store.put('monitor_error', dict(at=time.time(), message=message))
            # A single event per outage; the key changes after successful recovery.
            outage = self.store.get('outage_id') or uuid.uuid4().hex[:12]
            self.store.put('outage_id', outage)
            self.store.event('monitor:' + outage, self.config['telegram']['chat_ids'],
                             dict(kind='text', text=self.ui.text(
                                 'scenarios.notifications.collection_failed', message=message)))
            return False
        self.store.put('outage_id', None)
        for job in self.store.jobs(LIVE):
            if job.get('started') and wants_event(job['case'], 'started'):
                self.store.event(job['id'] + ':start', self.config['telegram']['chat_ids'],
                                 dict(kind='text', text=self.ui.text(
                                     'scenarios.notifications.queue_started', case_name=job['case']['name'],
                                     cores=job.get('actual_cores') or self.ui.text('strings.common.awaiting_observation'),
                                     cpu_list=job.get('actual_cpu_list') or self.ui.text('strings.common.awaiting_observation'),
                                     job_id=job['id'])))
        return True
