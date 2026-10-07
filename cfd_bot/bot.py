from . import diagnostics as _diagnostics
import hashlib
import logging
import os
import threading
import time
from pathlib import Path

from .artifacts import MAX_DOCUMENT, export_files, residual_files
from .config import cases_for, tickets_for
from .catalog import ticket_index
from .monitor import Monitor
from .processes import DaemonLock, snapshot as process_snapshot
from .queue_control import cancel_queued_jobs
from .queueing import job_queue_id
from .run_views import case_id_for_root, running_macro_views, tracking_registry
from .report import compact_status, macro_queue_text, queue_text, render_run
from .telegram import Telegram, TelegramError, chunks
from .texts import load_text
from .ui import load_ui

LOG = logging.getLogger(__name__)
QUEUE_PAGE = 8


@_diagnostics.trace
def button(text, data):
    return {'text': text, 'callback_data': data}


@_diagnostics.trace
def keyboard(rows):
    return {'inline_keyboard': rows}


@_diagnostics.trace
def case_id(case):
    return case_id_for_root(case['_root'])


@_diagnostics.trace
def elapsed_seconds(value):
    """Parse ps/ofps elapsed values such as MM:SS, HH:MM:SS or DD-HH:MM:SS."""
    try:
        day_text, clock = value.split('-', 1) if '-' in value else ('0', value)
        parts = [int(part) for part in clock.split(':')]
        if not 1 <= len(parts) <= 3:
            if _diagnostics.enabled: _diagnostics.step('bot.elapsed_seconds:L42:then')
            return 0
        seconds = sum(part * 60 ** index for index, part in enumerate(reversed(parts)))
        return int(day_text) * 86400 + seconds
    except (AttributeError, TypeError, ValueError):
        if _diagnostics.enabled: _diagnostics.step('bot.elapsed_seconds:L46:except')
        return 0


