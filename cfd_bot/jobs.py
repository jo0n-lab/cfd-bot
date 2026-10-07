"""Persistent FIFO scheduler and detached exit-code-recording worker."""
import os
import signal
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .artifacts import freeze_exports
from .config import cpu_set, load_case
from .catalog import ticket_index
from .cpu_allocation import allocate_cpus, format_cpus, managed_cpus, occupied_cpus
from .execution import apply_execution_settings, execution_case, openfoam_environment
from .logs import (case_logs, finish_log, read_log, recent_case_log,
                   select_case_log, start_cursor)
from .outcomes import decide, wants_event
from .processes import check_cpus, identity, snapshot
from .storage import LIVE, TERMINAL, Store
from .queueing import (borrowing_plan, job_queue_id, queue_heads,
                       registered_profiles)
from .ui import load_ui


def terminal_event(store, job, chats, ui=None):
    ui = ui or load_ui(job.get('case', {}).get('_ui_dir'))
    if job['status'] not in ('succeeded', 'failed', 'interrupted'):
        return
    store.remember_run(job)
    if not wants_event(job['case'], job['status']):
        store.mark_terminal_published(job['id'])
        return
    # Attachment snapshots are taken before the next FIFO job is admitted.
    payload = store.get('event:' + job['id'])
    if payload is None:
        files, notes = freeze_exports(job['case'], store.root / 'events' / job['id'],
                                     job.get('started', job['created']), job['status'])
        payload = dict(kind='terminal', run=job, files=files, notes=notes)
        store.put('event:' + job['id'], payload)
    store.event(job['id'] + ':terminal', chats, payload)
    store.mark_terminal_published(job['id'])


def scheduling_candidates(jobs, active=()):
    """Runnable batch heads first, then one head per idle named queue."""
    active_batches = {job.get('batch') for job in active if job.get('batch')}
    active_queues = {job_queue_id(job) for job in active if job.get('priority') != 'run'}
    jobs = [job for job in jobs if not job.get('batch') or job['batch'] not in active_batches]
    immediate = []
    batches = set()
    for job in sorted((job for job in jobs if job.get('priority') == 'run'),
                      key=lambda item: item['created']):
        batch = job.get('batch')
        if batch and batch in batches:
            continue
        if batch:
            batches.add(batch)
        immediate.append(job)
    queued = [job for job in jobs if (job.get('priority') != 'run'
                                      and job_queue_id(job) not in active_queues)]
    return immediate + sorted(queue_heads(queued), key=lambda item: item['created'])


