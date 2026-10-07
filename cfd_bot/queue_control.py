"""Shared queue controls used by Telegram, native GUI, web and CLI."""

import os
import signal
import time

from .processes import identity
from .ui import load_ui


def cancel_queued_jobs(store, job_ids, *, ui=None):
    """Cancel every selected job that is still queued in one DB transaction."""
    if not isinstance(job_ids, (list, tuple, set)):
        job_ids = []
    ids = list(dict.fromkeys(str(job_id) for job_id in job_ids if str(job_id)))
    if not ids:
        catalog = ui or load_ui()
        raise ValueError(catalog.text('scenarios.diagnostics.queue.selection_required'))
    return store.cancel_queued(ids)


def interrupt_process_groups(job, signum=signal.SIGTERM):
    """Signal only session leaders whose current /proc identity still matches."""
    signalled = []
    seen = set()
    for kind in ('hook', 'monitor', 'solver'):
        pid = job.get(kind + '_pid')
        recorded = job.get(kind + '_identity')
        if not isinstance(pid, int) or pid <= 1 or pid in seen or not recorded:
            continue
        seen.add(pid)
        if identity(pid) != recorded:
            continue
        try:
            if os.getpgid(pid) != pid:
                continue
            os.killpg(pid, signum)
            signalled.append(pid)
        except ProcessLookupError:
            continue
    return signalled


def interrupt_running_job(store, jid, *, ui=None):
    """Request interruption without releasing the job's CPU reservation early."""
    catalog = ui or load_ui()
    job = store.request_interruption(
        str(jid), catalog.text('scenarios.jobs.user_interrupt_requested'))
    if job is None:
        return None
    interrupt_process_groups(job)
    worker_alive = (job.get('worker_identity') and
                    identity(job.get('worker_pid')) == job['worker_identity'])
    child_alive = any(
        job.get(kind + '_identity') and
        identity(job.get(kind + '_pid')) == job[kind + '_identity']
        for kind in ('hook', 'monitor', 'solver'))
    if not worker_alive and not child_alive:
        completed = store.update_job(
            job['id'], expected=('stopping',), status='interrupted', finished=time.time(),
            reason=catalog.text('scenarios.jobs.user_interrupted'))
        return completed or store.job(job['id'])
    return store.job(job['id'])
