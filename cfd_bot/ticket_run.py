"""Shared execution actions for saved macro and single tickets."""
from pathlib import Path
import subprocess
import time
import uuid

from .config import load_case, read_json
from .cpu_allocation import capacity_status
from .execution import execution_case
from .processes import snapshot
from .storage import LIVE
from .tickets import atomic_json, ticket_lock
from .ui import load_ui


class TicketRunner:
    def __init__(self, service, config, store):
        self.service, self.config, self.store = service, config, store
        self.ui = load_ui(config.get('_ui_dir'))

    def _snapshot(self):
        try:
            current = dict(snapshot(self.config['ofps_command']), at=time.time())
            self.store.put('snapshot', current)
            return current
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            raise ValueError(self.ui.text('scenarios.runtime.ticket.state_failed', error=exc)) from None

    def _members(self, name, *, fresh=True):
        from .catalog import folder_index
        index = None if fresh else folder_index(self.service.folder)
        read = load_case if fresh else index.document
        path = self.service.path(name)
        ticket = read(path)
        if ticket['task_type'] != 'macro':
            return ticket, [ticket]
        members = []
        for row in ticket['cases']:
            child = read(self.service.path(row['ticket']))
            if (child['role'] != 'child' or Path(child['_root']) != Path(row['case_dir'])
                    or (Path(child['_config']).parent / child['macro_ticket']).resolve() != path):
                raise ValueError(self.ui.text('scenarios.runtime.ticket.macro_members_invalid'))
            members.append(child)
        if not members:
            raise ValueError(self.ui.text('scenarios.runtime.ticket.macro_empty'))
        return ticket, members

    def _capacity(self, members, observed, active):
        resources = [(member if member.get('resource_source') in ('macro', 'ticket')
                      else execution_case(member)) for member in members]
        statuses = [capacity_status(member, observed.get('cases', {}), active, self.config)
                    for member in resources]
        queue = members[0].get('execution_queue') if members else None
        dynamic = bool(queue and any(member.get('dynamic_cores') for member in members))
        largest = max(item['required_cores'] for item in statuses)
        if dynamic:
            # A dynamic macro admits one ordered child at a time.  The first
            # child is the only immediate capacity decision; later children
            # are admitted again when they become the batch head.
            status = dict(statuses[0])
            status['can_run'] = status['can_run'] and largest <= status['capacity']
        else:
            status = max(statuses, key=lambda item: item['required_cores'])
            status = dict(status, can_run=all(item['can_run'] for item in statuses))
        if dynamic and largest > status['capacity']:
            status['availability_message'] = self.ui.text(
                'scenarios.runtime.ticket.dynamic_impossible', required=largest,
                capacity=status['capacity'])
        elif status['free_cores'] is None:
            status['availability_message'] = status['capacity_reason']
        elif dynamic:
            key = ('scenarios.runtime.ticket.dynamic_head_available' if status['can_run']
                   else 'scenarios.runtime.ticket.dynamic_head_insufficient')
            status['availability_message'] = self.ui.text(
                key, free=status['free_cores'], used=status['used_cores'],
                capacity=status['capacity'], required=status['required_cores'],
                largest=largest)
        else:
            key = ('scenarios.runtime.ticket.capacity_available' if status['can_run']
                   else 'scenarios.runtime.ticket.capacity_insufficient')
            status['availability_message'] = self.ui.text(
                key, free=status['free_cores'], used=status['used_cores'],
                capacity=status['capacity'], required=status['required_cores'])
        if queue:
            status.update(queue_id=queue['id'], queue_quota=None if dynamic else largest,
                          queue_required=status['required_cores'], queue_dynamic=dynamic,
                          queue_max_required=largest,
                          queue_possible=largest <= status['capacity'])
        return status

    def _state(self, ticket, members, observed, *, jobs_by_root=None, external=None, active=None):
        roots = {member['_root'] for member in members}
        jobs = ([job for job in self.store.jobs_for_roots(roots) if job['status'] in (*LIVE, 'queued')]
                if jobs_by_root is None else [j for root in roots for j in jobs_by_root.get(root, [])])
        active = self.store.jobs(LIVE) if active is None else active
        if external is None:
            external = self.store.get_many('observed:' + root for root in roots)
        running = (roots & set(observed.get('cases', {}))) or any(job['status'] in LIVE for job in jobs)
        # Keep a briefly disappearing external process blocked until the monitor
        # has completed its normal missing-poll checks.
        running = running or any((external.get('observed:' + root) or {}).get('status') in LIVE
                                 for root in roots)
        if running:
            return dict(state='running', enabled=False, run_enabled=False, queue_enabled=False,
                        label=self.ui.text('scenarios.runtime.ticket_state.running'),
                        queue_label=self.ui.text('scenarios.runtime.ticket_state.queue_action'))
        if jobs or ticket.get('queue', {}).get('submit') or any(member.get('queue', {}).get('submit') for member in members):
            return dict(state='queued', enabled=False, run_enabled=False, queue_enabled=False,
                        label=self.ui.text('scenarios.runtime.ticket_state.queued'),
                        queue_label=self.ui.text('scenarios.runtime.ticket_state.queue_action'))
        if not observed and any(member.get('queue', {}).get('state') == 'running' for member in members):
            return dict(state='running', enabled=False, run_enabled=False, queue_enabled=False,
                        label=self.ui.text('scenarios.runtime.ticket_state.running'),
                        queue_label=self.ui.text('scenarios.runtime.ticket_state.queue_action'))
        capacity = self._capacity(members, observed, active)
        enabled = capacity['can_run']
        queue_enabled = capacity.get('queue_possible', True)
        return dict(state='idle', enabled=enabled, run_enabled=enabled, queue_enabled=queue_enabled,
                    label=self.ui.text('scenarios.runtime.ticket_state.idle' if enabled
                                       else 'scenarios.runtime.ticket_state.insufficient'),
                    queue_label=self.ui.text('scenarios.runtime.ticket_state.queue_action'), **capacity)

    def state(self, name, *, fresh=False):
        observed = self._snapshot() if fresh else self.store.get('snapshot', {})
        with ticket_lock(self.service.folder):
            ticket, members = self._members(name, fresh=False)
            return self._state(ticket, members, observed)

    def states(self, tickets):
        """One DB read per data set for a page showing many ticket buttons."""
        by_path = {c['_config']: c for c in tickets}
        jobs = {}
        all_jobs = self.store.jobs((*LIVE, 'queued'))
        active = [job for job in all_jobs if job['status'] in LIVE]
        for job in all_jobs:
            jobs.setdefault(job['case_root'], []).append(job)
        external = self.store.get_many('observed:' + c['_root'] for c in tickets)
        observed = self.store.get('snapshot', {})
        result = {}
        for ticket in tickets:
            name = Path(ticket['_config']).name
            try:
                if ticket['task_type'] == 'macro':
                    members = []
                    for row in ticket['cases']:
                        child = by_path.get(str(self.service.path(row['ticket'])))
                        if (child is None or child['role'] != 'child' or child['_root'] != row['case_dir']
                                or str((Path(child['_config']).parent / child['macro_ticket']).resolve()) != ticket['_config']):
                            raise ValueError(self.ui.text('scenarios.runtime.ticket.macro_members_invalid'))
                        members.append(child)
                    if not members:
                        raise ValueError(self.ui.text('scenarios.runtime.ticket.macro_empty'))
                else:
                    members = [ticket]
                result[name] = self._state(ticket, members, observed, jobs_by_root=jobs,
                                           external=external, active=active)
            except (ValueError, OSError) as exc:
                result[name] = dict(state='invalid', enabled=False, run_enabled=False,
                                    queue_enabled=False, error=str(exc))
        return result

    def request(self, name, *, expected_revision=None, request_id=None, mode='run', lane=1):
        if mode not in ('run', 'queue'):
            raise ValueError(self.ui.text('scenarios.runtime.ticket.request_mode'))
        # Never trust a previously displayed button or just the ticket's state.
        observed = self._snapshot()
        with ticket_lock(self.service.folder):
            ticket, members = self._members(name)
            if expected_revision and self.service.revision(name) != expected_revision:
                raise ValueError(self.ui.text('scenarios.runtime.ticket.changed'))
            status = self._state(ticket, members, observed)
            if status['state'] == 'running':
                raise ValueError(self.ui.text('scenarios.runtime.ticket.already_running'))
            if status['state'] == 'queued':
                return dict(already_queued=True, request_id=ticket.get('queue', {}).get('request_id'))
            if mode == 'run' and not status['run_enabled']:
                raise ValueError(status['availability_message'])
            if mode == 'queue' and not status['queue_enabled']:
                raise ValueError(status['availability_message'])
            profile = ticket.get('execution_queue')
            if request_id and ticket.get('queue', {}).get('request_id') == request_id:
                return dict(already_queued=True, request_id=request_id)
            for member in members:
                if not member.get('command') and not (Path(member['_root']) / 'Allrun').is_file():
                    raise ValueError(self.ui.text('scenarios.runtime.ticket.missing_command', name=member['name']))
            request_id = request_id or uuid.uuid4().hex
            path = self.service.path(name)
            data = read_json(path)
            staged = {}
            if ticket['task_type'] == 'macro':
                for member in members:
                    child_path = Path(member['_config'])
                    child = read_json(child_path)
                    child['queue'] = dict(state='waiting', request_id=request_id)
                    staged[child_path] = child
                for row in data['cases']:
                    for key in ('result', 'reason', 'job_id'):
                        row.pop(key, None)
                    row['state'] = 'waiting'
            queue_data = dict(state='waiting', submit=True, request_id=request_id,
                              mode=mode, updated_at=time.time())
            if profile:
                queue_data['id'] = profile['id']
            else:
                queue_data['lane'] = lane
            data['queue'] = queue_data
            staged[path] = data
            backups = {target: target.read_bytes() for target in staged}
            try:
                for target, document in staged.items():
                    atomic_json(target, document)
            except OSError:
                for target, before in backups.items():
                    target.write_bytes(before)
                raise
            return dict(already_queued=False, request_id=request_id, count=len(members),
                        mode=mode, queue_id=profile['id'] if profile else f'legacy-{lane}')