class Scheduler:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.ui = load_ui(config.get('_ui_dir'))
        self.children = []

    def _borrow_state(self, queued, active):
        """Maintain the single drain claim and post-borrow donor turns."""
        fairness = self.store.get('queue_fair_turns', {})
        queued_ids = {job_queue_id(job) for job in queued}
        fairness = {qid: jid for qid, jid in fairness.items() if qid in queued_ids}
        for job in self.store.jobs(TERMINAL):
            donors = job.get('borrowed_queues', [])
            if not donors or job.get('borrow_released'):
                continue
            for qid in donors:
                if qid in queued_ids:
                    fairness[qid] = job['id']
            self.store.update_job(job['id'], expected=TERMINAL, borrow_released=True)
        self.store.put('queue_fair_turns', fairness)

        claim = self.store.get('queue_drain_claim')
        all_by_id = {job['id']: job for job in (*queued, *active)}
        if claim and claim.get('job_id') not in all_by_id:
            claim = None
            self.store.put('queue_drain_claim', None)
        return claim, fairness

    def _claim_dynamic(self, candidates, profiles, pool, claim, fairness):
        if claim:
            return claim
        for job in sorted(candidates, key=lambda item: item['created']):
            if not job.get('dynamic_cores'):
                continue
            plan = borrowing_plan(job, profiles, pool)
            if not plan or not plan['oversized']:
                continue
            if any(qid in fairness for qid in plan['donors']):
                continue
            claim = {'job_id': job['id'], 'queue_id': job_queue_id(job),
                     'donors': plan['donors'], 'allowed': format_cpus(plan['allowed'])}
            self.store.put('queue_drain_claim', claim)
            return claim
        return None

    def _assign_queue_profiles(self, candidates, profiles, pool, observed, active):
        """Assign fixed queue CPU positions from the head job's core count.

        New tickets store only a queue id.  A profile exists while queued or
        active work needs it and is recreated from the next head when idle.
        Legacy jobs that already carry queue_cpu_set keep their old contract.
        """
        blocked = set()
        for job in sorted(candidates, key=lambda item: item['created']):
            if (job.get('priority') == 'run' or not job.get('queue_id')
                    or job.get('dynamic_cores')
                    or job.get('queue_cpu_set')):
                continue
            qid = job_queue_id(job)
            try:
                case = execution_case(job['case'])
                required = int(case['cores']) + int(
                    bool(case.get('monitoring', {}).get('allocate_cpu')))
            except (OSError, ValueError) as exc:
                blocked.add(job['id'])
                self.store.update_job(job['id'], expected=('queued',), reason=str(exc))
                continue
            assigned = profiles.get(qid)
            if any(active_job.get('priority') != 'run'
                   and job_queue_id(active_job) == qid for active_job in active):
                # Do not copy the previous head's profile into the next job.
                # Once the active head finishes, this job derives a fresh
                # profile whose size matches its own NP.
                blocked.add(job['id'])
                continue
            if assigned is not None and required > len(assigned):
                blocked.add(job['id'])
                self.store.update_job(
                    job['id'], expected=('queued',),
                    reason=self.ui.text('scenarios.jobs.queue_resize_waiting',
                                        cores=required, current=len(assigned)))
                continue
            if assigned is None:
                reserved = set().union(*profiles.values()) if profiles else set()
                try:
                    allocation = allocate_cpus(required, observed, active,
                                               allowed=set(pool) - reserved)
                    assigned = cpu_set(allocation['cpu_set'])
                except (OSError, ValueError, RuntimeError) as exc:
                    blocked.add(job['id'])
                    self.store.update_job(job['id'], expected=('queued',), reason=str(exc))
                    continue
                profiles[qid] = assigned
            value = format_cpus(assigned)
            updated = self.store.update_job(job['id'], expected=('queued',),
                                            queue_cpu_set=value, reason='')
            if updated is None:
                blocked.add(job['id'])
                continue
            job['queue_cpu_set'] = value
        return profiles, blocked

    def recover(self):
        self.children = [p for p in self.children if p.poll() is None]
        for job in self.store.jobs(LIVE):
            now = time.time()
            if job['status'] == 'starting' and now - job.get('claimed', now) < 60:
                continue
            alive = job.get('worker_identity') and identity(job.get('worker_pid')) == job['worker_identity']
            if alive:
                continue
            hook_alive = job.get('hook_identity') and identity(job.get('hook_pid')) == job['hook_identity']
            if hook_alive:
                self.store.update_job(job['id'], expected=LIVE,
                                      reason=self.ui.text('scenarios.jobs.hook_alive'))
                continue
            solver_alive = job.get('solver_identity') and identity(job.get('solver_pid')) == job['solver_identity']
            if solver_alive:
                self.store.update_job(job['id'], expected=LIVE,
                                      reason=self.ui.text('scenarios.jobs.solver_alive'))
                self.store.event(job['id'] + ':worker_lost', self.config['telegram']['chat_ids'],
                                 dict(kind='text', text=self.ui.text(
                                     'scenarios.notifications.worker_lost_solver_alive',
                                     case_name=job['case']['name'])))
                continue
            monitor_alive = (job.get('monitor_identity') and
                             identity(job.get('monitor_pid')) == job['monitor_identity'])
            if monitor_alive:
                try:
                    os.killpg(job['monitor_pid'], signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if not job.get('started') or job.get('phase') == 'preprocess':
                self.store.update_job(
                    job['id'], expected=LIVE, status='failed', finished=now,
                    reason=self.ui.text('scenarios.jobs.preprocess_lost' if job.get('phase') == 'preprocess'
                                        else 'scenarios.jobs.startup_lost'))
                continue
            try:
                telemetry, logfile = recent_case_log(
                    job['case'], job.get('telemetry') or None, final=True)
                recent = logfile.exists() and logfile.stat().st_mtime >= job['started']
                status, reason = decide(job['case'], telemetry, job['started'],
                                        log_fresh=recent)
                self.store.update_job(
                    job['id'], expected=LIVE, status=status, finished=now,
                    solver_finished=now, telemetry=telemetry,
                    log_path=str(logfile), reason=reason)
            except (OSError, ValueError) as exc:
                self.store.update_job(
                    job['id'], expected=LIVE, status='failed', finished=now,
                    reason=self.ui.text('scenarios.jobs.verdict_failed', error=exc))

    def tick(self, observed):
        self.recover()
        for job in self.store.unpublished_terminal_jobs():
            terminal_event(self.store, job, self.config['telegram']['chat_ids'], self.ui)
        active = self.store.jobs(LIVE)
        if not self.config['scheduler']['enabled'] or self.store.get('queue_paused', False):
            return
        all_queued = self.store.jobs(('queued',))
        queued = scheduling_candidates(all_queued, active)
        pool = managed_cpus(self.config)
        # Ticket profiles remain reserved even while their queue is empty.
        # Job snapshots cover accepted work if the source ticket was renamed.
        profiles = registered_profiles((*all_queued, *active),
                                       ticket_index(self.config).queue_profiles())
        profiles, profile_waiting = self._assign_queue_profiles(
            queued, profiles, pool, observed, active)
        claim, fairness = self._borrow_state(all_queued, active)
        claim = self._claim_dynamic(queued, profiles, pool, claim, fairness)
        for job in queued:
            if len(active) >= self.config['scheduler']['max_parallel']:
                return
            case = job['case']
            qid = job_queue_id(job)
            if job['id'] in profile_waiting:
                continue
            if (job.get('priority') != 'run'
                    and any(active_job.get('priority') != 'run'
                            and job_queue_id(active_job) == qid for active_job in active)):
                continue
            if (claim and qid in claim.get('donors', [])
                    and job['id'] != claim.get('job_id')):
                continue
            if job.get('batch') and any(j.get('batch') == job['batch'] for j in active):
                continue
            if case['_root'] in observed:
                self.store.update_job(job['id'], expected=('queued',),
                                      reason=self.ui.text('scenarios.jobs.external_running'))
                continue
            external = self.store.get('observed:' + case['_root'])
            if external and external['status'] in LIVE:
                self.store.update_job(job['id'], expected=('queued',),
                                      reason=self.ui.text('scenarios.jobs.external_finishing'))
                continue
            if (case.get('role') == 'child' and external and external['status'] == 'succeeded'
                    and external.get('finished', 0) >= job['created']):
                self.store.update_job(job['id'], expected=('queued',), status='cancelled',
                                      finished=time.time(), reason=self.ui.text('scenarios.jobs.external_duplicate'))
                self.store.event(job['id'] + ':external_complete', self.config['telegram']['chat_ids'],
                                 dict(kind='text', text=self.ui.text(
                                     'scenarios.notifications.external_duplicate_skipped', case_name=case['name'])))
                continue
            try:
                if case.get('_config') and not Path(case['_config']).is_file():
                    renamed = ticket_index(self.config).lookup(case['_root'])
                    if renamed is None:
                        self.store.update_job(job['id'], expected=('queued',), status='cancelled',
                                              finished=time.time(), reason=self.ui.text('scenarios.jobs.ticket_deleted'))
                        self.store.event(job['id'] + ':missing_ticket', self.config['telegram']['chat_ids'],
                                         dict(kind='text', text=self.ui.text(
                                             'scenarios.notifications.ticket_deleted', case_name=case['name'])))
                        continue
                    case = renamed
                if case.get('_config') and Path(case['_config']).is_file():
                    latest = load_case(case['_config'])
                    latest['_ui_dir'] = self.config['_ui_dir']
                    if latest['_root'] != case['_root']:
                        raise ValueError(self.ui.text('scenarios.jobs.case_path_changed'))
                    case = latest
                if case.get('role') == 'child':
                    parent = Path(case['_config']).parent / case['macro_ticket']
                    if not parent.is_file():
                        raise ValueError(self.ui.text('scenarios.jobs.macro_missing'))
                case = execution_case(case)
            except (OSError, ValueError) as exc:
                reason = str(exc)
                self.store.update_job(job['id'], expected=('queued',), reason=reason)
                self.store.event(job['id'] + ':resources:' + reason,
                                 self.config['telegram']['chat_ids'],
                                 dict(kind='text', text=self.ui.text(
                                     'scenarios.notifications.waiting', case_name=case['name'], reason=reason)))
                continue
            automatic = case.get('cpu_policy') == 'auto'
            monitor_requested = bool(case.get('monitoring', {}).get('allocate_cpu'))
            allowed = pool
            borrowed = []
            if (job.get('dynamic_cores')
                    or (job.get('priority') != 'run' and job.get('queue_cpu_set'))):
                # The ticket may derive NP from the live case at dispatch
                # time, so plan with the resolved execution contract.
                plan = borrowing_plan(dict(job, case=case), profiles, pool)
                if plan is None:
                    self.store.update_job(job['id'], expected=('queued',),
                                          reason=self.ui.text('scenarios.jobs.queue_quota_insufficient'))
                    continue
                claimed_dynamic = bool(claim and claim.get('job_id') == job['id'])
                if plan['oversized'] or claimed_dynamic:
                    if not claim or claim.get('job_id') != job['id']:
                        continue
                    borrowed = list(claim.get('donors', plan['donors']))
                    if any(job_queue_id(active_job) in borrowed for active_job in active):
                        self.store.update_job(job['id'], expected=('queued',),
                                              reason=self.ui.text('scenarios.jobs.queue_draining',
                                                                  queues=', '.join(borrowed)))
                        continue
                allowed = cpu_set(claim['allowed']) if claimed_dynamic else plan['allowed']
                automatic = True
                case['cpu_policy'] = 'auto'
            if not automatic:
                requested = cpu_set(case['cpu_set'])
                unavailable = requested - pool
                if unavailable:
                    self.store.update_job(job['id'], expected=('queued',),
                                          reason=self.ui.text('scenarios.jobs.cpu_unavailable',
                                                              cpus=','.join(map(str, sorted(unavailable)))))
                    continue
                if requested & occupied_cpus({}, active):
                    self.store.update_job(job['id'], expected=('queued',),
                                          reason=self.ui.text('scenarios.jobs.cpu_reserved'))
                    continue
            env = os.environ.copy()
            # Neither sourcing OpenFOAM nor the detached worker needs the bot token.
            env.pop(self.config['telegram']['token_env'], None)
            try:
                env = openfoam_environment(self.config['scheduler'].get('openfoam_bashrc'), env)
            except (OSError, ValueError) as exc:
                reason = str(exc)
                self.store.update_job(job['id'], expected=('queued',), reason=reason)
                self.store.event(job['id'] + ':environment:' + reason,
                                 self.config['telegram']['chat_ids'],
                                 dict(kind='text', text=self.ui.text(
                                     'scenarios.notifications.waiting_suffix', case_name=case['name'], reason=reason)))
                continue
            # Environment setup may take time; inspect live CPU ownership last.
            try:
                live = {}
                if automatic or monitor_requested:
                    live = snapshot(self.config['ofps_command'])['cases']
                    if case['_root'] in live:
                        raise ValueError(self.ui.text('scenarios.jobs.external_running'))
                if automatic:
                    allocation = allocate_cpus(case['cores'] + int(monitor_requested), live, active,
                                               allowed=allowed)
                    selected = sorted(cpu_set(allocation['cpu_set']))
                    case['cpu_set'] = format_cpus(selected[:case['cores']])
                    if monitor_requested:
                        case['monitor_cpu'] = str(selected[case['cores']])
                elif monitor_requested:
                    allocation = allocate_cpus(1, live, active + [{'case': case}], allowed=pool)
                    case['monitor_cpu'] = allocation['cpu_set']
                safe, report = check_cpus(self.config['ofps_command'], case)
                if safe and monitor_requested:
                    safe, report = check_cpus(
                        self.config['ofps_command'],
                        dict(case, cpu_set=case['monitor_cpu'], allow_cross_socket=True))
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                safe, report = False, str(exc)
            if not safe:
                self.store.update_job(job['id'], expected=('queued',), reason=report)
                self.store.event(job['id'] + ':cpu_wait', self.config['telegram']['chat_ids'],
                                 dict(kind='text', text=self.ui.text(
                                     'scenarios.notifications.cpu_policy_waiting',
                                     case_name=case['name'], report=report)))
                continue
            claimed = self.store.update_job(job['id'], expected=('queued',), status='starting',
                                            claimed=time.time(), reason='', case=case,
                                            borrowed_queues=borrowed,
                                            openfoam_bashrc=self.config['scheduler'].get('openfoam_bashrc'),
                                            notification_chats=self.config['telegram']['chat_ids'])
            if claimed is None:
                continue
            if qid in fairness and not borrowed:
                fairness.pop(qid, None)
                self.store.put('queue_fair_turns', fairness)
            folder = self.store.root / 'jobs' / job['id']
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            package_root = str(Path(__file__).resolve().parent.parent)
            env['PYTHONPATH'] = package_root + os.pathsep + env.get('PYTHONPATH', '')
            # The bot token is unnecessary in solver/worker environments.
            env.pop(self.config['telegram']['token_env'], None)
            try:
                with (folder / 'worker.log').open('ab') as output:
                    child = subprocess.Popen([sys.executable, '-m', 'cfd_bot', '_worker',
                                              '--state', str(self.store.root), '--job', job['id']],
                                             stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                             start_new_session=True, env=env)
                self.children.append(child)
                active.append(claimed)
            except OSError as exc:
                failed = self.store.update_job(job['id'], expected=('starting',), status='failed',
                                               finished=time.time(), reason=self.ui.text(
                                                   'scenarios.jobs.worker_start_failed', error=exc))
                terminal_event(self.store, failed, self.config['telegram']['chat_ids'], self.ui)


def _retry_locked(operation, *args, **kwargs):
    """Keep a live child running while a transient SQLite writer lock clears."""
    while True:
        try:
            return operation(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if 'locked' not in message and 'busy' not in message:
                raise
            time.sleep(0.5)


def run_hook(command, root, timeout, output, *, env=None, label=None, on_start=None, ui=None):
    ui = ui or load_ui()
    label = label or ui.text('scenarios.jobs.postprocess')
    try:
        child = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                 stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        raise RuntimeError(ui.text('scenarios.jobs.hook_failed', label=label, error=exc)) from exc
    try:
        if on_start:
            on_start(child)
        code = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        raise RuntimeError(ui.text('scenarios.jobs.hook_timeout', label=label, timeout=timeout)) from None
    except BaseException:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        raise
    if code:
        raise RuntimeError(ui.text('scenarios.jobs.hook_exit', label=label, code=code,
                                   command=shlex.join(command)))


def run_case_hooks(case, stage, store, jid, folder, env):
    ui = load_ui(case.get('_ui_dir'))
    errors = []
    if not case.get(stage):
        return errors
    label = ui.text('scenarios.jobs.preprocess' if stage == 'preprocess' else 'scenarios.jobs.postprocess')
    with (folder / f'{stage}.log').open('ab') as output:
        for hook in case[stage]:
            command = list(hook['command'])
            # A bare Allclean/Allpost (or another case-owned script) runs from the case.
            if '/' not in command[0] and (Path(case['_root']) / command[0]).is_file():
                command[0] = './' + command[0]
            output.write((f'[{label}] {shlex.join(command)}\n').encode())
            output.flush()
            try:
                run_hook(['taskset', '-c', case['cpu_set']] + command, case['_root'],
                         hook['timeout_seconds'], output, env=env, label=label,
                         ui=ui,
                         on_start=lambda child: _retry_locked(
                             store.update_job, jid, expected=LIVE,
                             hook_pid=child.pid, hook_identity=identity(child.pid)))
            except (OSError, RuntimeError) as exc:
                if stage == 'preprocess':
                    raise
                errors.append(str(exc))
            finally:
                _retry_locked(store.update_job, jid, expected=LIVE,
                              hook_pid=None, hook_identity=None)
    return errors


def _case_command(case, command):
    command = list(command)
    if '/' not in command[0] and (Path(case['_root']) / command[0]).is_file():
        command[0] = './' + command[0]
    return command


def _stop_child(child, timeout=10):
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def worker(state_dir, jid):
    store = Store(state_dir)
    job = store.job(jid)
    case = job['case']
    ui = load_ui(case.get('_ui_dir'))
    watcher = case['watcher']
    folder = store.root / 'jobs' / jid
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = time.time()
    job = store.update_job(jid, expected=('starting',), status='running', started=now,
                           worker_pid=os.getpid(), worker_identity=identity(os.getpid()), phase='preprocess')
    if job is None:
        return 1
    solver = monitor = None
    monitor_output = None
    try:
        apply_execution_settings(case, folder)
        env = os.environ.copy()
        env['CFD_BOT_JOB_ID'] = jid
        env['CFD_BOT_CASE_DIR'] = case['_root']
        env['NP'] = str(case['cores'])
        env['CPU_SET'] = case['cpu_set']
        run_case_hooks(case, 'preprocess', store, jid, folder, env)
        # Allclean may remove old logs. Capture cursors only after preprocessing.
        configured_logs = case_logs(case)
        log_states = {str(path): start_cursor(path) for path in configured_logs}
        initial_mtimes = {
            str(path): path.stat().st_mtime_ns if path.exists() else None
            for path in configured_logs
        }
        runner_log = folder / 'command.log'
        telemetry = None
        wrapper_telemetry = None
        _retry_locked(store.update_job, jid, expected=('running',), phase='solver')
        with runner_log.open('wb') as output:
            # Inherited affinity also constrains programs launched by Allrun.
            command = ['taskset', '-c', case['cpu_set']] + case['command']
            solver = subprocess.Popen(command, cwd=case['_root'], env=env, stdin=subprocess.DEVNULL,
                                      stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            _retry_locked(store.update_job, jid, expected=('running',),
                          solver_pid=solver.pid, solver_identity=identity(solver.pid))
            monitoring = case.get('monitoring')
            monitor_ended_early = False
            if monitoring:
                monitor_env = dict(env, TCB_MONITORED_SOLVER_PID=str(solver.pid),
                                   CFD_BOT_MONITOR_CPU=case['monitor_cpu'])
                monitor_command = (['taskset', '-c', case['monitor_cpu']] +
                                   _case_command(case, monitoring['command']))
                monitor_output = (folder / 'monitor.log').open('ab')
                monitor_output.write((f"[monitor] {shlex.join(monitor_command)}\n").encode())
                monitor_output.flush()
                monitor = subprocess.Popen(
                    monitor_command, cwd=case['_root'], env=monitor_env,
                    stdin=subprocess.DEVNULL, stdout=monitor_output,
                    stderr=subprocess.STDOUT, start_new_session=True)
                _retry_locked(store.update_job, jid, expected=('running',),
                              monitor_pid=monitor.pid, monitor_identity=identity(monitor.pid),
                              monitor_cpu=case['monitor_cpu'])
            if wants_event(case, 'started'):
                notice = ('scenarios.notifications.queue_started_monitor'
                          if monitoring else 'scenarios.notifications.queue_started')
                _retry_locked(
                    store.event, jid + ':start', job.get('notification_chats', []),
                    dict(kind='text', text=ui.text(
                        notice, case_name=case['name'], cores=case['cores'],
                        cpu_list=case['cpu_set'], monitor_cpu=case.get('monitor_cpu', ''),
                        job_id=jid)))
            while True:
                rc = solver.poll()
                wrapper_telemetry = read_log(runner_log, wrapper_telemetry,
                                             final=rc is not None,
                                             watcher=watcher)
                configured_log = select_case_log(case, changed_from=initial_mtimes)
                if configured_log is not None:
                    telemetry = read_log(configured_log, log_states[str(configured_log)],
                                         final=rc is not None,
                                         watcher=watcher)
                    telemetry['log_path'] = str(configured_log)
                    log_states[str(configured_log)] = telemetry
                    current, log_path = telemetry, str(configured_log)
                else:
                    current, log_path = wrapper_telemetry, str(runner_log)
                _retry_locked(store.update_job, jid, expected=('running',),
                              telemetry=current, log_path=log_path)
                if rc is not None:
                    break
                if monitor is not None and monitor.poll() is not None:
                    monitor_ended_early = True
                time.sleep(0.5)
        monitor_errors = []
        monitor_rc = None
        if monitor is not None:
            try:
                monitor_rc = monitor.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _stop_child(monitor)
                monitor_rc = monitor.returncode
                monitor_errors.append(ui.text('scenarios.jobs.hook_timeout',
                                               label=ui.text('scenarios.jobs.monitor'), timeout=30))
            if monitor_ended_early:
                monitor_errors.append(ui.text('scenarios.jobs.monitor_ended_early', code=monitor_rc))
            elif monitor_rc:
                monitor_errors.append(ui.text(
                    'scenarios.jobs.hook_exit', label=ui.text('scenarios.jobs.monitor'),
                    code=monitor_rc, command=shlex.join(monitoring['command'])))
            _retry_locked(store.update_job, jid, expected=('running',),
                          monitor_returncode=monitor_rc, monitor_finished=time.time(),
                          monitor_errors=monitor_errors)
            monitor_output.close()
            monitor_output = None
        current = finish_log(log_path, current, watcher=watcher)
        wrapper_telemetry = finish_log(runner_log, wrapper_telemetry,
                                       watcher=watcher)
        errors = current.get('errors', []) + wrapper_telemetry.get('errors', [])
        current['errors'] = list(dict.fromkeys(errors))[-12:]
        status, reason = decide(case, current, now, returncode=rc)
        _retry_locked(store.update_job, jid, expected=('running',), telemetry=current,
                      returncode=rc, solver_finished=time.time())
        post_errors = []
        if status == 'succeeded' and case['postprocess']:
            _retry_locked(store.update_job, jid, expected=('running',),
                          status='postprocessing', phase='postprocess')
            post_errors = run_case_hooks(case, 'postprocess', store, jid, folder, env)
        # Freeze files before publishing the terminal state (which releases queue slots).
        files, notes = freeze_exports(case, store.root / 'events' / jid, now,
                                      status)
        job = _retry_locked(store.job, jid)
        job.update(status=status, finished=time.time(), reason=reason, telemetry=current,
                   postprocess_errors=post_errors, monitor_errors=monitor_errors,
                   monitor_returncode=monitor_rc, returncode=rc)
        _retry_locked(store.put, 'event:' + jid,
                      dict(kind='terminal', run=job, files=files, notes=notes))
        _retry_locked(store.update_job, jid, expected=LIVE, status=status,
                      finished=job['finished'], reason=reason, telemetry=current,
                      postprocess_errors=post_errors, monitor_errors=monitor_errors,
                      monitor_returncode=monitor_rc, returncode=rc)
        _retry_locked(store.remember_run, job)
        return 0 if status == 'succeeded' else 1
    except Exception as exc:
        _stop_child(solver)
        _stop_child(monitor)
        if monitor_output is not None:
            monitor_output.close()
        _retry_locked(store.update_job, jid, expected=LIVE, status='failed',
                      finished=time.time(),
                      reason=ui.text('scenarios.jobs.worker_error', error=exc))
        return 1
