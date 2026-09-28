"""Shared execution actions for saved macro and single tickets."""
from pathlib import Path
import subprocess
import time
import uuid

from .config import load_case, read_json
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

    def _members(self, name):
        path = self.service.path(name)
        ticket = load_case(path)
        if ticket['task_type'] != 'macro':
            return ticket, [ticket]
        members = []
        for row in ticket['cases']:
            child = load_case(self.service.path(row['ticket']))
            if (child['role'] != 'child' or Path(child['_root']) != Path(row['case_dir'])
                    or (Path(child['_config']).parent / child['macro_ticket']).resolve() != path):
                raise ValueError(self.ui.text('scenarios.runtime.ticket.macro_members_invalid'))
            members.append(child)
        if not members:
            raise ValueError(self.ui.text('scenarios.runtime.ticket.macro_empty'))
        return ticket, members

    def _state(self, ticket, members, observed):
        roots = {member['_root'] for member in members}
        jobs = [job for job in self.store.jobs((*LIVE, 'queued')) if job['case_root'] in roots]
        running = (roots & set(observed.get('cases', {}))) or any(job['status'] in LIVE for job in jobs)
        # Keep a briefly disappearing external process blocked until the monitor
        # has completed its normal missing-poll checks.
        running = running or any((self.store.get('observed:' + root) or {}).get('status') in LIVE
                                 for root in roots)
        if running:
            return dict(state='running', enabled=False,
                        label=self.ui.text('scenarios.runtime.ticket_state.running'))
        if jobs or ticket.get('queue', {}).get('submit') or any(member.get('queue', {}).get('submit') for member in members):
            return dict(state='queued', enabled=True,
                        label=self.ui.text('scenarios.runtime.ticket_state.queued'))
        if not observed and any(member.get('queue', {}).get('state') == 'running' for member in members):
            return dict(state='running', enabled=False,
                        label=self.ui.text('scenarios.runtime.ticket_state.running'))
        return dict(state='idle', enabled=True, label=self.ui.text('scenarios.runtime.ticket_state.idle'))

    def state(self, name, *, fresh=False):
        observed = self._snapshot() if fresh else self.store.get('snapshot', {})
        with ticket_lock(self.service.folder):
            ticket, members = self._members(name)
            return self._state(ticket, members, observed)

    def request(self, name, *, expected_revision=None, request_id=None):
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
            data['queue'] = dict(state='waiting', submit=True, request_id=request_id, updated_at=time.time())
            staged[path] = data
            backups = {target: target.read_bytes() for target in staged}
            try:
                for target, document in staged.items():
                    atomic_json(target, document)
            except OSError:
                for target, before in backups.items():
                    target.write_bytes(before)
                raise
            return dict(already_queued=False, request_id=request_id, count=len(members))
