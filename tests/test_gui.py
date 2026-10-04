"""Ticket form regressions; optional native-widget tests require a display."""
from copy import deepcopy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cfd_bot.config import load_bot, load_case
from cfd_bot.gui import TEMPLATE, TicketEditor, form_document, form_values, validate_document
from cfd_bot.editor import TicketService, case_browser_start
from cfd_bot.patterns import DEFAULT_NAME, DEFAULT_RULES, PatternLibrary
from cfd_bot.storage import Store


class FormTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tickets = self.root / 'tickets'
        self.tickets.mkdir()
        self.case = self.root / 'case with spaces'
        self.case.mkdir()
        self.fields = form_values(TEMPLATE, self.tickets)
        self.fields['case_dir'] = str(self.case)

    def validate(self, fields=None):
        return validate_document(json.dumps(form_document(fields or self.fields)), self.tickets)

    def loaded(self, data, name):
        path = self.tickets / name
        path.write_text(json.dumps(data), encoding='utf-8')
        case = load_case(path)
        case.pop('_config')
        # load_case retains the legacy single-log alias alongside canonical logs.
        case['watcher'].pop('log', None)
        return case

    def test_new_ticket_only_requires_case_directory(self):
        fields = form_values(TEMPLATE, self.tickets)
        self.assertEqual(fields['case_dir'], '')
        self.assertEqual(fields['name'], '')
        for key in ('cores', 'cpu_set', 'expected_seconds', 'start', 'end', 'command'):
            self.assertNotIn(key, fields)
        with self.assertRaisesRegex(ValueError, 'Case directory'):
            form_document(fields)
        data = self.validate()
        self.assertEqual(data['name'], 'case with spaces')
        for key in ('cores', 'cpu_set', 'command', 'expected_seconds', 'simulation'):
            self.assertNotIn(key, data)
        self.assertEqual(data['preprocess'], [{'command': ['./Allclean']}])
        self.assertEqual(data['postprocess'], [{'command': ['./Allpost']}])
        self.assertNotIn('progress', data['watcher'])
        self.assertEqual(self.loaded(data, 'new.json')['_root'], str(self.case))

    def test_single_execution_override_roundtrip_and_return_to_case_settings(self):
        from cfd_bot.execution import execution_case, apply_execution_settings
        settings = self.case / 'Allrun'
        original = '#!/bin/sh\nNP=99\nCPU_SET=0-98\n'
        settings.write_text(original)
        self.fields.update(execution_source='ticket', macro_cores='4', macro_command='./Allrun',
                           macro_cpu_policy='auto', macro_cpu_set='0-98')
        data = self.validate()
        self.assertEqual(data['resource_source'], 'ticket')
        self.assertNotIn('cpu_set', data)
        case = self.loaded(data, 'alone-example.json')
        execution = execution_case(case)
        self.assertEqual(execution['cores'], 4)
        self.assertNotIn('cpu_set', execution)
        execution['cpu_set'] = '4-7'  # Allocation would be supplied by Scheduler.
        apply_execution_settings(execution, self.root / 'job')
        self.assertEqual(settings.read_text(), '#!/bin/sh\nNP=4\nCPU_SET=4-7\n')
        self.assertEqual((self.root / 'job/case-settings-before/Allrun').read_text(), original)
        reopened = form_values(data, self.tickets)
        self.assertEqual(reopened['execution_source'], 'ticket')
        self.assertEqual(reopened['macro_cores'], '4')
        reopened['execution_source'] = 'case'
        inherited = self.validate(reopened)
        self.assertEqual(inherited['resource_source'], 'case')
        self.assertNotIn('cores', inherited)
        self.assertNotIn('command', inherited)

    def test_monitoring_cpu_and_script_roundtrip(self):
        self.assertFalse(self.fields['monitoring_cpu'])
        self.assertEqual(self.fields['monitoring_command'], './Allmonitor')
        self.fields.update(monitoring_cpu=True,
                           monitoring_command='./Allmonitor --interval 2')
        data = self.validate()
        self.assertEqual(data['monitoring'], {
            'allocate_cpu': True, 'command': ['./Allmonitor', '--interval', '2']})
        reopened = form_values(data, self.tickets)
        self.assertTrue(reopened['monitoring_cpu'])
        self.assertEqual(reopened['monitoring_command'], './Allmonitor --interval 2')
        reopened['monitoring_cpu'] = False
        self.assertNotIn('monitoring', self.validate(reopened))
        self.fields.update(monitoring_cpu=True, monitoring_command='')
        with self.assertRaisesRegex(ValueError, '모니터링 스크립트'):
            self.validate()

    def test_single_explicit_execution_validates_core_count_and_manual_allocation(self):
        self.fields.update(execution_source='ticket', macro_cpu_policy='manual', macro_cpu_set='2-3')
        for count in ('', '0', '-1', '1.5'):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, '코어 수'):
                self.fields['macro_cores'] = count
                self.validate()
        self.fields['macro_cores'] = '2'
        data = self.validate()
        self.assertEqual(data['cpu_set'], '2-3')
        self.assertEqual(data['resource_source'], 'ticket')
        self.fields['macro_command'] = ''
        with self.assertRaises(ValueError):
            self.validate()

    def test_case_picker_defaults_to_openfoam_run_and_retains_selected_case(self):
        run = self.root / 'OpenFOAM/joon-dev/run'
        run.mkdir(parents=True)
        with patch('cfd_bot.editor.DEFAULT_CASE_ROOT', run):
            self.assertEqual(case_browser_start('', self.tickets), run)
            self.assertEqual(case_browser_start('missing', self.tickets), run)
            self.assertEqual(case_browser_start(str(self.case), self.tickets), self.case)

    def test_script_editing_preserves_arguments_and_timeout_and_can_disable(self):
        original = dict(TEMPLATE, case_dir=str(self.case),
                        preprocess=[{'command': ['./prepare', 'old value'], 'timeout_seconds': 600}])
        fields = form_values(original, self.tickets)
        fields.update(preprocess="'./scripts/prepare case' 'new value'\n./check", postprocess='')
        result = self.validate(fields)
        self.assertEqual(result['preprocess'], [
            {'command': ['./scripts/prepare case', 'new value'], 'timeout_seconds': 600},
            {'command': ['./check']}])
        self.assertEqual(result['postprocess'], [])
        self.assertEqual(form_values(result, self.tickets)['preprocess'], fields['preprocess'])
        fields['preprocess'] = "./prepare 'unterminated"
        with self.assertRaises(ValueError):
            self.validate(fields)

    def test_legacy_ticket_save_does_not_enable_clean_or_post_script(self):
        fields = form_values({'version': 1, 'case_dir': str(self.case)}, self.tickets)
        self.assertEqual(fields['preprocess'], '')
        self.assertEqual(fields['postprocess'], '')
        result = self.validate(fields)
        self.assertEqual(result['preprocess'], [])
        self.assertEqual(result['postprocess'], [])

    def test_existing_ticket_keeps_failure_rules_and_configured_duration(self):
        original = {
            'version': 1, 'case_dir': str(self.case), 'cores': 4, 'cpu_set': '0-3',
            'command': ['./Allrun'], 'expected_seconds': 7200, 'simulation': {'start': 1, 'end': 1000},
            'watcher': {
                'logs': ['log.first', 'log.last'],
                'progress': {'pattern': r'^ITER (\d+)$', 'group': 1, 'start': 1, 'end': 1000},
                'success': {'patterns': ['COMPLETE'], 'required_files': ['.complete']},
                'failure': {'patterns': ['failed stage:'], 'updated_files': ['FAILED.md']},
            },
            'exports': [{'name': 'result', 'pattern': '*.csv', 'on': ['succeeded']}],
            'postprocess': [{'command': ['./plot'], 'timeout_seconds': 60}],
        }
        snapshot = deepcopy(original)
        rebuilt = self.validate(form_values(original, self.tickets))
        self.assertEqual(original, snapshot)
        self.assertEqual(rebuilt['command'], ['./Allrun'])
        self.assertEqual(rebuilt['postprocess'], original['postprocess'])
        self.assertEqual(rebuilt['watcher']['progress'], {'pattern': r'^ITER (\d+)$', 'group': 1})
        self.assertEqual(rebuilt['watcher']['failure']['updated_files'], ['FAILED.md'])
        self.assertNotIn('success', rebuilt['watcher'])
        self.assertNotIn('simulation', rebuilt)
        self.assertEqual(rebuilt['expected_seconds'], 7200)
        self.assertEqual(rebuilt['exports'][0]['on'], [])
        self.assertFalse(rebuilt['exports'][0]['on_complete'])
        self.loaded(rebuilt, 'edited.json')

    def test_macro_form_requires_shared_execution_and_preserves_selected_order(self):
        fields = dict(self.fields, task_type='macro', macro_cores='4', macro_cpu_set='0-3',
                      macro_command='./Allrun --foreground', end_time='1250',
                      cases=[{'case_dir': str(self.case / 'b'), 'state': 'waiting'},
                             {'case_dir': str(self.case / 'a'), 'state': 'waiting'}])
        data = self.validate(fields)
        self.assertEqual(data['resource_source'], 'macro')
        self.assertEqual(data['cpu_policy'], 'auto')
        self.assertNotIn('cpu_set', data)
        self.assertEqual(data['command'], ['./Allrun', '--foreground'])
        self.assertEqual(data['end_time'], 1250)
        self.assertEqual([row['case_dir'] for row in data['cases']],
                         [str(self.case / 'b'), str(self.case / 'a')])
        fields['macro_cores'] = ''
        with self.assertRaisesRegex(ValueError, '공통 코어 수'):
            self.validate(fields)

    def test_legacy_manual_mapping_is_preserved_until_auto_is_selected(self):
        data = dict(TEMPLATE, task_type='macro', case_dir=str(self.case), resource_source='macro',
                    cores=4, cpu_set='0-3', command=['./Allrun'], cases=[], allow_cross_socket=True)
        data.pop('cpu_policy')
        fields = form_values(data, self.tickets)
        self.assertEqual(fields['macro_cpu_policy'], 'manual')
        self.assertEqual(self.validate(fields)['cpu_set'], '0-3')
        fields['macro_cpu_policy'] = 'auto'
        result = self.validate(fields)
        self.assertEqual(result['cpu_policy'], 'auto')
        self.assertNotIn('cpu_set', result)
        self.assertTrue(result['allow_cross_socket'])

    def test_multistage_logs_and_empty_notifications(self):
        self.fields.update(logs='log.preprocess\nlog.solver', events=[])
        case = self.loaded(self.validate(), 'multi.json')
        self.assertEqual(case['watcher']['logs'], ['log.preprocess', 'log.solver'])
        self.assertEqual(case['notifications']['events'], [])

    def test_residual_path_roundtrips_as_a_case_relative_png_pattern(self):
        self.fields['residual_pattern'] = 'plots/residual*.png'
        data = self.validate()
        self.assertEqual(self.loaded(data, 'residual.json')['residual_pattern'], 'plots/residual*.png')
        self.assertEqual(form_values(data, self.tickets)['residual_pattern'], 'plots/residual*.png')
        self.fields['residual_pattern'] = ''
        self.assertEqual(self.validate()['residual_pattern'], '')

    def test_invalid_paths_and_patterns_leave_no_validation_files(self):
        for edits in [{'case_dir': str(self.root / 'missing')}, {'logs': '../secret'},
                      {'failure_patterns': '['}, {'updated_files': '/etc/passwd'},
                      {'residual_pattern': '../residual.png'}, {'residual_pattern': 'log.solver'},
                      {'logs': 'log.solver\nlog.solver'}, {'logs': ''}]:
            with self.subTest(edits=edits):
                with self.assertRaises(ValueError):
                    self.validate(dict(self.fields, **edits))
                self.assertEqual(list(self.tickets.iterdir()), [])

    def test_template_saved_for_reuse_without_copying_case_paths(self):
        path = self.root / 'ticket-patterns.json'
        library = PatternLibrary(path)
        rules = dict(DEFAULT_RULES, failure_patterns=['failed stage:', 'Final monitoring failed'])
        library.save('정상상태 계산', rules)
        loaded = PatternLibrary(path).load()
        self.assertEqual(loaded['정상상태 계산'], rules)
        self.assertEqual(loaded[DEFAULT_NAME], DEFAULT_RULES)
        self.assertNotIn('case_dir', path.read_text())
        self.assertEqual(list(self.tickets.glob('*.json')), [])

    def test_invalid_template_does_not_replace_saved_library(self):
        library = PatternLibrary(self.root / 'ticket-patterns.json')
        library.save('valid', DEFAULT_RULES)
        before = library.path.read_bytes()
        with self.assertRaises(ValueError):
            library.save('broken', dict(DEFAULT_RULES, failure_patterns=['[']))
        self.assertEqual(before, library.path.read_bytes())
        with self.assertRaises(ValueError):
            library.save(DEFAULT_NAME, DEFAULT_RULES)


