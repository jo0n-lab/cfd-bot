"""Telegram screens and input conversations backed by the native ticket service."""
from . import diagnostics as _diagnostics
from copy import deepcopy
import json
from pathlib import Path
import secrets
import shlex
import threading
import uuid

from .config import cpu_set, glob_patterns, inside, patterns
from .control import control_times
from .editor import (DEFAULT_SCRIPTS, EVENTS, TicketService, case_browser_start,
                     lines, numeric, script_commands, validate_export)
from .patterns import DEFAULT_NAME, PatternLibrary
from .processes import snapshot
from .tickets import STATES, discover_cases, has_postprocessing
from .telegram import TelegramError, chunks
from .ticket_run import TicketRunner

PAGE = 8
FIELD_KEYS = {
    'case_dir', 'name', 'filename', 'logs', 'residual_pattern', 'end_time',
    'failure_patterns', 'updated_files', 'macro_ticket', 'macro_cores',
    'macro_cpu_set', 'macro_command', 'preprocess', 'postprocess', 'x.name',
    'monitoring_command', 'x.pattern', 'x.max_files', 'template_name', 'browse_path',
    'case_include_patterns', 'case_exclude_patterns',
    'queue_id',
}
CLEARABLE = {'name', 'residual_pattern', 'end_time', 'failure_patterns', 'updated_files', 'macro_ticket',
             'preprocess', 'postprocess', 'case_include_patterns', 'case_exclude_patterns'}
COMMANDS = {'/start', '/help', '/stat', '/clean', '/queue', '/cases', '/data'}


@_diagnostics.trace
def short(value, length=150):
    value = str(value)
    return value if len(value) <= length else value[:length - 1] + '…'