class Bot:
    @_diagnostics.trace
    def __init__(self, config, store, api):
        self.config, self.store, self.api = config, store, api
        self.ui = load_ui(config.get('_ui_dir'))
        self.ticket_ui = None

    @_diagnostics.trace
    def cases(self):
        return {case_id(c): c for c in cases_for(self.config)}

    @_diagnostics.trace
    def templates(self):
        return load_text(self.config.get('_text_file'), self.ui)

    @_diagnostics.trace
    def _remember(self, chat, result):
        if result is None:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot._remember:L63:then')
            return
        results = result if isinstance(result, list) else [result]
        for message in results:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot._remember:L66:loop', message=message)
            if isinstance(message, dict) and type(message.get('message_id')) is int:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot._remember:L67:then')
                self.store.remember_message(chat, message['message_id'], message.get('date'))

    @_diagnostics.trace
    def send(self, chat, text, markup=None):
        result = self.api.send(chat, text, markup)
        self._remember(chat, result)
        return result

    @_diagnostics.trace
    def file(self, chat, item):
        result = self.api.file(chat, item)
        self._remember(chat, result)
        return result

    @_diagnostics.trace
    def active_runs(self, snapshot=None, cases=None):
        """Translate the current ofps snapshot directly; watcher state is irrelevant."""
        snapshot = snapshot if snapshot is not None else (self.store.get('snapshot') or {})
        runs = []
        cases = ticket_index(self.config).cases(snapshot.get('cases', {})) if cases is None else cases
        registered = {case['_root']: case for case in cases}
        observed_at = snapshot.get('at', time.time())
        for root, record in snapshot.get('cases', {}).items():
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.active_runs:L87:loop', root=root, record=record)
            case = registered.get(root)
            is_registered = case is not None
            if case is None:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.active_runs:L90:then')
                case = {'name': Path(root).name + ' · ' + self.ui.text('strings.common.unregistered'),
                        '_root': root, '_ui_dir': self.config['_ui_dir'],
                        'watcher': {}, 'command': []}
            members = record.get('supervisors', []) + record.get('processes', [])
            elapsed = max((elapsed_seconds(member.get('elapsed')) for member in members),
                          default=0)
            runs.append(dict(
                id='ofps-' + hashlib.sha256(root.encode()).hexdigest()[:12],
                case=case, case_root=root, created=observed_at,
                started=observed_at - elapsed, status='running', telemetry={},
                external=True, registered=is_registered,
                owner=record.get('owner', self.ui.text('strings.common.unavailable')),
                actual_cores=record.get('actual_cores'),
                actual_cpu_list=record.get('actual_cpu_list'),
            ))
        return runs

    @_diagnostics.trace
    def fresh_runs(self):
        """Run ofps for this /stat request so Telegram sees the same live data."""
        try:
            snap = process_snapshot(self.config['ofps_command'])
            snap['at'] = time.time()
            cases = ticket_index(self.config).cases(snap['cases'])
            return snap, self.active_runs(snap, cases), None
        except (OSError, RuntimeError) as exc:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.fresh_runs:L115:except')
            snap = {'at': time.time(), 'cases': {}}
            return snap, [], {'message': self.ui.text('strings.common.technical_error',
                                                     type=type(exc).__name__, error=exc)}

    @staticmethod
    @_diagnostics.trace
    def queue_selection_key(chat, user=None):
        return f'queue-selection:{chat}:{chat if user is None else user}'

    @_diagnostics.trace
    def queue_selection(self, chat, user=None):
        jobs = self.store.jobs(('queued',))
        available = {job['id'] for job in jobs}
        selected = [jid for jid in self.store.get(self.queue_selection_key(chat, user), []) if jid in available]
        self.store.put(self.queue_selection_key(chat, user), selected)
        return jobs, selected

    @_diagnostics.trace
    def show_queue_selection(self, chat, page=0, notice='', user=None):
        jobs, selected = self.queue_selection(chat, user)
        page = max(0, min(int(page), max(0, (len(jobs) - 1) // QUEUE_PAGE)))
        visible = jobs[page * QUEUE_PAGE:(page + 1) * QUEUE_PAGE]
        rows = [[button(self.ui.text('strings.common.checked' if job['id'] in selected
                                     else 'strings.common.unchecked') +
                               self.ui.text('menus.queue.selection_item',
                                            queue=job_queue_id(job),
                                            case_name=job['case']['name']),
                        'qtoggle:' + job['id'])] for job in visible]
        navigation = []
        if page:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.show_queue_selection:L142:then')
            navigation.append(button(self.ui.text('menus.queue.previous'), f'qpage:{page - 1}'))
        if (page + 1) * QUEUE_PAGE < len(jobs):
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.show_queue_selection:L144:then')
            navigation.append(button(self.ui.text('menus.queue.next'), f'qpage:{page + 1}'))
        if navigation:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.show_queue_selection:L146:then')
            rows.append(navigation)
        rows += [[button(self.ui.text('menus.queue.select_all'), 'qall'),
                  button(self.ui.text('menus.queue.clear_all'), 'qnone')],
                 [button(self.ui.text('menus.queue.cancel_many', count=len(selected)), 'qcancel')],
                 [button(self.ui.text('menus.queue.back'), 'queue')]]
        text = self.ui.text('menus.queue.selection_title', selected=len(selected), total=len(jobs))
        self.send(chat, (notice + '\n\n' if notice else '') + text, keyboard(rows))

    @_diagnostics.trace
    def status(self, snap=None, runs=None, error=None):
        snap = snap if snap is not None else self.store.get('snapshot')
        if error is None:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.status:L157:then')
            error = self.store.get('monitor_error')
        age = self.ui.text('scenarios.status.age', seconds=max(0, int(time.time() - snap['at']))) if snap else ''
        lines = [self.ui.text('scenarios.status.title', age=age)]
        if error:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.status:L161:then')
            lines.append(self.ui.text('scenarios.status.collection_error', message=error['message']))
        runs = self.active_runs(snap) if runs is None else runs
        for run in runs:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.status:L164:loop', run=run)
            if run.get('registered', True):
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.status:L165:then')
                run['runtime_history'] = self.store.runtime_history(
                    run['case'], run.get('actual_cores'))
            lines.append(compact_status(run, self.ui))
        if not runs:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.status:L169:then')
            lines.append(self.ui.text('scenarios.status.empty'))
        return '\n'.join(lines)

    @_diagnostics.trace
    def status_keyboard(self, runs=None):
        rows = []
        for run in self.active_runs() if runs is None else runs:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.status_keyboard:L175:loop', run=run)
            if run.get('registered', True):
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.status_keyboard:L176:then')
                case = run['case']
                rows.append([button(case['name'], 'detail:' + case_id(case))])
        return keyboard(rows) if rows else None

    @_diagnostics.trace
    def acknowledge_callback(self, callback_id, text=None):
        payload = {'callback_query_id': callback_id}
        if text:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.acknowledge_callback:L183:then')
            payload['text'] = text
        try:
            self.api.call('answerCallbackQuery', payload)
        except TelegramError as exc:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.acknowledge_callback:L187:except')
            LOG.warning('Telegram callback acknowledgement failed: %s', exc)

    @_diagnostics.trace
    def start_callback_ack(self, callback_id, text=None):
        threading.Thread(target=_diagnostics.inherit_context(self.acknowledge_callback), args=(callback_id, text),
                         daemon=True).start()

    @_diagnostics.trace
    def show_cases(self, chat, page=0):
        cases = list(self.cases().items())
        page = max(0, min(page, max(0, (len(cases) - 1) // 8)))
        rows = [[button(c['name'], 'case:' + cid)] for cid, c in cases[page * 8:page * 8 + 8]]
        nav = []
        if page:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.show_cases:L199:then')
            nav.append(button(self.ui.text('menus.cases.previous'), f'cases:{page-1}'))
        if (page + 1) * 8 < len(cases):
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.show_cases:L201:then')
            nav.append(button(self.ui.text('menus.cases.next'), f'cases:{page+1}'))
        if nav:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.show_cases:L203:then')
            rows.append(nav)
        self.send(chat, self.ui.text('menus.cases.select' if cases else 'menus.cases.empty'), keyboard(rows))

    @_diagnostics.trace
    def case_menu(self, chat, cid):
        case = self.cases().get(cid)
        if case is None:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.case_menu:L209:then')
            raise ValueError(self.ui.text('menus.cases.removed'))
        rows = [[button(self.ui.text('menus.cases.detail'), 'detail:' + cid)]]
        if case.get('residual_pattern'):
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.case_menu:L212:then')
            rows[0].append(button(self.ui.text('menus.cases.residual'), 'residual:' + cid))
        rows += [[button(e['name'], 'export:' + cid + ':' + e['name'])] for e in case['exports']]
        if case.get('command') or (Path(case['_root']) / 'Allrun').is_file():
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.case_menu:L215:then')
            state = self.case_runner(case).state(Path(case['_config']).name)
            rows.append([button(state['label'], ('prepare:' if state.get('queue_enabled') else 'case:') + cid)])
        ticket_key = hashlib.sha256(case['_config'].encode()).hexdigest()[:16]
        rows.append([button(self.ui.text('menus.cases.edit_ticket'), 'ticketopen:' + ticket_key)])
        self.send(chat, self.ui.text('scenarios.data.case_header', case_name=case['name'],
                                     case_root=case['_root']), keyboard(rows))

    @_diagnostics.trace
    def case_runner(self, case):
        from .editor import TicketService
        from .ticket_run import TicketRunner
        return TicketRunner(TicketService(Path(case['_config']).parent), self.config, self.store)

    @_diagnostics.trace
    def detail_keyboard(self, case, cid):
        rows = [[button(self.ui.text('menus.cases.residual'), 'residual:' + cid)]] if case.get('residual_pattern') else []
        rows += [[button(e['name'], 'export:' + cid + ':' + e['name'])] for e in case['exports']]
        return keyboard(rows) if rows else None

    @_diagnostics.trace
    def latest_run(self, case):
        runs = [j for j in self.store.jobs() if j['case_root'] == case['_root']]
        external = self.store.get('observed:' + case['_root'])
        if external:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.latest_run:L236:then')
            runs.append(external)
        if not runs:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.latest_run:L238:then')
            return None
        run = max(runs, key=lambda x: x['created'])
        run['runtime_history'] = self.store.runtime_history(case, run.get('actual_cores'))
        return run

    @_diagnostics.trace
    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if callback else update.get('message', {})
        sender = (callback or message).get('from', {}).get('id')
        chat = message.get('chat', {}).get('id')
        # Both sender and destination must be explicitly allowed, including in groups.
        if sender not in self.config['telegram']['allowed_user_ids'] or chat not in self.config['telegram']['chat_ids']:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.handle:L250:then')
            if callback:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.handle:L251:then')
                self.start_callback_ack(callback['id'], self.ui.text('menus.home.unauthorized'))
            return
        if type(message.get('message_id')) is int:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.handle:L254:then')
            self.store.remember_message(chat, message['message_id'], message.get('date'))
        if callback:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.handle:L256:then')
            self.start_callback_ack(callback['id'])
        if self.ticket_ui is None:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.handle:L258:then')
            from .ticket_chat import TicketChat
            self.ticket_ui = TicketChat(self)
        if self.ticket_ui.handle(update):
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.handle:L261:then')
            return
        if callback:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.handle:L263:then')
            action = callback.get('data', '')
        else:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.handle:L263:else')
            command = message.get('text', '').split()
            name = command[0].split('@')[0] if command else ''
            action = {'/start': 'home', '/help': 'home', '/stat': 'status',
                      '/clean': 'clean', '/queue': 'queue', '/cases': 'cases:0',
                      '/data': 'cases:0'}.get(name, 'home')
        try:
            self.dispatch(chat, action, str(update['update_id']), sender)
        except (ValueError, OSError) as exc:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.handle:L273:except')
            self.send(chat, self.ui.text('menus.home.request_failed', error=str(exc)))

    @_diagnostics.trace
    def dispatch(self, chat, action, request_key, user=None):
        actor = chat if user is None else user
        if action == 'home':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L278:then')
            spec = self.ui.value('menus.home.keyboard')
            rows = [[button(item['text'], item['action']) for item in row] for row in spec]
            self.send(chat, self.ui.text('menus.home.help'), keyboard(rows))
            return
        if action == 'status':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L283:then')
            snap, runs, error = self.fresh_runs()
            self.send(chat, self.status(snap, runs, error), self.status_keyboard(runs))
            return
        if action == 'clean':
            # Telegram cannot delete messages older than 48 hours. Use the
            # message's Telegram timestamp and a small boundary margin so one
            # expired ID cannot force the whole batch into a slow fallback.
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L287:then')
            messages = self.store.chat_messages(chat, since=time.time() - 48 * 3600 + 60)
            self.api.delete_messages(chat, messages)
            # Expired and otherwise undeletable IDs must not poison every later
            # /clean attempt. A transport/server error raises before this point.
            self.store.clear_messages(chat)
            if self.ticket_ui is not None:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L296:then')
                self.ticket_ui.forget_panels(chat)
            return
        if action in ('queue', 'pause', 'resume'):
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L299:then')
            if action != 'queue':
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L300:then')
                self.store.put('queue_paused', action == 'pause')
            enabled = self.config['scheduler']['enabled'] and not self.store.get('queue_paused', False)
            cases = list(self.cases().values())
            registry = tracking_registry(cases)
            jobs = self.store.jobs()
            macros = running_macro_views(
                [ticket for ticket in tickets_for(self.config) if ticket['task_type'] == 'macro'],
                cases, jobs, self.store)
            rows = [[button(self.ui.text('menus.queue.add'), 'cases:0'),
                     button(self.ui.text('menus.queue.edit_tickets'), 'tickets')]]
            rows.append([button(self.ui.text('menus.queue.resume') if self.store.get('queue_paused', False)
                                else self.ui.text('menus.queue.pause'),
                                'resume' if self.store.get('queue_paused', False) else 'pause')])
            if self.store.jobs(('queued',)):
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L314:then')
                rows.append([button(self.ui.text('menus.queue.multi_select'), 'qselect')])
            rows += [[button(self.ui.text('menus.queue.cancel_case', case_name=j['case']['name'],
                                          queue=job_queue_id(j)),
                             'cancel:' + j['id'])]
                     for j in self.store.jobs(('queued',))[:20]]
            rows += [[button(self.ui.text('menus.queue.result_data', case_name=j['case']['name']),
                             'case:' + registry[j['case_root']]['case_id'])]
                     for j in jobs if j['status'] == 'running' and j['case_root'] in registry]
            recent = [j for j in reversed(jobs) if j['status'] not in
                      ('queued', 'starting', 'running', 'postprocessing')
                      and j['case_root'] in registry][:5]
            rows += [[button(self.ui.text('menus.queue.recent_result', case_name=j['case']['name']),
                             'case:' + registry[j['case_root']]['case_id'])] for j in recent]
            text = queue_text(self.store, enabled, self.ui) + macro_queue_text(macros, self.ui)
            self.send(chat, text, keyboard(rows))
            return
        if action == 'qselect':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L331:then')
            self.store.put(self.queue_selection_key(chat, actor), [])
            self.show_queue_selection(chat, user=actor)
            return
        if action == 'qback':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L335:then')
            self.show_queue_selection(chat, user=actor)
            return
        if action.startswith('qpage:'):
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L338:then')
            self.show_queue_selection(chat, int(action.split(':', 1)[1]), user=actor)
            return
        if action.startswith('qtoggle:'):
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L341:then')
            jid = action.split(':', 1)[1]
            jobs, selected = self.queue_selection(chat, actor)
            if jid not in {job['id'] for job in jobs}:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L344:then')
                self.show_queue_selection(chat, notice=self.ui.text('menus.queue.cancel_unavailable'), user=actor)
                return
            selected.remove(jid) if jid in selected else selected.append(jid)
            self.store.put(self.queue_selection_key(chat, actor), selected)
            self.show_queue_selection(chat, user=actor)
            return
        if action in ('qall', 'qnone'):
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L351:then')
            jobs, _ = self.queue_selection(chat, actor)
            self.store.put(self.queue_selection_key(chat, actor),
                           [job['id'] for job in jobs] if action == 'qall' else [])
            self.show_queue_selection(chat, user=actor)
            return
        if action == 'qcancel':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L357:then')
            jobs, selected = self.queue_selection(chat, actor)
            if not selected:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L359:then')
                raise ValueError(self.ui.text('scenarios.diagnostics.queue.selection_required'))
            names = {job['id']: job['case']['name'] for job in jobs}
            preview = '\n'.join(names[jid] for jid in selected[:30])
            if len(selected) > 30:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L363:then')
                preview += '\n…'
            self.send(chat, self.ui.text('menus.queue.cancel_many_confirm', count=len(selected), names=preview),
                      keyboard([[button(self.ui.text('menus.queue.cancel_many', count=len(selected)),
                                        'qcancelyes'),
                                 button(self.ui.text('strings.common.cancel'), 'qback')]]))
            return
        if action == 'qcancelyes':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L370:then')
            selected = self.store.get(self.queue_selection_key(chat, actor), [])
            result = cancel_queued_jobs(self.store, selected, ui=self.ui)
            self.store.put(self.queue_selection_key(chat, actor), [])
            notice = self.ui.text('menus.queue.cancel_many_result',
                                  cancelled=len(result['cancelled']),
                                  unavailable=len(result['unavailable']))
            self.show_queue_selection(chat, notice=notice, user=actor)
            return
        parts = action.split(':')
        if parts[0] == 'cases':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L380:then')
            self.show_cases(chat, int(parts[1]))
            return
        if parts[0] == 'cancel' and len(parts) == 2:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L383:then')
            result = cancel_queued_jobs(self.store, [parts[1]], ui=self.ui)
            self.send(chat, self.ui.text('menus.queue.cancelled' if result['cancelled']
                                         else 'menus.queue.cancel_unavailable'))
            return
        if len(parts) < 2:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L388:then')
            raise ValueError(self.ui.text('menus.home.unknown_button'))
        cid = parts[1]
        case = self.cases().get(cid)
        if case is None:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L392:then')
            raise ValueError(self.ui.text('menus.cases.unregistered'))
        if parts[0] == 'case':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L394:then')
            self.case_menu(chat, cid)
        elif parts[0] == 'detail':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L396:then')
            run = self.latest_run(case)
            text = (render_run(run, self.templates(), 'detail', self.ui) if run else
                    self.ui.text('scenarios.data.no_observation', case_name=case['name']))
            self.send(chat, text, self.detail_keyboard(case, cid))
        elif parts[0] == 'prepare':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L401:then')
            from .execution import execution_case
            state = self.case_runner(case).state(Path(case['_config']).name, fresh=True)
            if state['state'] == 'running':
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L404:then')
                raise ValueError(self.ui.text('scenarios.launch.already_running'))
            if state['state'] == 'queued':
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L406:then')
                self.send(chat, self.ui.text('scenarios.launch.already_queued'))
                return
            execution = execution_case(case)
            actions = []
            if state['run_enabled']:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L411:then')
                actions.append([button(self.ui.text('scenarios.launch.run'), 'enqueue:' + cid + ':run')])
            if state['queue_enabled']:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L413:then')
                profile = case.get('execution_queue')
                if profile:
                    if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L415:then')
                    actions.append([button(self.ui.text('scenarios.launch.register',
                                                        queue=profile['id']),
                                           f'enqueue:{cid}:queue')])
                else:
                    # Existing pre-#21 tickets retain their persisted lane
                    # semantics until they are edited into a named profile.
                    if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L415:else')
                    actions.extend([
                        button(self.ui.text('scenarios.launch.register', queue=str(lane)),
                               f'enqueue:{cid}:queue:{lane}')
                    ] for lane in range(1, 4))
            self.send(chat, self.ui.text(
                'scenarios.launch.confirm', case_name=case['name'], cores=execution['cores'],
                cpu_set=execution.get('cpu_set', self.ui.text('scenarios.launch.automatic_cpu')),
                command=execution['command'], availability=state.get('availability_message', '')),
                keyboard(actions))
        elif parts[0] == 'enqueue':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L431:then')
            mode = parts[2] if len(parts) > 2 else 'queue'
            lane = int(parts[3]) if len(parts) > 3 else 1
            state = self.case_runner(case).state(Path(case['_config']).name, fresh=True)
            if state['state'] == 'running':
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L435:then')
                raise ValueError(self.ui.text('scenarios.launch.already_running'))
            if state['state'] == 'queued':
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L437:then')
                self.send(chat, self.ui.text('scenarios.launch.already_queued'))
                return
            if mode == 'run' and not state['run_enabled']:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L440:then')
                raise ValueError(state['availability_message'])
            if mode == 'queue' and not state['queue_enabled']:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L442:then')
                raise ValueError(state['availability_message'])
            job = self.store.enqueue(case, request_key=request_key, priority=mode,
                                     queue_lane=lane)
            self.send(chat, self.ui.text('scenarios.launch.run_requested' if mode == 'run'
                                         else 'scenarios.launch.queued',
                                         case_name=case['name'], job_id=job['id'],
                                         queue=job_queue_id(job)))
        elif parts[0] == 'residual':
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L450:then')
            if not case.get('residual_pattern'):
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L451:then')
                self.send(chat, self.ui.text('scenarios.data.residual_path_required'))
                return
            files = residual_files(case)
            if not files:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L455:then')
                self.send(chat, self.ui.text('scenarios.data.residual_missing', pattern=case['residual_pattern']))
            for item in files:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L457:loop', item=item)
                self.file(chat, item)
        elif parts[0] == 'export' and len(parts) == 3:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L459:then')
            export = next((e for e in case['exports'] if e['name'] == parts[2]), None)
            if export is None:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L461:then')
                raise ValueError(self.ui.text('scenarios.data.unknown_export'))
            files = export_files(case, export)
            if not files:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L464:then')
                self.send(chat, self.ui.text('scenarios.data.export_missing', name=export['name']))
            for path in files:
                if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L466:loop', path=path)
                if path.stat().st_size > MAX_DOCUMENT:
                    if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L467:then')
                    self.send(chat, self.ui.text('scenarios.data.too_large', filename=path.name))
                else:
                    if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L467:else')
                    stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(path.stat().st_mtime))
                    self.file(chat, dict(path=str(path), kind=export['kind'], caption=self.ui.text(
                        'scenarios.data.caption', case_name=case['name'], name=export['name'], modified=stamp)))
        else:
            if _diagnostics.enabled: _diagnostics.step('bot.Bot.dispatch:L459:else')
            raise ValueError(self.ui.text('menus.home.unknown_request'))


@_diagnostics.trace
def remember_sent(store, chat, result):
    if result is None:
        if _diagnostics.enabled: _diagnostics.step('bot.remember_sent:L478:then')
        return
    results = result if isinstance(result, list) else [result]
    for message in results:
        if _diagnostics.enabled: _diagnostics.step('bot.remember_sent:L481:loop', message=message)
        if isinstance(message, dict) and type(message.get('message_id')) is int:
            if _diagnostics.enabled: _diagnostics.step('bot.remember_sent:L482:then')
            store.remember_message(chat, message['message_id'], message.get('date'))


@_diagnostics.trace
def deliver(store, api, text_path=None, ui=None):
    ui = ui or load_ui()
    for event in store.pending():
        if _diagnostics.enabled: _diagnostics.step('bot.deliver:L488:loop', event=event)
        body = event['body']
        try:
            if 'messages' not in body:
                if _diagnostics.enabled: _diagnostics.step('bot.deliver:L491:then')
                if body['kind'] == 'terminal':
                    if _diagnostics.enabled: _diagnostics.step('bot.deliver:L492:then')
                    run = body['run']
                    # PNGs were frozen at completion, before another run could overwrite them.
                    body.setdefault('files', [])
                    text = render_run(run, load_text(text_path, ui), 'completion', ui)
                    if body.get('notes'):
                        if _diagnostics.enabled: _diagnostics.step('bot.deliver:L497:then')
                        text += '\n' + ui.text('scenarios.data.attachment_notes', notes='\n'.join(body['notes']))
                else:
                    if _diagnostics.enabled: _diagnostics.step('bot.deliver:L492:else')
                    text = body['text']
                    body.setdefault('files', [])
                body['messages'] = list(chunks(text))
                body['message_index'] = 0
                body['file_index'] = 0
                store.save_delivery(event['id'], body)
            while body['message_index'] < len(body['messages']):
                if _diagnostics.enabled: _diagnostics.step('bot.deliver:L506:loop')
                result = api.send(event['chat_id'], body['messages'][body['message_index']])
                remember_sent(store, event['chat_id'], result)
                body['message_index'] += 1
                store.save_delivery(event['id'], body)
            while body['file_index'] < len(body['files']):
                if _diagnostics.enabled: _diagnostics.step('bot.deliver:L511:loop')
                item = body['files'][body['file_index']]
                try:
                    result = api.file(event['chat_id'], item)
                    remember_sent(store, event['chat_id'], result)
                except (FileNotFoundError, ValueError) as exc:
                    if _diagnostics.enabled: _diagnostics.step('bot.deliver:L516:except')
                    result = api.send(event['chat_id'], ui.text('scenarios.data.attachment_skipped', error=str(exc)))
                    remember_sent(store, event['chat_id'], result)
                body['file_index'] += 1
                store.save_delivery(event['id'], body)
            store.delivered(event['id'])
        except TelegramError as exc:
            if _diagnostics.enabled: _diagnostics.step('bot.deliver:L522:except')
            LOG.warning('Notification retry: %s', exc)
            store.retry(event['id'], exc.retry_after)
        except Exception as exc:
            if _diagnostics.enabled: _diagnostics.step('bot.deliver:L525:except')
            LOG.warning('Notification processing retry: %s', exc)
            store.retry(event['id'])


@_diagnostics.trace
def serve(config, store, stop=None):
    token = os.environ.get(config['telegram']['token_env'])
    if not token:
        if _diagnostics.enabled: _diagnostics.step('bot.serve:L532:then')
        raise ValueError(load_ui(config.get('_ui_dir')).text(
            'scenarios.diagnostics.service.token_missing', name=config['telegram']['token_env']))
    if not config['telegram']['allowed_user_ids'] or not config['telegram']['chat_ids']:
        if _diagnostics.enabled: _diagnostics.step('bot.serve:L535:then')
        raise ValueError(load_ui(config.get('_ui_dir')).text(
            'scenarios.diagnostics.service.allowlist_missing'))
    api = Telegram(token)
    stop = stop or threading.Event()
    bot = Bot(config, store, api)
    ui = bot.ui
    monitor = Monitor(config, store)

    @_diagnostics.trace
    def monitor_loop():
        while not stop.is_set():
            if _diagnostics.enabled: _diagnostics.step('bot.serve.monitor_loop:L545:loop')
            monitor.run_once()
            stop.wait(config['poll_seconds'])

    @_diagnostics.trace
    def notification_loop():
        while not stop.is_set():
            if _diagnostics.enabled: _diagnostics.step('bot.serve.notification_loop:L550:loop')
            deliver(store, api, config.get('_text_file'), ui)
            stop.wait(1)

    with DaemonLock(store.root / 'daemon.lock'):
        cases_for(config)
        load_text(config.get('_text_file'), ui)
        api.call('getMe', {})
        api.call('setMyCommands', {'commands': ui.value('menus.home.commands')})
        webhook = api.call('getWebhookInfo', {})
        if webhook.get('url'):
            if _diagnostics.enabled: _diagnostics.step('bot.serve:L560:then')
            raise ValueError(ui.text('scenarios.diagnostics.service.webhook'))
        workers = [threading.Thread(target=_diagnostics.inherit_context(monitor_loop), daemon=True),
                   threading.Thread(target=_diagnostics.inherit_context(notification_loop), daemon=True)]
        for thread in workers:
            if _diagnostics.enabled: _diagnostics.step('bot.serve:L564:loop', thread=thread)
            thread.start()
        try:
            while not stop.is_set():
                if _diagnostics.enabled: _diagnostics.step('bot.serve:L567:loop')
                try:
                    updates = api.updates(store.get('telegram_offset', 0))
                    for update in updates:
                        if _diagnostics.enabled: _diagnostics.step('bot.serve:L570:loop', update=update)
                        try:
                            bot.handle(update)
                        except Exception as exc:
                            if _diagnostics.enabled: _diagnostics.step('bot.serve:L573:except')
                            LOG.warning('Telegram request failed: %s', exc)
                        # A broken update must not indefinitely block subsequent buttons.
                        store.put('telegram_offset', update['update_id'] + 1)
                except TelegramError as exc:
                    if _diagnostics.enabled: _diagnostics.step('bot.serve:L577:except')
                    LOG.warning('Telegram polling: %s', exc)
                    stop.wait(max(3, exc.retry_after))
        finally:
            stop.set()
            for thread in workers:
                if _diagnostics.enabled: _diagnostics.step('bot.serve:L582:loop', thread=thread)
                thread.join(timeout=2)
