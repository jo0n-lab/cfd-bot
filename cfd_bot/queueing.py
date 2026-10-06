"""Named queue profiles and dynamic quota borrowing rules."""
import re

from .config import cpu_set


QUEUE_ID = re.compile(r'[A-Za-z0-9._-]{1,48}')


def queue_profile(case):
    """Return the normalized named queue profile stored in a ticket/job case."""
    profile = case.get('execution_queue') or {}
    if profile:
        return {'id': profile['id'], 'cpu_set': profile['cpu_set'],
                'cpus': cpu_set(profile['cpu_set'])}
    return None


def job_queue_id(job):
    """Read new job metadata while retaining old queue_lane rows."""
    return job.get('queue_id') or f"legacy-{job.get('queue_lane', 1)}"


def registered_profiles(jobs, cases=()):
    """Collect consistent named profiles from tickets and live job snapshots."""
    profiles = {}
    def add(qid, cpus):
        previous = profiles.get(qid)
        if previous is not None and previous != cpus:
            raise ValueError(f'{qid}: inconsistent queue CPU quota')
        profiles[qid] = cpus

    for case in cases:
        profile = queue_profile(case)
        if profile and profile['cpus']:
            add(profile['id'], profile['cpus'])
    for job in jobs:
        qid = job_queue_id(job)
        value = job.get('queue_cpu_set')
        if value:
            add(qid, cpu_set(value))
    items = list(profiles.items())
    for index, (qid, cpus) in enumerate(items):
        for other, other_cpus in items[index + 1:]:
            if cpus & other_cpus:
                raise ValueError(f'{qid}/{other}: overlapping queue CPU quota')
    return profiles


def queue_heads(jobs):
    """Return the oldest queued job for every unbounded queue id."""
    heads = {}
    for job in sorted(jobs, key=lambda item: item['created']):
        heads.setdefault(job_queue_id(job), job)
    return list(heads.values())


def borrowing_plan(job, profiles, pool):
    """Plan CPUs for a dynamic oversized head.

    The queue's own quota and globally unreserved CPUs are free inputs. Donor
    queues are added smallest-first until the requested count fits.
    """
    qid = job_queue_id(job)
    own = set(profiles.get(qid, ()))
    pool = set(pool)
    # A queue quota is an authorization boundary.  Never let a stale or
    # hand-written ticket expand the scheduler beyond its managed CPU pool.
    if not own or not own <= pool:
        return None
    required = int(job['case']['cores']) + int(bool(job['case'].get('monitoring', {}).get('allocate_cpu')))
    if required <= len(own):
        return {'allowed': own, 'donors': [], 'required': required, 'oversized': False}
    if not job.get('dynamic_cores'):
        return None
    reserved = set().union(*profiles.values()) if profiles else set()
    allowed = (pool - reserved) | own
    donors = []
    for donor, cpus in sorted(profiles.items(), key=lambda item: (len(item[1]), item[0])):
        if donor == qid:
            continue
        if len(allowed) >= required:
            break
        donors.append(donor)
        allowed |= cpus
    if len(allowed) < required:
        return None
    return {'allowed': allowed, 'donors': donors, 'required': required, 'oversized': True}


def queue_profile_conflict(candidate, others):
    """Return a stable conflict description for top-level ticket profiles."""
    profile = candidate.get('execution_queue')
    if not profile:
        return None
    current_id = profile['id']
    current_cpus = cpu_set(profile['cpu_set'])
    for other in others:
        if other.get('role', 'alone') == 'child' or not other.get('execution_queue'):
            continue
        existing = other['execution_queue']
        existing_cpus = cpu_set(existing['cpu_set'])
        if existing['id'] == current_id:
            if existing_cpus != current_cpus:
                return ('same_id', existing['id'], existing['cpu_set'])
            if candidate.get('task_type') == 'macro' or other.get('task_type') == 'macro':
                return ('macro_shared', existing['id'], other.get('name', existing['id']))
        elif current_cpus & existing_cpus:
            overlap = ','.join(map(str, sorted(current_cpus & existing_cpus)))
            return ('overlap', existing['id'], overlap)
    return None
