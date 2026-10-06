from copy import deepcopy
import json
import hashlib
from pathlib import Path
import threading
from unittest.mock import patch

from cfd_bot.bot import Bot, case_id
from cfd_bot.config import load_case, read_json
from cfd_bot.editor import TicketService, form_document, form_values
from cfd_bot.patterns import PatternLibrary
from cfd_bot.storage import Store
from cfd_bot.tickets import accept_submissions, sync_ticket_states
from tests.test_core import Environment, FakeAPI


class PanelsAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.panels = {}

    def send(self, chat, text, keyboard=None):
        result = super().send(chat, text, keyboard)
        self.panels[chat, result[-1]['message_id']] = dict(text=text, reply_markup=keyboard)
        return result

    def call(self, method, payload):
        super().call(method, payload)
        if method == 'editMessageText':
            self.panels[payload['chat_id'], payload['message_id']] = deepcopy(payload)
            return {'message_id': payload['message_id']}


class TicketServiceTests(Environment):
    def setUp(self):
        super().setUp()
        self.service = TicketService(self.root / 'tickets')
        self.name = 'alone-example.json'
        self.service.path(self.name).write_text(json.dumps(dict(self.case_data, case_dir=str(self.case_root))))

    def test_state_updates_do_not_conflict_but_other_editor_changes_do(self):
        draft = self.service.open(self.name)
        on_disk = read_json(self.service.path(self.name))
        on_disk['queue'] = {'state': 'running', 'job_id': 'example'}
        self.service.path(self.name).write_text(json.dumps(on_disk))
        draft['values']['name'] = 'edited'
        self.service.save(draft['values'], self.name, self.name, expected_revision=draft['revision'])
        saved = read_json(self.service.path(self.name))
        self.assertEqual(saved['queue']['state'], 'running')
        self.assertEqual(saved['name'], 'edited')
        with self.assertRaisesRegex(ValueError, '다른 편집기'):
            self.service.save(draft['values'], self.name, self.name, expected_revision=draft['revision'])

    def test_running_single_ticket_request_data_remains_editable(self):
        document = read_json(self.service.path(self.name))
        document['queue'] = dict(state='running', job_id='active-job', request_id='original-request')
        self.service.path(self.name).write_text(json.dumps(document))
        draft = self.service.open(self.name)
        draft['values']['exports'] = [dict(name='plot', pattern='plots/live.png', kind='photo', max_files=1)]
        self.service.save(draft['values'], self.name, self.name, expected_revision=draft['revision'])
        saved = read_json(self.service.path(self.name))
        self.assertEqual(saved['queue'], document['queue'])
        self.assertEqual(saved['command'], document['command'])
        self.assertEqual(saved['exports'][0]['pattern'], 'plots/live.png')

    def test_running_single_ticket_rejects_changed_scripts(self):
        document = read_json(self.service.path(self.name))
        document['queue'] = dict(state='running', job_id='active-job')
        self.service.path(self.name).write_text(json.dumps(document))
        before = self.service.path(self.name).read_bytes()
        for stage, command in [('preprocess', './Allclean'), ('postprocess', './Allpost')]:
            with self.subTest(stage=stage):
                draft = self.service.open(self.name)
                draft['values'][stage] = command
                with self.assertRaisesRegex(ValueError, '전·후처리 실행 명령'):
                    self.service.save(draft['values'], self.name, self.name)
                self.assertEqual(self.service.path(self.name).read_bytes(), before)

    def test_running_single_ticket_rejects_execution_changes(self):
        document = read_json(self.service.path(self.name))
        document.update(resource_source='ticket', cpu_policy='auto', cores=2, command=['./Allrun'])
        document.pop('cpu_set', None)
        document['queue'] = dict(state='running', job_id='active-job')
        self.service.path(self.name).write_text(json.dumps(document))
        before = self.service.path(self.name).read_bytes()
        for change in (dict(macro_cores='3'), dict(macro_command='./OtherRun'),
                       dict(execution_source='case'), dict(macro_cpu_policy='manual', macro_cpu_set='0-1')):
            with self.subTest(change=change):
                draft = self.service.open(self.name)
                draft['values'].update(change)
                with self.assertRaisesRegex(ValueError, '계산 중에는 코어'):
                    self.service.save(draft['values'], self.name, self.name)
                self.assertEqual(self.service.path(self.name).read_bytes(), before)
        draft = self.service.open(self.name)
        draft['values'].update(monitoring_cpu=True, monitoring_command='./Allmonitor')
        with self.assertRaisesRegex(ValueError, '모니터링 실행 설정'):
            self.service.save(draft['values'], self.name, self.name)
        self.assertEqual(self.service.path(self.name).read_bytes(), before)

    def test_submission_retry_keeps_request_id_and_source_files(self):
        draft = self.service.open(self.name)
        first, _ = self.service.save(draft['values'], self.name, self.name,
                                    submit=True, request_id='one-action', expected_revision=draft['revision'])
        second, document = self.service.save(draft['values'], self.name, self.name,
                                             submit=True, request_id='one-action', expected_revision=draft['revision'])
        self.assertEqual(first, second)
        self.assertEqual(document['queue']['request_id'], 'one-action')
        with self.assertRaisesRegex(ValueError, '대기 취소'):
            self.service.delete(self.name)

    def test_duplicate_uses_same_form_settings_and_preserves_original(self):
        before = self.service.path(self.name).read_bytes()
        draft = self.service.duplicate(self.name)
        root = self.root / 'clone'
        root.mkdir()
        draft['values']['case_dir'] = str(root)
        filename, _ = self.service.save(draft['values'], draft['filename'])
        self.assertTrue(filename.startswith('alone-'))
        self.assertEqual(self.service.path(self.name).read_bytes(), before)
        clone = load_case(self.service.path(filename))
        self.assertEqual(clone['_root'], str(root))
        self.assertEqual(clone['command'], self.case_data['command'])

    def test_bulk_delete_checks_every_ticket_before_removing_anything(self):
        other = 'alone-other.json'
        self.service.path(other).write_bytes(self.service.path(self.name).read_bytes())
        plan = self.service.deletion_preview([self.name, other])
        changed = read_json(self.service.path(other))
        changed['queue'] = dict(state='running', job_id='busy')
        self.service.path(other).write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, '대기 취소'):
            self.service.delete_many(plan['names'], plan['revisions'])
        self.assertTrue(self.service.path(self.name).exists())
        self.assertTrue(self.service.path(other).exists())

    def macro_documents(self):
        name, child = 'macro-batch.json', 'child-example.json'
        data = read_json(self.service.path(self.name))
        data.update(role='child', macro_ticket=name, queue={'state': 'finished'})
        self.service.path(child).write_text(json.dumps(data))
        data.update(task_type='macro', role='alone',
                    cases=[dict(case_dir=str(self.case_root), ticket=child, state='finished')])
        data.pop('macro_ticket')
        self.service.path(name).write_text(json.dumps(data))
        return name, child

    def test_bulk_delete_expands_macros_deduplicates_children_and_keeps_case_data(self):
        macro, child = self.macro_documents()
        with self.assertRaisesRegex(ValueError, '매크로 티켓도 선택'):
            self.service.delete_many([self.name, child])
        self.assertTrue(self.service.path(self.name).exists())
        plan = self.service.deletion_preview([child, macro, self.name, child])
        self.assertEqual(set(plan['names']), {child, macro, self.name})
        self.assertEqual(set(self.service.delete_many(plan['names'], plan['revisions'])), set(plan['names']))
        self.assertEqual(self.service.listing(), [])
        self.assertTrue(self.control.exists())

    def test_bulk_delete_detects_changed_child_after_confirmation_preview(self):
        macro, child = self.macro_documents()
        plan = self.service.deletion_preview([macro])
        data = read_json(self.service.path(child))
        data['name'] = 'changed in GUI'
        self.service.path(child).write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, '다른 편집기'):
            self.service.delete_many(plan['names'], plan['revisions'])
        self.assertTrue(self.service.path(macro).exists())
        self.assertTrue(self.service.path(child).exists())

    def test_finished_macro_does_not_allow_deleting_a_running_child(self):
        macro, child = self.macro_documents()
        data = read_json(self.service.path(child))
        data['queue'] = dict(state='running', job_id='still-running')
        self.service.path(child).write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, '대기 취소'):
            self.service.delete_many([self.name, macro])
        self.assertEqual(len(self.service.listing()), 3)

    def test_bulk_delete_restores_removed_files_on_io_failure(self):
        other = 'alone-other.json'
        before = self.service.path(self.name).read_bytes()
        self.service.path(other).write_bytes(before)
        unlink = Path.unlink
        def fail_second(path, *args, **kwargs):
            if path == self.service.path(other):
                raise OSError('disk error')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_second):
            with self.assertRaisesRegex(OSError, 'disk error'):
                self.service.delete_many([self.name, other])
        self.assertEqual(self.service.path(self.name).read_bytes(), before)
        self.assertEqual(self.service.path(other).read_bytes(), before)