class TicketChat:
    @_diagnostics.trace
    def __init__(self, bot):
        self.bot, self.store = bot, bot.store
        self.ui = bot.ui
        self.service = TicketService(Path(bot.config['_path']).parent / 'tickets')
        self.runner = TicketRunner(self.service, bot.config, self.store)
        self.library = PatternLibrary(self.service.folder.parent / 'ticket-patterns.json')
        self.lock = threading.RLock()
        self.workers = []
        self.instance_id = uuid.uuid4().hex

    @_diagnostics.trace
    def t(self, key, **values):
        return self.ui.text('menus.tickets.' + key, **values)

    @_diagnostics.trace
    def field_meta(self, key):
        resource = key.replace('.', '_')
        return (self.t(f'fields.{resource}.title'), self.t(f'fields.{resource}.hint'))

    @staticmethod
    @_diagnostics.trace
    def key(chat, user):
        return f'ticket-editor:{chat}:{user}'

    @_diagnostics.trace
    def load(self, chat, user):
        session = self.store.get(self.key(chat, user), {})
        if session.get('scan_id') and session.get('scan_owner') != self.instance_id:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.load:L63:then')
            session.pop('scan_id', None)
            self.persist(chat, user, session)
        return session

    @_diagnostics.trace
    def persist(self, chat, user, session):
        self.store.put(self.key(chat, user), session)

    @_diagnostics.trace
    def forget_panels(self, chat):
        """A cleared conversation has no usable editor prompts; retain drafts."""
        with self.lock, self.store.connect() as db:
            rows = db.execute('SELECT key, body FROM kv WHERE key LIKE ?',
                              (f'ticket-editor:{chat}:%',)).fetchall()
            for row in rows:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.forget_panels:L76:loop', row=row)
                session = json.loads(row['body'])
                for key in ('panel_id', 'token', 'pending'):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.forget_panels:L78:loop', key=key)
                    session.pop(key, None)
                session['view'] = 'away'
                db.execute('UPDATE kv SET body=? WHERE key=?', (json.dumps(session), row['key']))

    @_diagnostics.trace
    def render(self, chat, user, session, text, rows, view):
        parts = list(chunks(text, units=3400))
        text = parts[0] + ('\n…' if len(parts) > 1 else '')
        session.update(token=secrets.token_hex(5), view=view)
        markup = {'inline_keyboard': [[dict(text=short(label, 55),
                    callback_data=f"te:{session['token']}:{action}" + (f':{arg}' if arg is not None else ''))
                   for label, action, arg in row] for row in rows]}
        # Persist the new button generation before Telegram can deliver a click.
        self.persist(chat, user, session)
        panel = session.get('panel_id')
        if panel:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.render:L93:then')
            try:
                self.bot.api.call('editMessageText', dict(chat_id=chat, message_id=panel,
                                                         text=text, reply_markup=markup))
                return
            except TelegramError:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.render:L98:except')
                pass  # A deleted/old panel is replaced with a fresh message.
        sent = self.bot.send(chat, text, markup)
        messages = sent if isinstance(sent, list) else [sent]
        ids = [m['message_id'] for m in messages if isinstance(m, dict) and 'message_id' in m]
        if ids:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.render:L103:then')
            session['panel_id'] = ids[-1]
            self.persist(chat, user, session)

    @_diagnostics.trace
    def draft(self, session):
        if not session.get('draft'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.draft:L108:then')
            raise ValueError(self.t('errors.draft_required'))
        values = session['draft']['values']
        source = values.get('_source', {})
        values.setdefault('macro_cpu_policy', source.get('cpu_policy', 'manual' if source.get('cpu_set') else 'auto'))
        return session['draft']

    @_diagnostics.trace
    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if callback else update.get('message', {})
        chat = message['chat']['id']
        user = (callback or message)['from']['id']
        text = message.get('text', '')
        command = text.split()[0].split('@')[0] if text.split() else ''
        with self.lock:
            session = self.load(chat, user)
            try:
                if callback:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L125:then')
                    data = callback.get('data', '')
                    if data == 'tickets':
                        if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L127:then')
                        session.pop('pending', None)
                        session.pop('panel_id', None)
                        self.listing(chat, user, session)
                        return True
                    if data.startswith('ticketopen:'):
                        if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L132:then')
                        import hashlib
                        name = next((name for name in self.service.listing()
                                     if hashlib.sha256(str(self.service.path(name)).encode()).hexdigest()[:16] == data[11:]), None)
                        if name is None:
                            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L136:then')
                            raise ValueError(self.t('errors.open_required'))
                        session.pop('panel_id', None)
                        self.switch(chat, user, session, 'open', name)
                        return True
                    if not data.startswith('te:'):
                        if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L141:then')
                        session.pop('pending', None)
                        session['view'] = 'away'
                        self.persist(chat, user, session)
                        return False
                    parts = data.split(':', 3)
                    if len(parts) < 3 or parts[1] != session.get('token'):
                        if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L147:then')
                        self.bot.send(chat, self.t('errors.stale_button'))
                        return True
                    self.action(chat, user, session, parts[2], parts[3] if len(parts) == 4 else '')
                    return True
                if command in ('/tickets', '/ticket'):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L152:then')
                    session.pop('pending', None)
                    # Entry commands must answer at the end of the conversation.
                    # Editing an old panel in place looks like no response.
                    session.pop('panel_id', None)
                    self.listing(chat, user, session)
                    return True
                if command == '/cancel':
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L159:then')
                    session.pop('pending', None)
                    session.pop('browser', None)
                    session.pop('scan_id', None)
                    if session.get('draft'):
                        if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L163:then')
                        self.card(chat, user, session, self.t('notices.cancelled'))
                    else:
                        if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L163:else')
                        self.listing(chat, user, session)
                    return True
                if command in COMMANDS:
                    # Leaving the editor must not interpret subsequent conversation as a field.
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L168:then')
                    session.pop('pending', None)
                    session['view'] = 'away'
                    self.persist(chat, user, session)
                    return False
                if session.get('pending') and text:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L174:then')
                    self.input(chat, user, session, text)
                    return True
            except (ValueError, OSError, KeyError, IndexError) as exc:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L177:except')
                self.bot.send(chat, self.t('errors.prefix', error=exc))
                if session.get('pending'):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L179:then')
                    self.persist(chat, user, session)
                elif session.get('view', '').startswith('bulk'):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L181:then')
                    self.bulk_list(chat, user, session, session.get('bulk_page', 0))
                elif session.get('draft'):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L183:then')
                    self.card(chat, user, session)
                else:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.handle:L183:else')
                    self.listing(chat, user, session)
                return True
        return False

    @_diagnostics.trace
    def listing(self, chat, user, s, page=0):
        files = self.service.listing()
        page = max(0, min(int(page), max(0, (len(files) - 1) // PAGE)))
        s['choices'] = files[page * PAGE:(page + 1) * PAGE]
        rows = [[(name, 'open', i)] for i, name in enumerate(s['choices'])]
        rows += self.pages('list', page, len(files))
        if s.get('draft'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.listing:L196:then')
            rows.append([(self.t('list.resume'), 'card', None), (self.t('list.discard'), 'discard', None)])
        rows += [[(self.t('list.new_single'), 'new', 'single'), (self.t('list.new_macro'), 'new', 'macro')],
                 [(self.t('list.bulk'), 'bulk', None)], [(self.t('list.refresh'), 'list', page)]]
        self.render(chat, user, s, self.t('list.title', count=len(files)), rows, 'list')

    @_diagnostics.trace
    def bulk_list(self, chat, user, s, page=0):
        files = self.service.listing()
        selected = [name for name in s.get('bulk_selected', []) if name in files]
        page = max(0, min(int(page), max(0, (len(files) - 1) // PAGE)))
        s.update(bulk_selected=selected, bulk_page=page,
                 bulk_choices=files[page * PAGE:(page + 1) * PAGE])
        rows = [[((self.ui.text('strings.common.checked' if name in selected
                                else 'strings.common.unchecked')) + name, 'btoggle', index)]
                for index, name in enumerate(s['bulk_choices'])]
        rows += self.pages('bulkpage', page, len(files))
        rows += [[(self.t('list.page_all'), 'ball', None), (self.t('list.clear_all'), 'bnone', None)],
                 [(self.t('list.delete_selected', count=len(selected)), 'breview', None)],
                 [(self.t('list.back'), 'list', 0)]]
        self.render(chat, user, s, self.t('list.bulk_title', count=len(selected)), rows, 'bulk_list')

    @_diagnostics.trace
    def delete_review(self, chat, user, s, names, *, bulk=False):
        d = s.get('draft') or {}
        revisions = {d['current']: d['revision']} if d.get('current') and d.get('revision') else None
        plan = self.service.deletion_preview(names, revisions)
        s['delete_plan'] = plan
        text = self.t('list.delete_review', selected=len(names), total=len(plan['names']),
                      names='\n'.join(plan['names']))
        self.render(chat, user, s, text,
                    [[(self.t('list.delete_confirm'), 'bdelete' if bulk else 'deleteyes', None),
                      (self.ui.text('strings.common.cancel'), 'bulkpage' if bulk else 'card',
                       s.get('bulk_page', 0) if bulk else None)]],
                    'bulk_confirm' if bulk else 'delete_confirm')

    @_diagnostics.trace
    def delete_confirmed(self, chat, user, s):
        plan = s['delete_plan']
        deleted = self.service.delete_many(plan['names'], plan['revisions'])
        if (s.get('draft') or {}).get('current') in deleted:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.delete_confirmed:L233:then')
            s['draft'] = None
            s.pop('scan_id', None)
            s.pop('pending', None)
        s.pop('delete_plan', None)
        s.pop('bulk_selected', None)
        self.bot.send(chat, self.t('list.deleted', count=len(deleted)))
        self.listing(chat, user, s)

    @_diagnostics.trace
    def pages(self, action, page, count):
        row = []
        if page:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.pages:L244:then')
            row.append((self.t('list.previous'), action, page - 1))
        if (page + 1) * PAGE < count:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.pages:L246:then')
            row.append((self.t('list.next'), action, page + 1))
        return [row] if row else []

    @_diagnostics.trace
    def card(self, chat, user, s, notice=''):
        d = self.draft(s)
        v = d['values']
        s.pop('pending', None)
        rows = [[(self.t('card.basic'), 'basic', None), (self.t('card.rules'), 'rules', None)],
                [(self.t('card.exports'), 'exports', 0), (self.t('card.queue'), 'queue', None)],
                [(self.t('card.scripts'), 'scripts', None)],
                [(self.t('card.mode'), 'mode', None), (self.t('card.filename'), 'field', 'filename')],
                [(self.t('card.validate'), 'validate', None), (self.t('card.save'), 'review', 'save')]]
        state = None
        if d['current']:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.card:L260:then')
            try:
                state = self.runner.state(d['current'])
            except (OSError, ValueError):
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.card:L263:except')
                state = dict(run_enabled=False, queue_enabled=False, label=self.t('notices.run_check'))
            actions = [(self.t('notices.save_then_run') if d['dirty'] and state['run_enabled']
                        else state['label'], 'runreview' if state['run_enabled'] else 'runstate', 'run')]
            if state['queue_enabled']:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.card:L267:then')
                actions.append((state['queue_label'], 'runreview' if v.get('queue_id') else 'queuelanes',
                                'queue' if v.get('queue_id') else None))
            rows.insert(0, actions)
            rows.insert(1, [(self.t('card.refresh_run'), 'runstate', None)])
            rows += [[(self.t('card.duplicate'), 'duplicate', None),
                      (self.t('card.delete'), 'delete', None)]]
        else:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.card:L260:else')
            rows += [[(self.t('card.save_execute'), 'review', 'run'),
                      (self.t('card.save_queue'), 'review' if v.get('queue_id') else 'queuelanes',
                       'queue' if v.get('queue_id') else None)]]
        rows.append([(self.t('card.list'), 'list', 0)])
        kind = (self.t('card.macro') if v['task_type'] == 'macro'
                else self.t('card.single', role=v['role']))
        text = self.t(
            'card.body', kind=kind, edit_state=self.t('card.dirty' if d['dirty'] else 'card.editing'),
            filename=short(d['filename'] or self.ui.text('strings.common.auto_filename')),
            name=short(v['name'] or self.ui.text('strings.common.folder_name')),
            case_dir=short(v['case_dir'] or self.ui.text('strings.common.unspecified'), 700),
            logs=short(v['logs'], 450),
            residual=short(v['residual_pattern'] or self.ui.text('strings.common.unset')),
            end=self.end_label(v), exports=len(v['exports']))
        if v['task_type'] == 'macro':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.card:L289:then')
            existing = d['current'] and v['_source'].get('task_type') == 'macro'
            text += self.t('card.macro_existing' if existing else 'card.macro_new', count=len(v['cases']))
        if state and state.get('availability_message'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.card:L292:then')
            text += '\n\n' + state['availability_message']
        if notice:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.card:L294:then')
            text = notice + '\n\n' + text
        self.render(chat, user, s, text, rows, 'card')

    @_diagnostics.trace
    def end_label(self, values):
        if values['end_time'].strip():
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.end_label:L299:then')
            return self.t('card.end_explicit', value=values['end_time'])
        root = (self.service.folder / Path(values['case_dir']).expanduser()).resolve()
        control = control_times({'_root': str(root)})
        return (self.t('card.end_control', value=f"{control['end']:g}") if control and 'end' in control
                else self.t('card.end_each'))

    @_diagnostics.trace
    def basic(self, chat, user, s):
        v = self.draft(s)['values']
        rows = [[(self.t('basic.case_input'), 'field', 'case_dir'),
                 (self.t('basic.folder_select'), 'browse', 'case')],
                [(self.t('basic.display_name'), 'field', 'name')],
                [(self.t('basic.log_input'), 'field', 'logs'),
                 (self.t('basic.log_select'), 'browse', 'logs')],
                [(self.t('basic.residual'), 'field', 'residual_pattern')],
                [(self.t('basic.end'), 'field', 'end_time')]]
        if v['task_type'] != 'macro':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.basic:L315:then')
            rows[3].append((self.t('basic.png_select'), 'browse', 'residual'))
        rows += [[(self.t('basic.event',
                          mark=self.ui.text('strings.common.checked' if event in v['events']
                                            else 'strings.common.unchecked'),
                          label=self.t('basic.event_' + event)), 'event', event)]
                 for event in EVENTS]
        rows.append([(self.ui.text('strings.common.back'), 'card', None)])
        self.render(chat, user, s, self.t('basic.body'), rows, 'basic')

    @_diagnostics.trace
    def rules(self, chat, user, s):
        v = self.draft(s)['values']
        empty = self.ui.text('strings.common.empty')
        self.render(chat, user, s, self.t('rules.body',
                    patterns=short(v['failure_patterns'] or empty, 1400),
                    files=short(v['updated_files'] or empty, 500)),
                    [[(self.t('rules.pattern_input'), 'field', 'failure_patterns')],
                     [(self.t('rules.openfoam', mark=self.ui.text(
                         'strings.common.checked' if v['openfoam_defaults']
                         else 'strings.common.unchecked').strip()),
                       'toggle', 'openfoam_defaults')],
                     [(self.t('rules.failure_file'), 'field', 'updated_files')],
                     [(self.t('rules.load_template'), 'templates', 0),
                      (self.t('rules.save_template'), 'field', 'template_name')],
                     [(self.ui.text('strings.common.back'), 'card', None)]], 'rules')

    @_diagnostics.trace
    def queue(self, chat, user, s):
        v = self.draft(s)['values']
        macro = v['task_type'] == 'macro'
        child = not macro and v['role'] == 'child'
        explicit = macro or v.get('execution_source', 'case') == 'ticket'
        rows = []
        if not macro:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L347:then')
            rows = [[(self.t('queue.alone'), 'role', 'alone'), (self.t('queue.child'), 'role', 'child')],
                    [(self.t('queue.macro_path'), 'field', 'macro_ticket')]]
        if not macro and not child:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L350:then')
            rows.append([(self.t('queue.use_case' if explicit else 'queue.use_ticket'),
                          'execsource', 'case' if explicit else 'ticket')])
        automatic = v['macro_cpu_policy'] == 'auto'
        unspecified = self.ui.text('strings.common.unspecified')
        cpu_label = (self.t('queue.auto_label') if automatic else
                     self.t('queue.manual_label', cpu_set=short(v['macro_cpu_set'] or unspecified)))
        if (explicit or child):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L357:then')
            text = self.t('queue.execution_body', cores=v['macro_cores'] or unspecified,
                          cpu=cpu_label, command=short(v['macro_command'], 600))
        else:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L357:else')
            text = self.t('queue.case_body')
        if child:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L362:then')
            text = self.t('queue.inherited', macro=short(v['macro_ticket'] or unspecified)) + '\n\n' + text
        if explicit and not child:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L364:then')
            rows += [[(self.t('queue.cores'), 'field', 'macro_cores')],
                     [(self.t('queue.command'), 'field', 'macro_command')]]
            if automatic:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L367:then')
                rows.append([(self.t('queue.manual'), 'cpupolicy', 'manual')])
            else:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L367:else')
                rows += [[(self.t('queue.automatic'), 'cpupolicy', 'auto')],
                         [(self.t('queue.cpu_set'), 'field', 'macro_cpu_set')],
                         [(self.t('queue.cross_socket', mark=self.ui.text(
                             'strings.common.checked' if v['macro_cross_socket']
                             else 'strings.common.unchecked').strip()),
                           'toggle', 'macro_cross_socket')]]
        if not child:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L376:then')
            rows.append([(self.t('queue.queue_id'), 'field', 'queue_id')])
        if macro:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L378:then')
            rows.append([(self.t('queue.dynamic', mark=self.ui.text(
                'strings.common.checked' if v.get('dynamic_cores')
                else 'strings.common.unchecked').strip()), 'toggle', 'dynamic_cores')])
            rows += [[(self.t('queue.include_patterns'), 'field', 'case_include_patterns'),
                      (self.t('queue.exclude_patterns'), 'field', 'case_exclude_patterns')],
                     [(self.t('queue.scan'), 'scan', None),
                      (self.t('queue.members'), 'members', 0)]]
            text = self.t('queue.macro_body', cores=v['macro_cores'] or unspecified,
                          cpu=cpu_label, command=short(v['macro_command'], 600), count=len(v['cases']),
                          include=short(v.get('case_include_patterns') or unspecified, 300),
                          exclude=short(v.get('case_exclude_patterns') or unspecified, 300))
        elif not child:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L390:then')
            text = self.t('queue.single_body', role=v['role'],
                          macro=short(v['macro_ticket'] or self.ui.text('strings.common.none'))) + '\n\n' + text
        monitoring = v.get('monitoring_cpu', False)
        text += '\n\n' + (self.t(
            'queue.monitor_status', state=self.t('queue.monitor_enabled'),
            command=short(v.get('monitoring_command', './Allmonitor')))
            if monitoring else self.t('queue.monitor_status_disabled'))
        if not child:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L398:then')
            if v.get('dynamic_cores'):
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L399:then')
                quota = self.t('queue.quota_dynamic')
            elif v.get('execution_source') == 'case':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L401:then')
                quota = self.t('queue.quota_case')
            else:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L401:else')
                quota = self.t('queue.quota_fixed', cores=v.get('macro_cores') or unspecified)
            text += '\n\n' + self.t('queue.profile',
                queue=short(v.get('queue_id') or unspecified),
                quota=quota)
        if not child:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L408:then')
            rows.append([(self.t('queue.monitor_cpu', mark=self.ui.text(
                'strings.common.checked' if monitoring else 'strings.common.unchecked').strip()),
                          'toggle', 'monitoring_cpu')])
            if monitoring:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.queue:L412:then')
                rows.append([(self.t('queue.monitor_command'), 'field', 'monitoring_command')])
        rows.append([(self.ui.text('strings.common.back'), 'card', None)])
        self.render(chat, user, s, text, rows, 'queue')

    @_diagnostics.trace
    def scripts(self, chat, user, s):
        v = self.draft(s)['values']
        unused = self.ui.text('strings.common.not_used')
        self.render(chat, user, s, self.t('scripts.body',
                    preprocess=short(v.get('preprocess') or unused, 800),
                    postprocess=short(v.get('postprocess') or unused, 800)),
                    [[(self.t('scripts.edit_pre'), 'field', 'preprocess'),
                      (self.t('scripts.default_pre'), 'scriptdefault', 'preprocess')],
                     [(self.t('scripts.edit_post'), 'field', 'postprocess'),
                      (self.t('scripts.default_post'), 'scriptdefault', 'postprocess')],
                     [(self.ui.text('strings.common.back'), 'card', None)]], 'scripts')

    @_diagnostics.trace
    def field(self, chat, user, s, key):
        if key not in FIELD_KEYS:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.field:L430:then')
            raise ValueError(self.t('errors.unsupported_field'))
        d = self.draft(s)
        current = (d['filename'] if key == 'filename' else s.get('export_edit', {}).get(key[2:], '')
                   if key.startswith('x.') else d['values'].get(key, ''))
        s['pending'] = key
        rows = []
        if key in CLEARABLE:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.field:L437:then')
            rows.append([(self.t('input.control_default' if key == 'end_time' else 'input.clear'),
                          'clear', key)])
        rows.append([(self.t('input.cancel'), 'backinput', None)])
        title, hint = self.field_meta(key)
        self.render(chat, user, s, self.t('input.body', title=title, hint=hint,
                    current=short(current or self.ui.text('strings.common.empty'), 1800)), rows, 'input')

    @_diagnostics.trace
    def apply_field(self, s, key, text):
        d = self.draft(s)
        v = d['values']
        value = text.strip()
        root = (self.service.folder / Path(v['case_dir']).expanduser()).resolve()
        if key == 'filename':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L450:then')
            d['filename'] = self.service.filename(v, value)
        elif key.startswith('x.'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L452:then')
            if key == 'x.max_files':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L453:then')
                value = numeric(value, self.t('fields.x_max_files.title'), integer=True, minimum=1)
                if value > 10:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L455:then')
                    raise ValueError(self.t('errors.max_files'))
            s['export_edit'][key[2:]] = value
            return
        else:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L452:else')
            if key == 'case_dir':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L460:then')
                path = (self.service.folder / Path(value).expanduser()).resolve()
                if not value or not path.is_dir():
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L462:then')
                    raise ValueError(self.t('errors.case_directory'))
                value = str(path)
                if value != v['case_dir']:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L465:then')
                    v['cases'] = []
                if not v['name'] or v['name'] == s.get('auto_name'):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L467:then')
                    v['name'] = path.name
                    s['auto_name'] = path.name
                if not d['filename'] or d['filename'] == s.get('auto_filename'):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L470:then')
                    d['filename'] = self.service.filename(dict(v, case_dir=value), path.name + '.json')
                    s['auto_filename'] = d['filename']
            elif key in ('end_time', 'macro_cores') and value:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L473:then')
                numeric(value, self.field_meta(key)[0], integer=key == 'macro_cores',
                        minimum=1 if key == 'macro_cores' else 0)
            elif key == 'macro_cpu_set':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L476:then')
                if v['macro_cpu_policy'] != 'manual':
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L477:then')
                    raise ValueError(self.t('errors.automatic_cpu'))
                cpu_set(value)
            elif key == 'queue_id' and value:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L480:then')
                import re
                if not re.fullmatch(r'[A-Za-z0-9._-]{1,48}', value):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L482:then')
                    raise ValueError(self.t('errors.queue_id'))
            elif key == 'macro_command':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L484:then')
                if not shlex.split(value):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L485:then')
                    raise ValueError(self.t('errors.command_required'))
            elif key == 'monitoring_command':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L487:then')
                if not shlex.split(value):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L488:then')
                    raise ValueError(self.t('errors.monitor_command_required'))
            elif key in DEFAULT_SCRIPTS:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L490:then')
                script_commands(value)
            elif key == 'failure_patterns':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L492:then')
                patterns(lines(text, strip=False), self.t('fields.failure_patterns.title'))
                value = text
            elif key in ('case_include_patterns', 'case_exclude_patterns'):
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L495:then')
                values = lines(value)
                glob_patterns(values, self.field_meta(key)[0])
                value = '\n'.join(values)
            elif key in ('logs', 'updated_files'):
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L499:then')
                paths = lines(value)
                if key == 'logs' and not paths:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L501:then')
                    raise ValueError(self.t('errors.logs_required'))
                if len(paths) != len(set(paths)):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L503:then')
                    raise ValueError(self.t('errors.duplicate_paths'))
                for path in paths:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L505:loop', path=path)
                    inside(root, path)
            elif key == 'residual_pattern' and value:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L507:then')
                inside(root, value)
                if not value.lower().endswith('.png'):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L509:then')
                    raise ValueError(self.t('errors.png_pattern'))
            elif key == 'macro_ticket' and value:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.apply_field:L511:then')
                inside(self.service.folder, value)
            v[key] = value
        d['dirty'] = True

    @_diagnostics.trace
    def input(self, chat, user, s, text):
        key = s['pending']
        if key.startswith('case_core:'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.input:L518:then')
            index = int(key.split(':', 1)[1])
            value = numeric(text.strip(), self.t('fields.member_cores.title'),
                            integer=True, minimum=1)
            self.draft(s)['values']['cases'][index]['cores'] = value
            self.draft(s)['dirty'] = True
            s.pop('pending', None)
            self.members(chat, user, s, index // PAGE)
            return
        if key == 'template_name':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.input:L527:then')
            name = text.strip()
            if name == DEFAULT_NAME or not name or len(name) > 80:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.input:L529:then')
                raise ValueError(self.t('errors.template_name'))
            s.pop('pending', None)
            s['template_name'] = name
            if name in self.library.load():
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.input:L533:then')
                self.render(chat, user, s, self.t('rules.template_overwrite', name=short(name)),
                            [[(self.t('rules.overwrite'), 'savetemplate', None),
                              (self.ui.text('strings.common.cancel'), 'rules', None)]], 'template_confirm')
            else:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.input:L533:else')
                self.save_template(chat, user, s)
            return
        if key == 'browse_path':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.input:L540:then')
            self.set_browser_path(s, text)
            s.pop('pending', None)
            self.browser(chat, user, s)
            return
        self.apply_field(s, key, text)
        s.pop('pending', None)
        if key.startswith('x.'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.input:L547:then')
            self.export_card(chat, user, s)
        else:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.input:L547:else')
            self.card(chat, user, s, self.t('notices.field_applied', field=self.field_meta(key)[0]))

    @_diagnostics.trace
    def browse_start(self, chat, user, s, kind):
        if kind not in ('case', 'logs', 'residual'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browse_start:L553:then')
            raise ValueError(self.t('errors.browse_kind'))
        v = self.draft(s)['values']
        if kind == 'case':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browse_start:L556:then')
            path = case_browser_start(v['case_dir'], self.service.folder)
            root = None
        else:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browse_start:L556:else')
            if not v['case_dir']:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browse_start:L560:then')
                raise ValueError(self.t('errors.case_first'))
            path = (self.service.folder / Path(v['case_dir']).expanduser()).resolve()
            if v['task_type'] == 'macro':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browse_start:L563:then')
                if not v['cases']:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browse_start:L564:then')
                    raise ValueError(self.t('errors.macro_scan_first'))
                path = Path(v['cases'][0]['case_dir'])
            root = str(path)
        if not path.is_dir():
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browse_start:L568:then')
            raise ValueError(self.t('errors.folder_retry'))
        s['browser'] = dict(kind=kind, root=root, path=str(path), selected=[])
        self.browser(chat, user, s)

    @_diagnostics.trace
    def set_browser_path(self, s, value):
        b = s['browser']
        path = Path(value).expanduser().resolve()
        if b['root'] and not path.is_relative_to(Path(b['root'])):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.set_browser_path:L576:then')
            raise ValueError(self.t('errors.inside_case'))
        if not path.is_dir():
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.set_browser_path:L578:then')
            raise ValueError(self.t('errors.folder_missing'))
        b['path'] = str(path)

    @_diagnostics.trace
    def browser(self, chat, user, s, page=0):
        b = s['browser']
        path = Path(b['path'])
        entries = []
        for p in path.iterdir():
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browser:L586:loop', p=p)
            if p.name.startswith('.'):
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browser:L587:then')
                continue
            if b['root'] and not p.resolve().is_relative_to(Path(b['root'])):
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browser:L589:then')
                continue
            if p.is_dir() or (b['kind'] != 'case' and p.is_file()
                              and (b['kind'] != 'residual' or p.suffix.lower() == '.png')):
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browser:L591:then')
                entries.append(p)
        entries.sort(key=lambda p: (not p.is_dir(), p.name))
        page = max(0, min(int(page), max(0, (len(entries) - 1) // PAGE)))
        b['page'] = page
        b['options'] = [str(p) for p in entries[page * PAGE:(page + 1) * PAGE]]
        rows = []
        for i, name in enumerate(b['options']):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browser:L599:loop', i=i, name=name)
            p = Path(name)
            selected = b['root'] and str(p.relative_to(b['root'])) in b['selected']
            marker = (self.ui.text('strings.common.folder_icon') if p.is_dir() else
                      self.ui.text('strings.common.checked' if selected else 'strings.common.unchecked'))
            rows.append([(marker + p.name,
                           'bd' if p.is_dir() else 'bf', i)])
        rows += self.pages('bp', page, len(entries))
        if path.parent != path and (not b['root'] or path != Path(b['root'])):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browser:L607:then')
            rows.append([(self.t('browser.parent'), 'bup', None)])
        rows.append([(self.t('browser.move'), 'field', 'browse_path')])
        if b['kind'] == 'case':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browser:L610:then')
            rows.append([(self.t('browser.select_folder'), 'bapply', None)])
        elif b['kind'] == 'logs':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.browser:L612:then')
            rows.append([(self.t('browser.apply_logs', count=len(b['selected'])), 'bapply', None)])
        rows.append([(self.t('browser.cancel'), 'bcancel', None)])
        macro_note = (self.t('browser.macro_note')
                      if self.draft(s)['values']['task_type'] == 'macro' and b['kind'] == 'logs' else '')
        self.render(chat, user, s, self.t('browser.body', path=short(path, 1400),
                                         macro_note=macro_note), rows, 'browser')

    @_diagnostics.trace
    def exports(self, chat, user, s, page=0):
        items = self.draft(s)['values']['exports']
        page = max(0, min(int(page), max(0, (len(items) - 1) // PAGE)))
        rows = [[(item['name'] + ' · ' + short(item['pattern'], 25), 'xe', i)]
                for i, item in enumerate(items) if page * PAGE <= i < (page + 1) * PAGE]
        rows += self.pages('exports', page, len(items))
        rows += [[(self.t('exports.add'), 'xnew', None)],
                 [(self.ui.text('strings.common.back'), 'card', None)]]
        self.render(chat, user, s, self.t('exports.body', count=len(items)), rows, 'exports')

    @_diagnostics.trace
    def export_card(self, chat, user, s):
        item = s['export_edit']
        rows = [[(self.t('exports.name'), 'field', 'x.name')],
                [(self.t('exports.pattern'), 'field', 'x.pattern')],
                [(self.t('exports.photo' if item.get('kind') == 'photo' else 'exports.document'),
                  'xkind', None)],
                [(self.t('exports.max_files'), 'field', 'x.max_files')],
                [(self.ui.text('strings.common.apply'), 'xapply', None)]]
        if s.get('export_index') is not None:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.export_card:L638:then')
            rows[-1].append((self.ui.text('strings.common.delete'), 'xdelete', None))
        rows.append([(self.t('exports.cancel'), 'exports', 0)])
        labels = (('name', self.t('exports.field_name')), ('pattern', self.t('exports.field_pattern')),
                  ('kind', self.t('exports.field_kind')), ('max_files', self.t('exports.field_max')))
        self.render(chat, user, s, self.t('exports.edit') + '\n' + '\n'.join(
            self.t('exports.field_line', label=label, value=short(item.get(key, ''), 500))
            for key, label in labels), rows, 'export')

    @_diagnostics.trace
    def templates(self, chat, user, s, page=0):
        names = list(self.library.load())
        page = max(0, min(int(page), max(0, (len(names) - 1) // PAGE)))
        s['templates'] = names[page * PAGE:(page + 1) * PAGE]
        rows = [[(name, 'template', i)] for i, name in enumerate(s['templates'])]
        rows += self.pages('templates', page, len(names))
        rows.append([(self.ui.text('strings.common.back'), 'rules', None)])
        self.render(chat, user, s, self.t('rules.template_title'), rows, 'templates')

    @_diagnostics.trace
    def save_template(self, chat, user, s):
        v = self.draft(s)['values']
        self.library.save(s['template_name'], dict(failure_patterns=lines(v['failure_patterns'], strip=False),
                                                  openfoam_defaults=v['openfoam_defaults']))
        self.rules(chat, user, s)

    @_diagnostics.trace
    def members(self, chat, user, s, page=0):
        cases = self.draft(s)['values']['cases']
        page = max(0, min(int(page), max(0, (len(cases) - 1) // PAGE)))
        rows, text = [], [self.t('members.title', count=len(cases))]
        for i, row in enumerate(cases):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.members:L666:loop', i=i, row=row)
            if not page * PAGE <= i < (page + 1) * PAGE:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.members:L667:then')
                continue
            marker = self.ui.text('strings.common.postprocess_icon') if has_postprocessing(row['case_dir']) else ''
            text.append(self.t('members.line', marker=marker, index=i + 1,
                               path=short(row['case_dir'], 260),
                               reason=short(row.get('reason') or STATES.get(row['state'], row['state']), 110)))
            buttons = [(self.t('members.button', marker=marker, index=i + 1,
                               name=Path(row['case_dir']).name), 'members', page)]
            if self.draft(s)['values'].get('dynamic_cores'):
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.members:L675:then')
                buttons.append((self.t('members.cores', cores=row.get('cores') or
                                       self.draft(s)['values'].get('macro_cores') or 1),
                                'membercore', i))
            buttons.append((self.t('members.remove'), 'remove', i))
            rows.append(buttons)
        rows += self.pages('members', page, len(cases))
        rows += [[(self.t('members.rescan'), 'scan', None)],
                 [(self.ui.text('strings.common.back'), 'queue', None)]]
        self.render(chat, user, s, '\n\n'.join(text), rows, 'members')

    @_diagnostics.trace
    def scan(self, chat, user, s):
        v = self.draft(s)['values']
        if v['task_type'] != 'macro' or not v['case_dir']:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan:L688:then')
            raise ValueError(self.t('errors.macro_required'))
        if s.get('scan_id'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan:L690:then')
            self.render(chat, user, s, self.t('members.searching'),
                        [[(self.t('members.cancel'), 'stopscan', None)]], 'scanning')
            return
        scan_id = uuid.uuid4().hex
        s['scan_id'] = scan_id
        s['scan_owner'] = self.instance_id
        case_dir = v['case_dir']
        root = str((self.service.folder / Path(case_dir).expanduser()).resolve())
        end_text = v['end_time']
        include_text = v.get('case_include_patterns', '')
        exclude_text = v.get('case_exclude_patterns', '')
        include_patterns = lines(include_text)
        exclude_patterns = lines(exclude_text)
        end = numeric(end_text, self.t('fields.end_time.title')) if end_text.strip() else None
        self.render(chat, user, s, self.t('members.checking'),
                    [[(self.t('members.cancel'), 'stopscan', None)]], 'scanning')
        @_diagnostics.trace
        def work():
            try:
                observed = snapshot(self.bot.config['ofps_command'])
                result = discover_cases(root, observed['cases'], end,
                                        include_patterns, exclude_patterns)
            except Exception as exc:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan.work:L712:except')
                result = exc
            with self.lock:
                current = self.load(chat, user)
                if current.get('scan_id') != scan_id:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan.work:L716:then')
                    return
                current.pop('scan_id', None)
                values = self.draft(current)['values']
                if (values['case_dir'] != case_dir or values['end_time'] != end_text
                        or values.get('case_include_patterns', '') != include_text
                        or values.get('case_exclude_patterns', '') != exclude_text
                        or values['task_type'] != 'macro'):
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan.work:L720:then')
                    self.persist(chat, user, current)
                    return
                try:
                    if isinstance(result, Exception):
                        if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan.work:L727:then')
                        self.card(chat, user, current, self.t('notices.search_failed', error=result))
                    else:
                        if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan.work:L727:else')
                        values['cases'], skipped = result
                        if values.get('dynamic_cores'):
                            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan.work:L731:then')
                            default = int(values.get('macro_cores') or 1)
                            for row in values['cases']:
                                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan.work:L733:loop', row=row)
                                row['cores'] = default
                        current['draft']['dirty'] = True
                        if current.get('view') == 'scanning':
                            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan.work:L736:then')
                            self.members(chat, user, current)
                        else:
                            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan.work:L736:else')
                            self.persist(chat, user, current)
                            self.bot.send(chat, self.t('notices.search_done', included=len(values['cases']),
                                                       excluded=len(skipped)))
                except TelegramError:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.scan.work:L742:except')
                    self.persist(chat, user, current)
        thread = threading.Thread(target=_diagnostics.inherit_context(work), daemon=True)
        self.workers = [w for w in self.workers if w.is_alive()]
        self.workers.append(thread)
        thread.start()

    @_diagnostics.trace
    def switch(self, chat, user, s, action, argument):
        if s.get('draft', {}).get('dirty'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.switch:L750:then')
            s['switch'] = [action, argument]
            self.render(chat, user, s, self.t('switch.body'),
                        [[(self.t('switch.discard'), 'switch', None)],
                         [(self.t('switch.back'), 'card', None)]], 'switch')
            return
        self.do_switch(chat, user, s, action, argument)

    @_diagnostics.trace
    def do_switch(self, chat, user, s, action, arg):
        if action == 'new':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.do_switch:L759:then')
            draft = self.service.new(arg)
        elif action == 'open':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.do_switch:L761:then')
            draft = self.service.open(arg)
        elif action == 'duplicate':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.do_switch:L763:then')
            draft = self.service.duplicate(arg)
        elif action == 'discard':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.do_switch:L765:then')
            draft = None
        else:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.do_switch:L765:else')
            raise ValueError(self.t('errors.switch'))
        panel = s.get('panel_id')
        s.clear()
        s.update(draft=draft, panel_id=panel)
        if draft:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.do_switch:L772:then')
            self.card(chat, user, s)
        else:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.do_switch:L772:else')
            self.listing(chat, user, s)

    @_diagnostics.trace
    def review(self, chat, user, s, mode):
        d = self.draft(s)
        if s.get('scan_id'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.review:L779:then')
            raise ValueError(self.t('errors.scan_before_save'))
        data = self.service.validate(d['values'], d['filename'], d['current'])
        filename = self.service.filename(d['values'], d['filename'])
        destination = self.service.path(filename)
        overwrite = filename != d['current'] and destination.exists()
        mode, _, lane_text = mode.partition(':')
        lane = int(lane_text) if lane_text else 1
        s['save_action'] = dict(mode=mode, lane=lane, request_id=uuid.uuid4().hex,
                                overwrite=overwrite)
        existing_macro = (d['current'] and d['values']['_source'].get('task_type') == 'macro'
                          and data['task_type'] == 'macro')
        request_mode = 'queue' if mode == 'submit' else mode
        wants_request = request_mode in ('run', 'queue')
        queue = wants_request and not existing_macro
        text = self.t('review.body', filename=filename, name=short(data['name']),
                      case_dir=short(data['case_dir'], 600))
        if data['task_type'] == 'macro':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.review:L796:then')
            text += self.t('review.macro', count=len(data['cases']), cores=data['cores'],
                           cpu=data.get('cpu_set', self.bot.ui.text('scenarios.launch.automatic_cpu')),
                           cross_socket=self.ui.text('strings.common.enabled' if data.get('allow_cross_socket')
                                                     else 'strings.common.disabled'),
                           command=short(shlex.join(data['command']), 600))
        unused = self.ui.text('strings.common.not_used')
        text += self.t('review.pre', value=short(d['values'].get('preprocess') or unused, 300))
        text += self.t('review.post', value=short(d['values'].get('postprocess') or unused, 300))
        text += self.t('review.monitor', value=(short(d['values'].get('monitoring_command'))
                                               if d['values'].get('monitoring_cpu') else unused))
        will_run = wants_request or queue
        text += self.t('review.will_run' if will_run else 'review.save_only')
        if existing_macro:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.review:L809:then')
            text += self.t('review.macro_run' if will_run else 'review.macro_save')
        if overwrite:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.review:L811:then')
            text += self.t('review.overwrite_notice')
        self.render(chat, user, s, text,
                    [[(self.t('review.overwrite_run') if overwrite and will_run else
                       self.t('review.overwrite') if overwrite else self.t('review.save_run') if will_run
                       else self.t('review.save'), 'save', None)],
                     [(self.t('review.back'), 'card', None)]], 'save_confirm')

    @_diagnostics.trace
    def action(self, chat, user, s, op, arg):
        if op == 'list': _diagnostics.step('ticket_chat.TicketChat.action:L820:then') if _diagnostics.enabled else None; self.listing(chat, user, s, arg or 0)
        elif op == 'bulk':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L821:then')
            s['bulk_selected'] = []
            self.bulk_list(chat, user, s)
        elif op == 'bulkpage': _diagnostics.step('ticket_chat.TicketChat.action:L824:then') if _diagnostics.enabled else None; self.bulk_list(chat, user, s, arg or 0)
        elif op == 'btoggle':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L825:then')
            name = s['bulk_choices'][int(arg)]
            selected = s['bulk_selected']
            if name in selected: _diagnostics.step('ticket_chat.TicketChat.action:L828:then') if _diagnostics.enabled else None; selected.remove(name)
            else: _diagnostics.step('ticket_chat.TicketChat.action:L828:else') if _diagnostics.enabled else None; selected.append(name)
            self.bulk_list(chat, user, s, s['bulk_page'])
        elif op == 'ball':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L831:then')
            s['bulk_selected'] = self.service.listing()
            self.bulk_list(chat, user, s, s['bulk_page'])
        elif op == 'bnone':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L834:then')
            s['bulk_selected'] = []
            self.bulk_list(chat, user, s, s['bulk_page'])
        elif op == 'breview': _diagnostics.step('ticket_chat.TicketChat.action:L837:then') if _diagnostics.enabled else None; self.delete_review(chat, user, s, s['bulk_selected'], bulk=True)
        elif op in ('bdelete', 'deleteyes'): _diagnostics.step('ticket_chat.TicketChat.action:L838:then') if _diagnostics.enabled else None; self.delete_confirmed(chat, user, s)
        elif op == 'new': _diagnostics.step('ticket_chat.TicketChat.action:L839:then') if _diagnostics.enabled else None; self.switch(chat, user, s, 'new', arg)
        elif op == 'open': _diagnostics.step('ticket_chat.TicketChat.action:L840:then') if _diagnostics.enabled else None; self.switch(chat, user, s, 'open', s['choices'][int(arg)])
        elif op == 'switch': _diagnostics.step('ticket_chat.TicketChat.action:L841:then') if _diagnostics.enabled else None; self.do_switch(chat, user, s, *s['switch'])
        elif op == 'discard': _diagnostics.step('ticket_chat.TicketChat.action:L842:then') if _diagnostics.enabled else None; self.switch(chat, user, s, 'discard', '')
        elif op == 'card': _diagnostics.step('ticket_chat.TicketChat.action:L843:then') if _diagnostics.enabled else None; self.card(chat, user, s)
        elif op == 'scripts': _diagnostics.step('ticket_chat.TicketChat.action:L844:then') if _diagnostics.enabled else None; self.scripts(chat, user, s)
        elif op == 'scriptdefault':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L845:then')
            self.apply_field(s, arg, DEFAULT_SCRIPTS[arg])
            self.scripts(chat, user, s)
        elif op == 'runstate':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L848:then')
            d = self.draft(s)
            state = self.runner.state(d['current'], fresh=True)
            self.card(chat, user, s, state.get('availability_message', state['label']))
        elif op == 'runreview':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L852:then')
            d = self.draft(s)
            state = self.runner.state(d['current'], fresh=True)
            mode, _, lane_text = (arg or 'run').partition(':')
            lane = int(lane_text) if lane_text else 1
            allowed = state['run_enabled'] if mode == 'run' else state['queue_enabled']
            if not allowed:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L858:then')
                self.card(chat, user, s,
                          state.get('availability_message', self.t('notices.already_running')))
                return
            if d['dirty']:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L862:then')
                self.review(chat, user, s, mode)
                return
            if state['state'] == 'queued':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L865:then')
                self.card(chat, user, s, self.t('notices.already_queued'))
                return
            s['run_request'] = uuid.uuid4().hex
            s['run_mode'] = mode
            s['run_lane'] = lane
            detail = self.t('run.macro' if d['values']['task_type'] == 'macro' else 'run.single')
            self.render(chat, user, s, self.t('run.confirm', filename=d['current'], detail=detail,
                                             action=self.t('run.action_' + mode)),
                        [[(self.t('run.button_' + mode), 'runyes', None),
                          (self.ui.text('strings.common.cancel'), 'card', None)]], 'run_confirm')
        elif op == 'runyes':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L876:then')
            d = self.draft(s)
            mode = s.get('run_mode', 'run')
            result = self.runner.request(d['current'], expected_revision=d['revision'],
                                         request_id=s['run_request'], mode=mode,
                                         lane=s.get('run_lane', 1))
            s['draft'] = self.service.open(d['current'])
            self.card(chat, user, s, self.t('notices.already_queued' if result['already_queued']
                                            else 'notices.run_requested' if mode == 'run'
                                            else 'notices.queued'))
        elif op == 'basic': _diagnostics.step('ticket_chat.TicketChat.action:L886:then') if _diagnostics.enabled else None; self.basic(chat, user, s)
        elif op == 'queuelanes':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L887:then')
            target = 'runreview' if self.draft(s)['current'] else 'review'
            self.render(chat, user, s, self.t('run.lane_title'),
                        [[(self.t('run.lane', lane=lane), target, f'queue:{lane}')]
                         for lane in range(1, 4)] +
                        [[(self.ui.text('strings.common.cancel'), 'card', None)]], 'queue_lane')
        elif op == 'rules': _diagnostics.step('ticket_chat.TicketChat.action:L893:then') if _diagnostics.enabled else None; self.rules(chat, user, s)
        elif op == 'queue': _diagnostics.step('ticket_chat.TicketChat.action:L894:then') if _diagnostics.enabled else None; self.queue(chat, user, s)
        elif op == 'field': _diagnostics.step('ticket_chat.TicketChat.action:L895:then') if _diagnostics.enabled else None; self.field(chat, user, s, arg)
        elif op == 'clear': _diagnostics.step('ticket_chat.TicketChat.action:L896:then') if _diagnostics.enabled else None; self.input(chat, user, s, '') if s.get('pending') == arg and arg in CLEARABLE else self.card(chat, user, s)
        elif op == 'backinput':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L897:then')
            previous = s.pop('pending', '')
            if previous.startswith('x.'): _diagnostics.step('ticket_chat.TicketChat.action:L899:then') if _diagnostics.enabled else None; self.export_card(chat, user, s)
            elif previous == 'browse_path': _diagnostics.step('ticket_chat.TicketChat.action:L900:then') if _diagnostics.enabled else None; self.browser(chat, user, s)
            else: _diagnostics.step('ticket_chat.TicketChat.action:L900:else') if _diagnostics.enabled else None; self.card(chat, user, s)
        elif op == 'mode':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L902:then')
            self.render(chat, user, s, self.t('run.mode_title'),
                        [[(self.t('run.single_button'), 'setmode', 'single'),
                          (self.t('run.macro_button'), 'setmode', 'macro')],
                         [(self.ui.text('strings.common.back'), 'card', None)]], 'mode')
        elif op == 'setmode':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L907:then')
            if arg not in ('single', 'macro'): _diagnostics.step('ticket_chat.TicketChat.action:L908:then') if _diagnostics.enabled else None; raise ValueError(self.t('errors.mode'))
            d = self.draft(s)
            d['values'].update(task_type=arg, cases=[])
            if arg == 'macro': _diagnostics.step('ticket_chat.TicketChat.action:L911:then') if _diagnostics.enabled else None; d['values']['role'] = 'alone'
            d['filename'] = self.service.filename(d['values'], d['filename'])
            d['dirty'] = True
            self.card(chat, user, s)
        elif op == 'role':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L915:then')
            if arg not in ('alone', 'child'): _diagnostics.step('ticket_chat.TicketChat.action:L916:then') if _diagnostics.enabled else None; raise ValueError(self.t('errors.role'))
            d = self.draft(s)
            d['values']['role'] = arg
            d['dirty'] = True
            self.queue(chat, user, s)
        elif op == 'event':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L921:then')
            if arg not in EVENTS: _diagnostics.step('ticket_chat.TicketChat.action:L922:then') if _diagnostics.enabled else None; raise ValueError(self.t('errors.event'))
            d = self.draft(s)
            events = set(d['values']['events'])
            events.symmetric_difference_update({arg})
            d['values']['events'] = [event for event in EVENTS if event in events]
            d['dirty'] = True
            self.basic(chat, user, s)
        elif op == 'execsource':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L929:then')
            if arg not in ('case', 'ticket'): _diagnostics.step('ticket_chat.TicketChat.action:L930:then') if _diagnostics.enabled else None; raise ValueError(self.t('errors.setting'))
            d = self.draft(s)
            if d['values']['task_type'] != 'single' or d['values']['role'] != 'alone':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L932:then')
                raise ValueError(self.t('errors.setting'))
            d['values']['execution_source'] = arg
            d['dirty'] = True
            self.queue(chat, user, s)
        elif op == 'cpupolicy':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L937:then')
            if arg not in ('auto', 'manual'): _diagnostics.step('ticket_chat.TicketChat.action:L938:then') if _diagnostics.enabled else None; raise ValueError(self.t('errors.cpu_policy'))
            d = self.draft(s)
            d['values']['macro_cpu_policy'] = arg
            if arg == 'auto':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L941:then')
                d['values']['macro_cross_socket'] = True
            d['dirty'] = True
            self.queue(chat, user, s)
        elif op == 'toggle':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L945:then')
            if arg not in ('openfoam_defaults', 'macro_cross_socket', 'monitoring_cpu', 'dynamic_cores'): _diagnostics.step('ticket_chat.TicketChat.action:L946:then') if _diagnostics.enabled else None; raise ValueError(self.t('errors.setting'))
            d = self.draft(s)
            if arg == 'macro_cross_socket' and d['values']['macro_cpu_policy'] == 'auto':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L948:then')
                raise ValueError(self.t('errors.auto_cross_socket'))
            d['values'][arg] = not d['values'][arg]
            d['dirty'] = True
            (self.rules if arg == 'openfoam_defaults' else self.queue)(chat, user, s)
        elif op == 'browse': _diagnostics.step('ticket_chat.TicketChat.action:L953:then') if _diagnostics.enabled else None; self.browse_start(chat, user, s, arg)
        elif op == 'bp': _diagnostics.step('ticket_chat.TicketChat.action:L954:then') if _diagnostics.enabled else None; self.browser(chat, user, s, arg)
        elif op == 'bd':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L955:then')
            self.set_browser_path(s, s['browser']['options'][int(arg)])
            self.browser(chat, user, s)
        elif op == 'bup':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L958:then')
            self.set_browser_path(s, str(Path(s['browser']['path']).parent))
            self.browser(chat, user, s)
        elif op == 'bf':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L961:then')
            b = s['browser']
            path = Path(b['options'][int(arg)]).resolve()
            relative = str(path.relative_to(b['root']))
            if not path.is_file(): _diagnostics.step('ticket_chat.TicketChat.action:L965:then') if _diagnostics.enabled else None; raise ValueError(self.t('errors.file_again'))
            if b['kind'] == 'residual':
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L966:then')
                self.apply_field(s, 'residual_pattern', relative)
                s.pop('browser', None)
                self.basic(chat, user, s)
            else:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L966:else')
                if relative in b['selected']: _diagnostics.step('ticket_chat.TicketChat.action:L971:then') if _diagnostics.enabled else None; b['selected'].remove(relative)
                else: _diagnostics.step('ticket_chat.TicketChat.action:L971:else') if _diagnostics.enabled else None; b['selected'].append(relative)
                self.browser(chat, user, s, b['page'])
        elif op == 'bapply':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L974:then')
            b = s['browser']
            self.apply_field(s, 'case_dir' if b['kind'] == 'case' else 'logs',
                             b['path'] if b['kind'] == 'case' else '\n'.join(b['selected']))
            s.pop('browser', None)
            self.basic(chat, user, s)
        elif op == 'bcancel':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L980:then')
            s.pop('browser', None)
            self.basic(chat, user, s)
        elif op == 'exports': _diagnostics.step('ticket_chat.TicketChat.action:L983:then') if _diagnostics.enabled else None; self.exports(chat, user, s, arg or 0)
        elif op in ('xe', 'xnew'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L984:then')
            index = int(arg) if op == 'xe' else None
            s['export_index'] = index
            s['export_edit'] = (deepcopy(self.draft(s)['values']['exports'][index]) if index is not None
                                else dict(name='', pattern='', kind='document', max_files=1))
            self.export_card(chat, user, s)
        elif op == 'xkind':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L990:then')
            item = s['export_edit']
            item['kind'] = 'document' if item.get('kind') == 'photo' else 'photo'
            self.export_card(chat, user, s)
        elif op in ('xapply', 'xdelete'):
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L994:then')
            d = self.draft(s)
            items = d['values']['exports']
            index = s['export_index']
            if op == 'xdelete': _diagnostics.step('ticket_chat.TicketChat.action:L998:then') if _diagnostics.enabled else None; items.pop(index)
            else:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L998:else')
                root = (self.service.folder / Path(d['values']['case_dir']).expanduser()).resolve()
                item = validate_export(s['export_edit'], items, root, index)
                if index is None: _diagnostics.step('ticket_chat.TicketChat.action:L1002:then') if _diagnostics.enabled else None; items.append(item)
                else: _diagnostics.step('ticket_chat.TicketChat.action:L1002:else') if _diagnostics.enabled else None; items[index] = item
            d['dirty'] = True
            self.exports(chat, user, s)
        elif op == 'templates': _diagnostics.step('ticket_chat.TicketChat.action:L1006:then') if _diagnostics.enabled else None; self.templates(chat, user, s, arg or 0)
        elif op == 'template':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1007:then')
            rules = self.library.load()[s['templates'][int(arg)]]
            d = self.draft(s)
            d['values'].update(failure_patterns='\n'.join(rules['failure_patterns']), openfoam_defaults=rules['openfoam_defaults'])
            d['dirty'] = True
            self.rules(chat, user, s)
        elif op == 'savetemplate': _diagnostics.step('ticket_chat.TicketChat.action:L1013:then') if _diagnostics.enabled else None; self.save_template(chat, user, s)
        elif op == 'scan': _diagnostics.step('ticket_chat.TicketChat.action:L1014:then') if _diagnostics.enabled else None; self.scan(chat, user, s)
        elif op == 'stopscan':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1015:then')
            s.pop('scan_id', None)
            self.queue(chat, user, s)
        elif op == 'members': _diagnostics.step('ticket_chat.TicketChat.action:L1018:then') if _diagnostics.enabled else None; self.members(chat, user, s, arg or 0)
        elif op == 'membercore':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1019:then')
            index = int(arg)
            s['pending'] = f'case_core:{index}'
            row = self.draft(s)['values']['cases'][index]
            self.render(chat, user, s, self.t('members.core_prompt',
                        name=Path(row['case_dir']).name,
                        cores=row.get('cores') or self.draft(s)['values'].get('macro_cores') or 1),
                        [[(self.t('input.cancel'), 'members', index // PAGE)]], 'member_core')
        elif op == 'remove':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1027:then')
            d = self.draft(s)
            d['values']['cases'].pop(int(arg))
            d['dirty'] = True
            self.members(chat, user, s, int(arg) // PAGE)
        elif op == 'duplicate':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1032:then')
            self.switch(chat, user, s, 'duplicate', self.draft(s)['current'])
        elif op == 'validate':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1034:then')
            d = self.draft(s)
            self.service.validate(d['values'], d['filename'], d['current'])
            self.card(chat, user, s, self.t('notices.validated'))
        elif op == 'review': _diagnostics.step('ticket_chat.TicketChat.action:L1038:then') if _diagnostics.enabled else None; self.review(chat, user, s, arg)
        elif op == 'save':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1039:then')
            d = self.draft(s)
            action = s['save_action']
            if action['mode'] in ('submit', 'run', 'queue') and d['current']:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1042:then')
                state = self.runner.state(d['current'], fresh=True)
                request_mode = 'queue' if action['mode'] == 'submit' else action['mode']
                allowed = state['run_enabled'] if request_mode == 'run' else state['queue_enabled']
                if not allowed:
                    if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1046:then')
                    raise ValueError(state.get('availability_message',
                                                self.bot.ui.text('scenarios.launch.already_running')))
            name, data = self.service.save(d['values'], d['filename'], d['current'],
                    submit=False, overwrite=action['overwrite'],
                    expected_revision=d['revision'], request_id=action['request_id'])
            s['draft'] = self.service.open(name)
            if action['mode'] in ('submit', 'run', 'queue'):
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1053:then')
                request_mode = 'queue' if action['mode'] == 'submit' else action['mode']
                result = self.runner.request(name, expected_revision=s['draft']['revision'],
                                             request_id=action['request_id'], mode=request_mode,
                                             lane=action.get('lane', 1))
                data = self.service.open(name)['values']['_source']
            s.pop('save_action', None)
            if action['mode'] in ('submit', 'run', 'queue'):
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1060:then')
                notice = self.t('notices.saved_queued' if result['already_queued'] else
                                'notices.saved_and_run' if request_mode == 'run' else
                                'notices.saved_and_queued')
            else:
                if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1060:else')
                notice = self.t('notices.saved') + (self.t('notices.awaiting_queue')
                                                     if data.get('queue', {}).get('submit') else '')
            self.card(chat, user, s, notice)
        elif op == 'delete':
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1068:then')
            d = self.draft(s)
            self.delete_review(chat, user, s, [d['current']])
        else:
            if _diagnostics.enabled: _diagnostics.step('ticket_chat.TicketChat.action:L1068:else')
            raise ValueError(self.t('errors.unknown_button'))
