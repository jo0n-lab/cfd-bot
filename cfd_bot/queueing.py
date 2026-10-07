"""Named FIFO queues, derived CPU profiles, and dynamic borrowing rules."""
from . import diagnostics as _diagnostics
import re

from .config import cpu_set


QUEUE_ID = re.compile(r'[A-Za-z0-9._-]{1,48}')


@_diagnostics.trace
def queue_profile(case):
    """Return a queue id and any legacy CPU profile stored in a case."""
    profile = case.get('execution_queue') or {}
    if profile:
        if _diagnostics.enabled: _diagnostics.step('queueing.queue_profile:L13:then')
        value = profile.get('cpu_set')
        return {'id': profile['id'], 'cpu_set': value,
                'cpus': cpu_set(value) if value else set()}
    return None


@_diagnostics.trace
def job_queue_id(job):
    """Read new job metadata while retaining old queue_lane rows."""
    return job.get('queue_id') or f"legacy-{job.get('queue_lane', 1)}"


@_diagnostics.trace
def registered_profiles(jobs, cases=()):
    """Collect assigned job profiles plus legacy profiles embedded in tickets."""
    profiles = {}
    @_diagnostics.trace
    def add(qid, cpus):
        previous = profiles.get(qid)
        if previous is not None and previous != cpus:
            if _diagnostics.enabled: _diagnostics.step('queueing.registered_profiles.add:L30:then')
            raise ValueError(f'{qid}: inconsistent queue CPU quota')
        profiles[qid] = cpus

    for case in cases:
        if _diagnostics.enabled: _diagnostics.step('queueing.registered_profiles:L34:loop', case=case)
        profile = queue_profile(case)
        if profile and profile['cpus']:
            if _diagnostics.enabled: _diagnostics.step('queueing.registered_profiles:L36:then')
            add(profile['id'], profile['cpus'])
    for job in jobs:
        if _diagnostics.enabled: _diagnostics.step('queueing.registered_profiles:L38:loop', job=job)
        qid = job_queue_id(job)
        value = job.get('queue_cpu_set')
        if value:
            if _diagnostics.enabled: _diagnostics.step('queueing.registered_profiles:L41:then')
            add(qid, cpu_set(value))
    items = list(profiles.items())
    for index, (qid, cpus) in enumerate(items):
        if _diagnostics.enabled: _diagnostics.step('queueing.registered_profiles:L44:loop', index=index, qid=qid, cpus=cpus)
        for other, other_cpus in items[index + 1:]:
            if _diagnostics.enabled: _diagnostics.step('queueing.registered_profiles:L45:loop', other=other, other_cpus=other_cpus)
            if cpus & other_cpus:
                if _diagnostics.enabled: _diagnostics.step('queueing.registered_profiles:L46:then')
                raise ValueError(f'{qid}/{other}: overlapping queue CPU quota')
    return profiles


@_diagnostics.trace
def queue_heads(jobs):
    """Return the oldest queued job for every unbounded queue id."""
    heads = {}
    for job in sorted(jobs, key=lambda item: item['created']):
        if _diagnostics.enabled: _diagnostics.step('queueing.queue_heads:L54:loop', job=job)
        heads.setdefault(job_queue_id(job), job)
    return list(heads.values())


@_diagnostics.trace
def borrowing_plan(job, profiles, pool):
    """Plan CPUs for a dynamic oversized head.

    New dynamic queues own no fixed profile. Globally unreserved CPUs are the
    first input, and assigned fixed queues are donors smallest-first. A legacy
    dynamic job may also contribute its own stored profile.
    """
    qid = job_queue_id(job)
    own = set(profiles.get(qid, ()))
    pool = set(pool)
    # A legacy stored profile is an authorization boundary. Never let it
    # expand the scheduler beyond its managed CPU pool.
    if own and not own <= pool:
        if _diagnostics.enabled: _diagnostics.step('queueing.borrowing_plan:L71:then')
        return None
    required = int(job['case']['cores']) + int(bool(job['case'].get('monitoring', {}).get('allocate_cpu')))
    if own and required <= len(own):
        if _diagnostics.enabled: _diagnostics.step('queueing.borrowing_plan:L74:then')
        return {'allowed': own, 'donors': [], 'required': required, 'oversized': False}
    if not job.get('dynamic_cores'):
        if _diagnostics.enabled: _diagnostics.step('queueing.borrowing_plan:L76:then')
        return None
    reserved = set().union(*profiles.values()) if profiles else set()
    allowed = (pool - reserved) | own
    donors = []
    for donor, cpus in sorted(profiles.items(), key=lambda item: (len(item[1]), item[0])):
        if _diagnostics.enabled: _diagnostics.step('queueing.borrowing_plan:L81:loop', donor=donor, cpus=cpus)
        if donor == qid:
            if _diagnostics.enabled: _diagnostics.step('queueing.borrowing_plan:L82:then')
            continue
        if len(allowed) >= required:
            if _diagnostics.enabled: _diagnostics.step('queueing.borrowing_plan:L84:then')
            break
        donors.append(donor)
        allowed |= cpus
    if len(allowed) < required:
        if _diagnostics.enabled: _diagnostics.step('queueing.borrowing_plan:L88:then')
        return None
    return {'allowed': allowed, 'donors': donors, 'required': required,
            'oversized': bool(donors)}


@_diagnostics.trace
def queue_profile_conflict(candidate, others):
    """Return a stable conflict description for top-level ticket profiles."""
    profile = candidate.get('execution_queue')
    if not profile:
        if _diagnostics.enabled: _diagnostics.step('queueing.queue_profile_conflict:L97:then')
        return None
    current_id = profile['id']
    current_cpus = cpu_set(profile['cpu_set']) if profile.get('cpu_set') else set()
    for other in others:
        if _diagnostics.enabled: _diagnostics.step('queueing.queue_profile_conflict:L101:loop', other=other)
        if other.get('role', 'alone') == 'child' or not other.get('execution_queue'):
            if _diagnostics.enabled: _diagnostics.step('queueing.queue_profile_conflict:L102:then')
            continue
        existing = other['execution_queue']
        existing_cpus = cpu_set(existing['cpu_set']) if existing.get('cpu_set') else set()
        if existing['id'] == current_id:
            if _diagnostics.enabled: _diagnostics.step('queueing.queue_profile_conflict:L106:then')
            if existing_cpus and current_cpus and existing_cpus != current_cpus:
                if _diagnostics.enabled: _diagnostics.step('queueing.queue_profile_conflict:L107:then')
                return ('same_id', existing['id'], existing['cpu_set'])
            if candidate.get('task_type') == 'macro' or other.get('task_type') == 'macro':
                if _diagnostics.enabled: _diagnostics.step('queueing.queue_profile_conflict:L109:then')
                return ('macro_shared', existing['id'], other.get('name', existing['id']))
        elif current_cpus and existing_cpus and current_cpus & existing_cpus:
            if _diagnostics.enabled: _diagnostics.step('queueing.queue_profile_conflict:L111:then')
            overlap = ','.join(map(str, sorted(current_cpus & existing_cpus)))
            return ('overlap', existing['id'], overlap)
    return None