class TicketChatTests(Environment):
    def setUp(self):
        super().setUp()
        self.service = TicketService(self.root / 'tickets')
        self.config['cases'] = []
        self.config['case_globs'] = [str(self.service.folder / '*.json')]
        self.config['telegram']['allowed_user_ids'].append(11)
        self.api = PanelsAPI()
        self.bot = Bot(self.config, self.store, self.api)
        self.update = 1000
        scanner = patch('cfd_bot.ticket_run.snapshot', return_value={'cases': {}})
        self.run_snapshot = scanner.start()
        self.addCleanup(scanner.stop)

    def message(self, text, user=10):
        self.update += 1
        self.bot.handle(dict(update_id=self.update, message=dict(
            message_id=self.update, text=text, chat={'id': 20}, **{'from': {'id': user}})))

    def session(self, user=10):
        return self.bot.ticket_ui.load(20, user)

    def panel(self, user=10):
        return self.api.panels[20, self.session(user)['panel_id']]

    def callback(self, data, user=10):
        self.update += 1
        self.bot.handle(dict(update_id=self.update, callback_query=dict(
            id=str(self.update), data=data, message={'chat': {'id': 20}}, **{'from': {'id': user}})))

    def button(self, op, arg=None, user=10):
        for row in self.panel(user)['reply_markup']['inline_keyboard']:
            for button in row:
                parts = button['callback_data'].split(':', 3)
                if parts[2] == op and (arg is None or (len(parts) > 3 and parts[3] == str(arg))):
                    return button['callback_data']
        self.fail(f'No {op}:{arg} button in {self.panel(user)}')

    def click(self, op, arg=None, user=10):
        self.callback(self.button(op, arg, user), user)

    def new(self, kind='single', user=10):
        self.message('/tickets', user)
        self.click('new', kind, user)

    def field(self, key, text, user=10):
        self.click('field', key, user)
        self.message(text, user)

    def source(self, name='alone-source.json'):
        self.service.path(name).write_text(json.dumps(dict(self.case_data, case_dir=str(self.case_root))))
        return name

    def test_single_form_file_selection_and_shared_save(self):
        self.new()
        self.click('basic')
        self.field('case_dir', str(self.case_root))
        self.assertEqual(self.session()['draft']['values']['case_dir'], str(self.case_root))
        self.assertIn('controlDict 기본값', self.panel()['text'])
        (self.case_root / 'log.solver').write_text('log')
        (self.case_root / 'log.second').write_text('log')
        (self.case_root / 'escaped.log').symlink_to(self.root / 'outside')
        self.click('basic')
        self.click('browse', 'logs')
        options = self.session()['browser']['options']
        self.assertFalse(any(Path(p).name == 'escaped.log' for p in options))
        for name in ('log.solver', 'log.second'):
            options = self.session()['browser']['options']
            self.click('bf', options.index(str(self.case_root / name)))
        self.click('bapply')
        self.assertEqual(self.session()['draft']['values']['logs'], 'log.solver\nlog.second')
        self.field('residual_pattern', 'plots/residual*.png')
        self.click('review', 'save')
        self.click('save')
        draft = self.session()['draft']
        saved = read_json(self.service.path(draft['current']))
        self.assertEqual(saved['watcher']['logs'], ['log.solver', 'log.second'])
        self.assertEqual(saved['residual_pattern'], 'plots/residual*.png')
        self.assertFalse(saved['queue'].get('submit', False))
        self.assertFalse(draft['dirty'])
        self.assertEqual(form_document(draft['values'])['watcher'], saved['watcher'])

    def test_bad_input_can_be_corrected_and_cancel_preserves_draft(self):
        self.new()
        self.click('rules')
        self.field('failure_patterns', '[')
        self.assertEqual(self.session()['pending'], 'failure_patterns')
        self.assertEqual(self.session()['draft']['values']['failure_patterns'], '')
        self.message('ERROR:\nfailed stage:')
        self.assertEqual(self.session()['draft']['values']['failure_patterns'], 'ERROR:\nfailed stage:')
        self.click('basic')
        self.click('field', 'name')
        self.message('/cancel')
        self.assertNotIn('pending', self.session())
        self.assertEqual(self.session()['draft']['values']['name'], '')
        self.assertTrue(self.session()['draft']['dirty'])

    def test_entry_points_and_authorization(self):
        name = self.source()
        self.message('/start', user=99)
        self.assertIsNone(self.bot.ticket_ui)
        self.assertEqual(self.api.messages, [])
        self.message('/start')
        panel = list(self.api.panels.values())[-1]
        self.assertIn('tickets', [button['callback_data']
                                 for row in panel['reply_markup']['inline_keyboard'] for button in row])
        self.callback('tickets')
        self.assertEqual(self.session()['view'], 'list')
        key = hashlib.sha256(str(self.service.path(name)).encode()).hexdigest()[:16]
        self.callback('ticketopen:' + key)
        self.assertEqual(self.session()['draft']['current'], name)
        self.click('basic')
        self.click('field', 'name')
        self.callback('queue')
        self.assertNotIn('pending', self.session())
        self.assertEqual(self.session()['view'], 'away')

    def test_tickets_command_opens_a_fresh_panel_at_end_of_conversation(self):
        self.source()
        self.message('/tickets')
        previous = self.session()['panel_id']
        self.message('/start')
        sent = len(self.api.messages)
        self.message('/tickets')
        self.assertNotEqual(self.session()['panel_id'], previous)
        self.assertEqual(len(self.api.messages), sent + 1)
        self.assertIn('티켓 목록', self.api.messages[-1][1])
        self.click('open', 0)
        self.assertEqual(self.session()['view'], 'card')

    def test_external_ticket_buttons_open_a_fresh_panel(self):
        name = self.source()
        self.message('/tickets')
        previous = self.session()['panel_id']
        self.callback('tickets')
        self.assertNotEqual(self.session()['panel_id'], previous)
        previous = self.session()['panel_id']
        key = hashlib.sha256(str(self.service.path(name)).encode()).hexdigest()[:16]
        self.callback('ticketopen:' + key)
        self.assertNotEqual(self.session()['panel_id'], previous)
        self.assertEqual(self.session()['draft']['current'], name)

    def test_clean_forgets_all_chat_panels_and_pending_inputs_but_keeps_drafts(self):
        self.new(user=10)
        self.click('basic', user=10)
        self.field('name', 'unsaved case', user=10)
        self.new(user=11)
        self.click('basic', user=11)
        self.click('field', 'name', user=11)
        old_panel = self.session(10)['panel_id']
        stale = self.button('mode', user=10)
        self.message('/clean', user=10)
        for user in (10, 11):
            self.assertNotIn('panel_id', self.session(user))
            self.assertNotIn('token', self.session(user))
            self.assertNotIn('pending', self.session(user))
        self.assertEqual(self.session(10)['draft']['values']['name'], 'unsaved case')
        self.assertTrue(self.session(10)['draft']['dirty'])
        self.callback(stale)
        self.assertEqual(self.session(10)['draft']['values']['task_type'], 'single')
        self.message('/tickets', user=10)
        self.assertNotEqual(self.session(10)['panel_id'], old_panel)
        self.click('card')
        self.assertEqual(self.session(10)['draft']['values']['name'], 'unsaved case')

    def test_folder_and_png_selection_and_cancelled_log_picker(self):
        self.new()
        self.click('basic')
        self.click('browse', 'case')
        self.field('browse_path', str(self.case_root))
        self.click('bapply')
        self.assertEqual(self.session()['draft']['values']['case_dir'], str(self.case_root))
        plot = self.case_root / 'plots' / 'residual.png'
        plot.parent.mkdir()
        plot.write_bytes(b'image')
        self.click('browse', 'residual')
        self.click('bd', self.session()['browser']['options'].index(str(plot.parent)))
        self.click('bf', self.session()['browser']['options'].index(str(plot)))
        self.assertEqual(self.session()['draft']['values']['residual_pattern'], 'plots/residual.png')
        self.click('browse', 'logs')
        self.click('bcancel')
        self.assertEqual(self.session()['draft']['values']['logs'], 'log.solver')

    def test_gui_edit_after_save_preview_is_not_overwritten(self):
        name = self.source()
        self.message('/tickets')
        self.click('open', 0)
        self.click('basic')
        self.field('name', 'Telegram change')
        self.click('review', 'save')
        native = self.service.open(name)
        native['values']['name'] = 'GUI change'
        self.service.save(native['values'], name, name, expected_revision=native['revision'])
        self.click('save')
        self.assertEqual(read_json(self.service.path(name))['name'], 'GUI change')
        self.assertTrue(self.session()['draft']['dirty'])
        self.assertTrue(any('다른 편집기' in message[1] for message in self.api.messages))

    def test_request_data_and_pattern_templates_share_gui_library(self):
        self.new()
        self.click('basic')
        self.field('case_dir', str(self.case_root))
        self.click('exports')
        self.click('xnew')
        for key, value in [('x.name', 'plot'), ('x.pattern', 'plots/*.png'), ('x.max_files', '2')]:
            self.field(key, value)
        self.click('xkind')
        self.click('xapply')
        item = self.session()['draft']['values']['exports'][0]
        self.assertEqual(item, dict(name='plot', pattern='plots/*.png', kind='photo', max_files=2,
                                    on=[], on_complete=False))
        self.click('card')
        self.click('rules')
        self.field('failure_patterns', 'CASE FAILED')
        self.click('rules')
        self.field('template_name', 'pipeline')
        library = PatternLibrary(self.root / 'ticket-patterns.json')
        self.assertEqual(library.load()['pipeline']['failure_patterns'], ['CASE FAILED'])
        self.click('templates')
        names = self.session()['templates']
        self.click('template', names.index('OpenFOAM 기본'))
        self.assertEqual(self.session()['draft']['values']['failure_patterns'], '')

    def test_scripts_defaults_edit_clear_and_macro_child_publication(self):
        self.new('macro')
        self.click('basic')
        self.field('case_dir', str(self.root))
        self.click('queue')
        self.field('macro_cores', '1')
        self.click('queue')
        self.click('toggle', 'monitoring_cpu')
        self.click('card')
        self.click('scripts')
        self.assertIn('./Allclean', self.panel()['text'])
        self.assertIn('./Allpost', self.panel()['text'])
        self.field('preprocess', './prepare --clean')
        self.click('scripts')
        self.click('field', 'postprocess')
        self.click('clear', 'postprocess')
        self.assertEqual(self.session()['draft']['values']['postprocess'], '')
        self.click('scripts')
        self.click('scriptdefault', 'postprocess')
        session = self.session()
        session['draft']['values']['cases'] = [dict(case_dir=str(self.case_root), state='waiting')]
        self.bot.ticket_ui.persist(20, 10, session)
        self.click('card')
        self.click('review', 'save')
        self.click('save')
        self.assertFalse(self.session()['draft']['dirty'])
        macro = read_json(self.service.path(self.session()['draft']['current']))
        child = read_json(self.service.path(macro['cases'][0]['ticket']))
        self.assertEqual(child['preprocess'], [{'command': ['./prepare', '--clean']}])
        self.assertEqual(child['postprocess'], [{'command': ['./Allpost']}])
        self.assertEqual(child['monitoring'], {
            'allocate_cpu': True, 'command': ['./Allmonitor']})

    def test_new_case_browser_starts_at_openfoam_run(self):
        run = self.root / 'OpenFOAM/joon-dev/run'
        run.mkdir(parents=True)
        self.new()
        self.click('basic')
        with patch('cfd_bot.editor.DEFAULT_CASE_ROOT', run):
            self.click('browse', 'case')
        self.assertEqual(self.session()['browser']['path'], str(run))

    def test_running_macro_export_edit_is_saved_and_next_request_uses_latest_path(self):
        draft = self.service.new('macro')
        draft['values'].update(case_dir=str(self.root), name='batch',
                               cases=[dict(case_dir=str(self.case_root), state='waiting')],
                               macro_cores='1', macro_cpu_set=self.cpu, macro_command='./Allrun',
                               exports=[dict(name='live-naoh', pattern='validation/live/steady_naoh_coupled.png',
                                             kind='photo', max_files=1)])
        macro_name, macro = self.service.save(draft['values'], 'macro-batch.json', submit=True)
        accept_submissions(self.config, self.store)
        job = self.store.jobs()[0]
        self.store.update_job(job['id'], status='running')
        self.run_snapshot.return_value = {'cases': {str(self.case_root): {}}}
        sync_ticket_states(self.config, self.store, self.run_snapshot.return_value)
        jobs = self.store.jobs()
        before = read_json(self.service.path(macro_name))
        image = self.case_root / 'validation/live/steady_coupled_naoh.png'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'fixture: no real Telegram transfer')
        # An already-open /data button must pick up the new path too.
        action = 'export:' + case_id(load_case(self.service.path(macro['cases'][0]['ticket']))) + ':live-naoh'
        self.bot.dispatch(20, action, 'before-edit')
        self.assertEqual(self.api.files, [])
        self.message('/tickets')
        self.click('open', self.session()['choices'].index(macro_name))
        self.click('exports')
        self.click('xe', 0)
        self.field('x.pattern', 'validation/live/steady_coupled_naoh.png')
        self.click('xapply')
        self.click('card')
        self.click('review', 'save')
        self.click('save')
        self.assertFalse(self.session()['draft']['dirty'])
        after = read_json(self.service.path(macro_name))
        self.assertEqual(after['queue'], before['queue'])
        self.assertEqual(after['cases'], before['cases'])
        self.bot.dispatch(20, action, 'after-edit')
        self.assertEqual(self.api.files[-1][1]['path'], str(image))
        self.assertEqual(self.api.files[-1][1]['kind'], 'photo')
        accept_submissions(self.config, self.store)
        self.assertEqual(self.store.jobs(), jobs)

    def test_clone_delete_and_stale_buttons_never_modify_original(self):
        name = self.source()
        original = self.service.path(name).read_bytes()
        self.message('/tickets')
        self.click('open', 0)
        stale = self.button('delete')
        self.click('duplicate')
        self.assertIsNone(self.session()['draft']['current'])
        self.callback(stale)
        self.assertEqual(self.service.path(name).read_bytes(), original)
        self.click('list')
        self.click('open', 0)
        self.click('switch')
        self.click('delete')
        self.assertTrue(self.service.path(name).exists())
        self.click('deleteyes')
        self.assertFalse(self.service.path(name).exists())

    def test_sessions_and_buttons_are_scoped_to_user_and_survive_restart(self):
        self.new(user=10)
        self.click('basic', user=10)
        self.click('field', 'name', user=10)
        self.new(user=11)
        other = self.button('mode', user=11)
        self.callback(other, user=10)
        self.assertEqual(self.session(10)['pending'], 'name')
        self.bot = Bot(self.config, Store(self.store.root), self.api)
        self.message('restored draft', user=10)
        self.assertEqual(self.session(10)['draft']['values']['name'], 'restored draft')
        self.assertEqual(self.session(11)['draft']['values']['name'], '')

    def test_save_review_and_replayed_callback_enqueue_only_once(self):
        self.source()
        self.message('/tickets')
        self.click('open', 0)
        self.click('basic')
        self.field('name', 'queued once')
        self.click('queuelanes')
        self.click('runreview', 'queue:2')
        self.assertEqual(self.store.jobs(), [])
        confirm = self.button('save')
        self.click('save')
        accept_submissions(self.config, self.store)
        jobs = self.store.jobs()
        self.assertEqual(len(jobs), 1)
        self.bot = Bot(self.config, Store(self.store.root), self.api)
        self.callback(confirm)
        accept_submissions(self.config, self.store)
        self.assertEqual([j['id'] for j in self.store.jobs()], [jobs[0]['id']])

    def test_macro_scan_row_removal_common_resources_and_submission(self):
        parent = self.root / 'batch'
        roots = [parent / name for name in ('a', 'b', 'skip-template')]
        for root in roots:
            (root / 'system').mkdir(parents=True)
            (root / 'system/controlDict').write_text('endTime 10;')
            (root / 'Allrun').write_text('#!/bin/sh\n')
        (roots[0] / '10').mkdir()
        (roots[0] / 'postProcessing/sample/10').mkdir(parents=True)
        self.new('macro')
        self.click('basic')
        self.field('case_dir', str(parent))
        for key, value in [('macro_cores', '1'), ('macro_command', './Allrun')]:
            self.click('queue')
            self.field(key, value)
        self.click('queue')
        self.field('case_include_patterns', 'a\nb')
        self.click('queue')
        self.field('case_exclude_patterns', 'skip*')
        self.click('queue')
        self.assertIn('포함 패턴: a\nb', self.panel()['text'])
        with patch('cfd_bot.ticket_chat.snapshot', return_value={'cases': {}}):
            self.click('scan')
            for thread in self.bot.ticket_ui.workers:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.session()['draft']['values']['cases']), 2)
        self.assertIn('종료값 도달', self.panel()['text'])
        member_buttons = [row[0]['text'] for row in self.panel()['reply_markup']['inline_keyboard']
                          if any(':remove:' in b['callback_data'] for b in row)]
        self.assertTrue(member_buttons[0].startswith('🟨 '))
        self.assertFalse(member_buttons[1].startswith('🟨 '))
        stale_remove = self.button('remove', 0)
        self.click('remove', 1)
        self.callback(stale_remove)
        self.assertEqual(len(self.session()['draft']['values']['cases']), 1)
        self.click('queue')
        self.click('card')
        self.click('review', 'save')
        self.click('save')
        draft = self.session()['draft']
        macro = read_json(self.service.path(draft['current']))
        self.assertEqual(len(macro['cases']), 1)
        self.assertEqual(macro['discovery'], {
            'include_patterns': ['a', 'b'], 'exclude_patterns': ['skip*']})
        child = load_case(self.service.path(macro['cases'][0]['ticket']))
        self.assertNotIn('discovery', child)
        self.assertEqual(child['cpu_policy'], 'auto')
        self.assertNotIn('cpu_set', child)
        self.assertEqual(child['resource_source'], 'macro')
        self.assertEqual(child['macro_ticket'], draft['current'])
        accept_submissions(self.config, self.store)
        self.assertEqual(self.store.jobs(), [])
        self.click('queuelanes')
        self.click('runreview', 'queue:3')
        self.click('runyes')
        accept_submissions(self.config, self.store)
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.jobs()[0]['case_root'], str(roots[0]))
        self.assertEqual(self.store.jobs()[0]['queue_lane'], 3)

    def test_child_execution_panel_displays_inherited_values_without_edit_buttons(self):
        name = self.source()
        document = read_json(self.service.path(name))
        document.update(role='child', macro_ticket='macro-batch.json', resource_source='macro',
                        cores=4, cpu_policy='auto')
        document.pop('cpu_set', None)
        self.service.path(name).write_text(json.dumps(document))
        self.message('/tickets')
        self.click('open', self.session()['choices'].index(name))
        self.click('queue')
        self.assertIn('매크로 실행 설정 상속', self.panel()['text'])
        self.assertIn('NP: 4', self.panel()['text'])
        callbacks = [b['callback_data'] for row in self.panel()['reply_markup']['inline_keyboard'] for b in row]
        self.assertFalse(any(':field:macro_cores' in c or ':execsource:' in c for c in callbacks))

    def test_single_execution_buttons_save_and_queue_explicit_resources(self):
        self.new()
        self.click('basic')
        self.field('case_dir', str(self.case_root))
        self.click('queue')
        self.assertIn('케이스 설정 사용', self.panel()['text'])
        self.click('execsource', 'ticket')
        self.field('macro_cores', '3')
        self.click('queue')
        self.field('macro_command', './CustomRun')
        self.click('review', 'save')
        self.click('save')
        filename = self.session()['draft']['current']
        saved = load_case(self.service.path(filename))
        self.assertEqual(saved['resource_source'], 'ticket')
        self.assertEqual(saved['cores'], 3)
        self.assertEqual(saved['command'], ['./CustomRun'])
        self.assertEqual(saved['cpu_policy'], 'auto')
        self.click('queue')
        self.assertIn('NP: 3', self.panel()['text'])
        self.click('card')
        # Only enqueue, no scheduler or solver is started.
        self.bot.ticket_ui.runner.request(filename)
        accept_submissions(self.config, self.store)
        self.assertEqual(self.store.jobs()[0]['case']['cores'], 3)
        self.assertEqual(self.store.jobs()[0]['case']['command'], ['./CustomRun'])

    def test_monitoring_cpu_toggle_enables_script_and_saves_shared_schema(self):
        self.new()
        self.click('basic')
        self.field('case_dir', str(self.case_root))
        self.click('queue')
        self.assertIn('모니터링 별도 코어: 비활성화', self.panel()['text'])
        self.assertNotIn('./Allmonitor', self.panel()['text'])
        self.click('toggle', 'monitoring_cpu')
        self.assertIn('모니터링 별도 코어: 활성화', self.panel()['text'])
        self.assertIn('./Allmonitor', self.panel()['text'])
        self.field('monitoring_command', './Allmonitor --interval 5')
        self.click('review', 'save')
        self.assertIn('모니터링: ./Allmonitor --interval 5', self.panel()['text'])
        self.click('save')
        saved = read_json(self.service.path(self.session()['draft']['current']))
        self.assertEqual(saved['monitoring'], {
            'allocate_cpu': True, 'command': ['./Allmonitor', '--interval', '5']})

    def test_manual_mapping_requires_advanced_mode_and_auto_clears_mapping(self):
        self.new('macro')
        self.click('basic')
        self.field('case_dir', str(self.root))
        self.click('queue')
        self.field('macro_cores', '1')
        self.click('queue')
        self.assertIn('자동 배정', self.panel()['text'])
        self.assertNotIn('macro_cpu_set', str(self.panel()['reply_markup']))
        self.click('cpupolicy', 'manual')
        self.field('macro_cpu_set', self.cpu)
        self.click('queue')
        self.click('toggle', 'macro_cross_socket')
        manual = form_document(self.session()['draft']['values'])
        self.assertEqual(manual['cpu_policy'], 'manual')
        self.assertEqual(manual['cpu_set'], self.cpu)
        self.assertFalse(manual['allow_cross_socket'])
        self.click('cpupolicy', 'auto')
        automatic = form_document(self.session()['draft']['values'])
        self.assertEqual(automatic['cpu_policy'], 'auto')
        self.assertNotIn('cpu_set', automatic)
        self.assertTrue(automatic['allow_cross_socket'])
        session = self.session()
        session['draft']['values']['cases'] = [dict(case_dir=str(self.case_root), state='waiting')]
        self.bot.ticket_ui.persist(20, 10, session)
        self.click('card')
        self.click('review', 'save')
        self.assertIn('실행 직전 자동 배정', self.panel()['text'])

    def test_cancelled_scan_cannot_replace_newer_draft(self):
        self.new('macro')
        self.click('basic')
        self.field('case_dir', str(self.case_root))
        self.click('queue')
        started, release = threading.Event(), threading.Event()
        def scan(command):
            started.set()
            release.wait(timeout=5)
            return {'cases': {}}
        with patch('cfd_bot.ticket_chat.snapshot', side_effect=scan):
            self.click('scan')
            self.assertTrue(started.wait(timeout=2))
            self.message('/cancel')
            release.set()
            for thread in self.bot.ticket_ui.workers:
                thread.join(timeout=5)
        self.assertEqual(self.session()['view'], 'card')
        self.assertNotIn('scan_id', self.session())

    def test_old_scan_is_recoverable_after_restart(self):
        self.new('macro')
        session = self.session()
        session.update(scan_id='abandoned', scan_owner='old-process')
        self.store.put(self.bot.ticket_ui.key(20, 10), session)
        self.bot = Bot(self.config, self.store, self.api)
        self.message('/tickets')
        self.assertNotIn('scan_id', self.session())

    def test_pagination_and_button_payload_lengths(self):
        for index in range(13):
            self.source(f'alone-example-{index:02}.json')
        self.message('/tickets')
        self.assertEqual(len(self.session()['choices']), 8)
        self.click('list', 1)
        self.assertEqual(len(self.session()['choices']), 5)
        for panel in self.api.panels.values():
            self.assertLessEqual(len(panel['text'].encode('utf-16-le')) // 2, 4096)
            for row in (panel.get('reply_markup') or {}).get('inline_keyboard', []):
                for button in row:
                    self.assertLessEqual(len(button['callback_data'].encode()), 64)

    def test_bulk_selection_survives_pagination_and_deletes_only_confirmed_files(self):
        for index in range(10):
            self.source(f'alone-{index:02}.json')
        self.message('/tickets')
        self.click('bulk')
        self.click('bulkpage', 1)
        self.click('ball')
        self.assertEqual(self.session()['bulk_selected'], [f'alone-{index:02}.json' for index in range(10)])
        self.click('bnone')
        self.assertEqual(self.session()['bulk_selected'], [])
        self.click('btoggle', 0)
        self.click('bulkpage', 0)
        self.click('btoggle', 0)
        self.assertEqual(self.session()['bulk_selected'], ['alone-08.json', 'alone-00.json'])
        self.click('breview')
        self.assertIn('총 2개', self.panel()['text'])
        self.assertEqual(len(self.service.listing()), 10)
        stale = self.button('bdelete')
        self.click('bdelete')
        self.assertEqual(self.service.listing(),
                         [f'alone-{index:02}.json' for index in range(1, 8)] + ['alone-09.json'])
        self.callback(stale)
        self.assertEqual(len(self.service.listing()), 8)

    def test_bulk_cancel_and_busy_ticket_keep_all_files(self):
        first, second = self.source('alone-a.json'), self.source('alone-b.json')
        self.message('/tickets')
        self.click('bulk')
        self.click('ball')
        self.click('breview')
        self.click('bulkpage')
        self.assertEqual(len(self.service.listing()), 2)
        self.click('breview')
        data = read_json(self.service.path(second))
        data['queue'] = dict(state='running', job_id='became-active')
        self.service.path(second).write_text(json.dumps(data))
        self.click('bdelete')
        self.assertEqual(self.service.listing(), [first, second])
        self.assertEqual(self.session()['view'], 'bulk_list')

    def test_run_button_disabled_for_external_run_and_enabled_after_finish(self):
        name = self.source()
        self.store.put('snapshot', {'cases': {str(self.case_root): {}}})
        self.message('/tickets')
        self.click('open', 0)
        callbacks = [button['callback_data'].split(':')[2]
                     for row in self.panel()['reply_markup']['inline_keyboard'] for button in row]
        self.assertNotIn('runreview', callbacks)
        self.assertIn('runstate', callbacks)
        self.assertTrue(any('계산중' in button['text']
                            for row in self.panel()['reply_markup']['inline_keyboard'] for button in row))
        self.click('runstate')
        self.click('runreview')
        self.click('runyes')
        accept_submissions(self.config, self.store)
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.session()['draft']['current'], name)

    def test_run_button_saves_changed_single_and_submits_once(self):
        name = self.source()
        self.message('/tickets')
        self.click('open', 0)
        self.click('basic')
        self.field('name', 'edited before run')
        before = self.service.path(name).read_bytes()
        self.click('runreview')
        self.assertEqual(self.session()['view'], 'save_confirm')
        self.assertIn('실행 큐', self.panel()['text'])
        self.assertEqual(self.service.path(name).read_bytes(), before)
        confirm = self.button('save')
        self.click('save')
        self.assertFalse(self.session()['draft']['dirty'])
        accept_submissions(self.config, self.store)
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.jobs()[0]['case']['name'], 'edited before run')
        self.callback(confirm)
        accept_submissions(self.config, self.store)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_run_button_saves_reselected_finished_macro_and_executes_new_members(self):
        draft = self.service.new('macro')
        draft['values'].update(case_dir=str(self.root), macro_cores='1', macro_cpu_set=self.cpu,
                               cases=[dict(case_dir=str(self.case_root), state='waiting')])
        name, _ = self.service.save(draft['values'], 'macro-batch.json')
        accept_submissions(self.config, self.store)
        for job in self.store.jobs():
            self.store.update_job(job['id'], status='succeeded', finished=job['created'] + 1)
        sync_ticket_states(self.config, self.store, {'cases': {}})
        second = self.root / 'second'
        second.mkdir()
        # Open the completed macro and select a different case, as with a rescan.
        self.message('/tickets')
        self.click('open', self.session()['choices'].index(name))
        s = self.session()
        s['draft']['values']['cases'] = [dict(case_dir=str(second), state='waiting')]
        s['draft']['dirty'] = True
        self.bot.ticket_ui.persist(20, 10, s)
        self.click('runreview')
        self.assertEqual(self.session()['view'], 'save_confirm')
        self.click('save')
        self.assertFalse(self.session()['draft']['dirty'])
        accept_submissions(self.config, self.store)
        self.assertEqual([j['case_root'] for j in self.store.jobs(('queued',))], [str(second)])
        self.assertEqual(read_json(self.service.path(name))['cases'][0]['case_dir'], str(second))

    def test_changed_draft_is_preserved_if_calculation_starts_before_save_and_run(self):
        name = self.source()
        self.message('/tickets')
        self.click('open', 0)
        self.click('basic')
        self.field('name', 'keep this draft')
        self.click('runreview')
        before = self.service.path(name).read_bytes()
        self.run_snapshot.return_value = {'cases': {str(self.case_root): {}}}
        self.click('save')
        self.assertEqual(self.service.path(name).read_bytes(), before)
        self.assertTrue(self.session()['draft']['dirty'])
        self.assertEqual(self.session()['draft']['values']['name'], 'keep this draft')
        self.assertEqual(self.store.jobs(), [])

    def test_running_between_execution_preview_and_confirm_is_rejected(self):
        name = self.source()
        before = self.service.path(name).read_bytes()
        self.message('/tickets')
        self.click('open', 0)
        self.click('runreview')
        self.run_snapshot.return_value = {'cases': {str(self.case_root): {}}}
        self.click('runyes')
        self.assertEqual(self.service.path(name).read_bytes(), before)
        self.assertEqual(self.store.jobs(), [])
        self.assertTrue(any('이미 계산 중' in message[1] for message in self.api.messages))