class BulkDeleteControllerTests(unittest.TestCase):
    def test_selected_tickets_are_deleted_while_unselected_current_draft_is_kept(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        service = TicketService(folder / 'tickets')
        files = [service.path(f'alone-{name}.json') for name in ('current', 'b', 'c')]
        for path in files:
            path.write_text(json.dumps(dict(TEMPLATE, case_dir=str(folder))))
        editor = TicketEditor.__new__(TicketEditor)
        editor.root = None
        editor.service, editor.files, editor.current = service, files, files[0]
        editor.file_revision = service.revision(files[0].name)
        editor.listbox = Mock()
        editor.listbox.curselection.return_value = (1, 2)
        editor.messagebox, editor.status = Mock(), Mock()
        editor.messagebox.askyesno.return_value = True
        editor.refresh, editor.new = Mock(), Mock()
        editor.delete()
        self.assertEqual(service.listing(), [files[0].name])
        editor.new.assert_not_called()
        editor.refresh.assert_called_once()
        editor.messagebox.showerror.assert_not_called()

    def test_multi_selection_does_not_replace_the_form_or_clear_selection(self):
        editor = TicketEditor.__new__(TicketEditor)
        editor.listbox, editor.status, editor.refresh = Mock(), Mock(), Mock()
        editor.listbox.curselection.return_value = (0, 1)
        editor.open_selected()
        editor.refresh.assert_not_called()
        self.assertIn('2개 선택', editor.status.set.call_args.args[0])


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk
        except ImportError:
            raise unittest.SkipTest('tkinter is not installed')
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f'GUI display unavailable: {exc}')
        cls.addClassCleanup(cls.root.destroy)

    def setUp(self):
        for widget in self.root.winfo_children():
            widget.destroy()
        for sequence in ('<FocusIn>', '<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.root.unbind(sequence)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.case = self.folder / 'case with spaces'
        self.case.mkdir()
        self.editor = TicketEditor(self.root, self.folder / 'tickets')
        errors = patch('tkinter.messagebox.showerror')
        self.errors = errors.start()
        self.addCleanup(errors.stop)
        self.root.update()

    def test_empty_case_entry_and_save_reopen(self):
        editor = self.editor
        self.assertEqual(editor.case_entry.winfo_class(), 'TEntry')
        self.assertEqual(editor.case_entry.get(), '')
        editor.variables['residual_pattern'][0].set('plots/residual*.png')
        self.assertFalse(editor.save())
        self.assertIn('Case directory', self.errors.call_args.args[1])
        editor.case_entry.insert(0, str(self.case))
        self.assertTrue(editor.save())
        path = editor.current
        self.assertEqual(path.name, 'alone-case-with-spaces.json')
        self.assertEqual(load_case(path)['_root'], str(self.case))
        editor.new()
        editor.listbox.selection_set(0)
        editor.open_selected()
        self.assertEqual(editor.case_entry.get(), str(self.case))
        self.assertEqual(editor.variables['name'][0].get(), 'case with spaces')
        self.assertEqual(editor.variables['residual_pattern'][0].get(), 'plots/residual*.png')

    def test_ticket_and_queue_select_all_clear_and_bulk_cancel(self):
        editor = self.editor
        editor.listbox.insert('end', 'one.json', 'two.json')
        editor.select_all_tickets()
        self.assertEqual(editor.listbox.curselection(), (0, 1))
        editor.clear_ticket_selection()
        self.assertEqual(editor.listbox.curselection(), ())

        editor.bot_config.write_text(json.dumps(dict(
            version=1, state_dir='state', cases=[],
            telegram=dict(allowed_user_ids=[10], chat_ids=[20]),
            scheduler=dict(enabled=True))))
        store = Store(load_bot(editor.bot_config)['state_dir'])
        jobs = []
        for index in range(2):
            root = self.folder / f'queued-{index}'
            root.mkdir()
            jobs.append(store.enqueue(dict(name=f'queue {index}', _root=str(root), command=['./Allrun'])))
        editor.open_queue_manager()
        self.root.update()
        self.assertEqual(editor.queue_listbox.size(), 2)
        editor.select_all_queue()
        self.assertEqual(editor.queue_listbox.curselection(), (0, 1))
        editor.clear_queue_selection()
        self.assertEqual(editor.queue_listbox.curselection(), ())
        editor.queue_listbox.selection_set(0, 'end')
        with patch('tkinter.messagebox.askyesno', return_value=True):
            editor.cancel_queue_selection()
        self.assertEqual([store.job(job['id'])['status'] for job in jobs], ['cancelled', 'cancelled'])
        self.assertEqual(editor.queue_listbox.size(), 0)
        self.assertEqual(editor.queue_result_listbox.size(), 2)
        editor.queue_result_listbox.selection_set(0)
        editor.open_queue_result_data()
        self.assertIn('티켓 JSON', editor.queue_status.get())

    def test_single_execution_controls_save_and_child_inherits(self):
        editor = self.editor
        editor.case_entry.insert(0, str(self.case))
        variable, choices = editor.variables['execution_source']
        variable.set(choices['ticket'])
        editor.update_execution_visibility()
        self.assertTrue(editor.common_execution.grid_info())
        editor.variables['macro_cores'][0].set('4')
        editor.variables['macro_command'][0].set('./Allrun')
        self.assertTrue(editor.save())
        case = load_case(editor.current)
        self.assertEqual((case['resource_source'], case['cores']), ('ticket', 4))
        editor.set_form(dict(case, role='child', macro_ticket='macro-test.json', resource_source='macro'))
        self.assertTrue(editor.common_execution.grid_info())
        self.assertTrue(editor.execution_source.instate(['disabled']))
        entries = [w for w in editor.common_execution.winfo_children() if w.winfo_class() == 'TEntry']
        self.assertTrue(all(w.instate(['disabled']) for w in entries))

    def test_monitoring_checkbox_controls_script_and_saves(self):
        editor = self.editor
        editor.case_entry.insert(0, str(self.case))
        self.assertFalse(editor.monitoring_command_group.winfo_ismapped())
        editor.variables['monitoring_cpu'][0].set(True)
        editor.update_execution_visibility()
        self.root.update_idletasks()
        self.assertTrue(editor.monitoring_command_group.winfo_ismapped())
        self.assertFalse(editor.monitoring_command.instate(['disabled']))
        editor.variables['monitoring_command'][0].set('./Allmonitor --interval 2')
        self.assertTrue(editor.save())
        self.assertEqual(load_case(editor.current)['monitoring'], {
            'allocate_cpu': True, 'command': ['./Allmonitor', '--interval', '2']})

    def test_execution_button_tracks_running_and_idle_ticket(self):
        editor = self.editor
        editor.case_entry.insert(0, str(self.case))
        self.assertTrue(editor.save())
        editor.runner = Mock()
        editor.runner.state.return_value = dict(state='running', enabled=False, label='계산중 · 실행 불가')
        editor.update_execution_button()
        self.assertTrue(editor.run_button.instate(['disabled']))
        editor.runner.state.return_value = dict(state='idle', enabled=True, label='실행')
        editor.update_execution_button()
        self.assertFalse(editor.run_button.instate(['disabled']))
        self.assertEqual(editor.run_button.cget('text'), '실행')

    def test_case_scan_accepts_openfoam_bootstrap_setting(self):
        editor = self.editor
        editor.bot_config.write_text(json.dumps(dict(version=1, scheduler=dict(
            openfoam_bashrc='deploy/openfoam-env.sh'))))
        variable, choices = editor.variables['task_type']
        variable.set(choices['macro'])
        editor.case_entry.insert(0, str(self.folder))
        with patch('cfd_bot.processes.snapshot', return_value={'cases': {}}):
            editor.scan_cases()
            deadline = time.monotonic() + 3
            while editor.scan_in_progress and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
        self.assertFalse(editor.scan_in_progress)
        self.errors.assert_not_called()

    def test_folder_picker_retains_other_fields_and_custom_names(self):
        editor = self.editor
        editor.variables['name'][0].set('custom case')
        editor.filename.set('custom.json')
        with patch('tkinter.filedialog.askdirectory', return_value=str(self.case)) as picker:
            editor.choose_case()
        self.assertEqual(picker.call_args.kwargs['initialdir'], str(case_browser_start('', editor.tickets_dir)))
        self.assertEqual(editor.case_entry.get(), str(self.case))
        self.assertEqual(editor.variables['name'][0].get(), 'custom case')
        self.assertEqual(editor.filename.get(), 'custom.json')

    def test_log_file_picker_supports_multiple_case_relative_paths_and_cancel(self):
        editor = self.editor
        editor.case_entry.insert(0, str(self.case))
        folder = self.case / 'logs with spaces'
        folder.mkdir()
        paths = [self.case / 'log.solver', folder / 'second.log']
        for path in paths:
            path.touch()
        with patch('tkinter.filedialog.askopenfilenames', return_value=tuple(map(str, paths))):
            editor.choose_logs()
        expected = 'log.solver\nlogs with spaces/second.log'
        self.assertEqual(editor.texts['logs'].get('1.0', 'end-1c'), expected)
        with patch('tkinter.filedialog.askopenfilenames', return_value=()):
            editor.choose_logs()
        self.assertEqual(editor.texts['logs'].get('1.0', 'end-1c'), expected)
        with patch('tkinter.filedialog.askopenfilenames', return_value=(str(self.folder / 'outside.log'),)):
            editor.choose_logs()
        self.errors.assert_called_once()
        self.assertEqual(editor.texts['logs'].get('1.0', 'end-1c'), expected)

    def test_cancel_switch_and_decline_overwrite_preserve_work(self):
        editor = self.editor
        editor.case_entry.insert(0, str(self.case))
        with patch('tkinter.messagebox.askyesnocancel', return_value=None):
            editor.new()
        self.assertEqual(editor.case_entry.get(), str(self.case))
        existing = editor.tickets_dir / 'alone-existing.json'
        existing.write_text('{"untouched": true}')
        editor.filename.set(existing.name)
        with patch('tkinter.messagebox.askyesno', return_value=False):
            self.assertFalse(editor.save())
        self.assertEqual(existing.read_text(), '{"untouched": true}')

    def test_request_data_dialog_applies_without_automatic_send(self):
        editor = self.editor
        editor.case_entry.insert(0, str(self.case))

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        for kind, values in [('exports', ['result', 'plots/*.png', 'photo', '2'])]:
            editor.edit_item(kind, new=True)
            dialog = next(w for w in self.root.winfo_children() if w.winfo_class() == 'Toplevel')
            widgets = list(descendants(dialog))
            fields = [w for w in widgets if w.winfo_class() in ('TEntry', 'TCombobox')]
            for widget, value in zip(fields, values):
                if widget.winfo_class() == 'TCombobox':
                    widget.set(value)
                else:
                    widget.delete(0, 'end')
                    widget.insert(0, value)
            next(w for w in widgets if w.winfo_class() == 'TButton' and w.cget('text') == '적용').invoke()
            self.assertFalse(dialog.winfo_exists())
        self.assertEqual(editor.exports[0]['max_files'], 2)
        self.assertTrue(editor.save())
        saved = load_case(editor.current)
        self.assertEqual(saved['exports'][0]['kind'], 'photo')
        self.assertEqual(saved['exports'][0]['on'], [])
        self.assertFalse(saved['exports'][0]['on_complete'])
        self.errors.assert_not_called()


    def test_removed_inputs_and_template_reuse(self):
        editor = self.editor
        for key in ('cores', 'cpu_set', 'expected_seconds', 'start', 'end', 'command', 'postprocess'):
            self.assertNotIn(key, editor.variables)
        self.assertEqual([editor.tabs.tab(tab, 'text') for tab in editor.tabs.tabs()],
                         ['기본 설정', '종료·실패 판정', '요청 데이터', '작업 큐', '전·후처리'])
        self.assertEqual(editor.texts['preprocess'].get('1.0', 'end-1c'), './Allclean')
        self.assertEqual(editor.texts['postprocess'].get('1.0', 'end-1c'), './Allpost')
        editor.texts['failure_patterns'].insert('1.0', 'failed stage:')
        with patch('tkinter.simpledialog.askstring', return_value='pipeline'):
            editor.save_pattern()
        with patch('tkinter.messagebox.askyesnocancel', return_value=False):
            editor.new()
        editor.pattern_choice.set('pipeline')
        editor.apply_pattern()
        self.assertEqual(editor.texts['failure_patterns'].get('1.0', 'end-1c'), 'failed stage:')

if __name__ == '__main__':
    unittest.main()
