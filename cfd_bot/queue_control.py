"""Shared queue controls used by Telegram, native GUI, web and CLI."""
from . import diagnostics as _diagnostics

import os
import signal
import time

from .processes import identity
from .ui import load_ui


@_diagnostics.trace
def cancel_queued_jobs(store, job_ids, *, ui=None):
    """Cancel every selected job that is still queued in one DB transaction."""
    if not isinstance(job_ids, (list, tuple, set)):
        if _diagnostics.detailed: _diagnostics.step('queue_control.cancel_queued_jobs:L8:then')
        job_ids = []
    ids = list(dict.fromkeys(str(job_id) for job_id in job_ids if str(job_id)))
    if not ids:
        if _diagnostics.detailed: _diagnostics.step('queue_control.cancel_queued_jobs:L11:then')
        catalog = ui or load_ui()
        raise ValueError(catalog.text('scenarios.diagnostics.queue.selection_required'))
    return store.cancel_queued(ids)


@_diagnostics.trace
def interrupt_process_groups(job, signum=signal.SIGTERM):
    """Signal only session leaders whose current /proc identity still matches."""
    signalled = []
    seen = set()
    for kind in ('hook', 'monitor', 'solver'):
        if _diagnostics.detailed: _diagnostics.step('queue_control.interrupt_process_groups:M30:loop', kind=kind)
        pid = job.get(kind + '_pid')
        recorded = job.get(kind + '_identity')
        if not isinstance(pid, int) or pid <= 1 or pid in seen or not recorded:
            if _diagnostics.detailed: _diagnostics.step('queue_control.interrupt_process_groups:M33:then')
            continue
        seen.add(pid)
        if identity(pid) != recorded:
            if _diagnostics.detailed: _diagnostics.step('queue_control.interrupt_process_groups:M36:then')
            continue
        try:
            if os.getpgid(pid) != pid:
                if _diagnostics.detailed: _diagnostics.step('queue_control.interrupt_process_groups:M39:then')
                continue
            _diagnostics.signal_process(pid, signum)
            signalled.append(pid)
        except ProcessLookupError:
            if _diagnostics.enabled: _diagnostics.step('queue_control.interrupt_process_groups:M43:except')
            continue
    return signalled


@_diagnostics.trace
def interrupt_running_job(store, jid, *, ui=None):
    """Request interruption without releasing the job's CPU reservation early."""
    catalog = ui or load_ui()
    job = store.request_interruption(
        str(jid), catalog.text('scenarios.jobs.user_interrupt_requested'))
    if job is None:
        if _diagnostics.detailed: _diagnostics.step('queue_control.interrupt_running_job:M53:then')
        return None
    interrupt_process_groups(job)
    worker_alive = (job.get('worker_identity') and
                    identity(job.get('worker_pid')) == job['worker_identity'])
    child_alive = any(
        job.get(kind + '_identity') and
        identity(job.get(kind + '_pid')) == job[kind + '_identity']
        for kind in ('hook', 'monitor', 'solver'))
    if not worker_alive and not child_alive:
        if _diagnostics.detailed: _diagnostics.step('queue_control.interrupt_running_job:M62:then')
        completed = store.update_job(
            job['id'], expected=('stopping',), status='interrupted', finished=time.time(),
            reason=catalog.text('scenarios.jobs.user_interrupted'))
        return completed or store.job(job['id'])
    return store.job(job['id'])


@_diagnostics.trace
def interrupt_running_jobs(store, job_ids, *, ui=None):
    """Interrupt the selected IDs using the existing per-job stop contract."""
    ids = (list(dict.fromkeys(str(jid) for jid in job_ids if str(jid)))
           if isinstance(job_ids, (list, tuple, set)) else [])
    if not ids:
        if _diagnostics.detailed: _diagnostics.step('queue_control.interrupt_running_jobs:M74:then')
        raise ValueError((ui or load_ui()).text('scenarios.diagnostics.queue.selection_required'))
    result = dict(interrupted=[], unavailable=[])
    for jid in ids:
        if _diagnostics.detailed: _diagnostics.step('queue_control.interrupt_running_jobs:M77:loop', jid=jid)
        job = interrupt_running_job(store, jid, ui=ui)
        result['interrupted' if job else 'unavailable'].append(jid)
    return result
