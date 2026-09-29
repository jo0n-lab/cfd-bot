"""Shared queue controls used by Telegram, native GUI, web and CLI."""

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
