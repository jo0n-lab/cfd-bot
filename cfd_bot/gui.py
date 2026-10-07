"""Form-based editor for case tickets; JSON is only the storage format."""
from copy import deepcopy
from pathlib import Path

from .artifacts import export_files, residual_files
from .patterns import DEFAULT_NAME, PatternLibrary
from .control import control_times
from .queue_control import cancel_queued_jobs, interrupt_running_jobs
from .run_views import job_view, running_macro_views, tracking_registry
from .tickets import STATES, discover_cases, has_postprocessing, ticket_name
from .queueing import job_queue_id

# Keep the form helpers importable here for existing integrations.
from .editor import (DEFAULT_SCRIPTS, EVENTS, TEMPLATE, TicketService, case_browser_start,
                     form_document, form_values, lines, numeric, validate_document, validate_export)


class TicketEditor:
    def __init__(self, root, tickets_dir, bot_config=None):
        # Import lazily so form conversion and CLI commands work without Tk.
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk, self.ttk = tk, ttk
        self.filedialog, self.messagebox = filedialog, messagebox
        self.root = root
        style = ttk.Style(root)
        style.configure('CaseHistory.TFrame', background='#fff0bf')
        style.configure('CaseHistory.TLabel', background='#fff0bf', foreground='#704800')
        self.tickets_dir = Path(tickets_dir).resolve()
        self.bot_config = Path(bot_config) if bot_config else self.tickets_dir.parent / 'bot.json'
        self.macro_cases = []
        self.case_core_vars = {}
        self.scan_in_progress = False
        self.auto_end = ''
        self.tickets_dir.mkdir(parents=True, exist_ok=True)
        self.service = TicketService(self.tickets_dir)
        self.file_revision = None
        self.runner = None
        self.run_busy = False
        self.current = None
        self.baseline = None
        self.variables, self.texts = {}, {}
        self.exports = []
        self.source = {}
        self.queue_window = None
        self.queue_listbox = None
        self.queue_listboxes = {}
        self.queue_jobs_by_lane = {'all': []}
        self.queue_jobs = []
        self.queue_store = None
        self.queue_config = None
        self.queue_result_listbox = None
        self.queue_result_jobs = []
        self.queue_active_listbox = None
        self.queue_active_jobs = []
        self.queue_registry = {}
        self.pattern_library = PatternLibrary(self.tickets_dir.parent / 'ticket-patterns.json')
        root.title('CFD bot ticket editor')
        root.geometry('1120x820')
        root.minsize(920, 720)
        root.protocol('WM_DELETE_WINDOW', self.close)

        outer = ttk.Panedwindow(root, orient='horizontal')
        outer.pack(fill='both', expand=True, padx=12, pady=12)
        left = ttk.Frame(outer, width=240)
        right = ttk.Frame(outer)
        outer.add(left, weight=1)
        outer.add(right, weight=4)
        ttk.Label(left, text='티켓 목록').pack(anchor='w')
        ttk.Label(left, text='Ctrl / Shift로 여러 티켓 선택').pack(anchor='w')
        listing = ttk.Frame(left)
        listing.pack(fill='both', expand=True, pady=8)
        self.listbox = tk.Listbox(listing, exportselection=False, width=27, selectmode='extended')
        self.listbox.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(listing, command=self.listbox.yview)
        scroll.pack(side='right', fill='y')
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.bind('<<ListboxSelect>>', self.open_selected)
        ticket_selection = ttk.Frame(left)
        ticket_selection.pack(fill='x', pady=(0, 8))
        ttk.Button(ticket_selection, text='전체 선택', command=self.select_all_tickets).pack(side='left', fill='x', expand=True)
        ttk.Button(ticket_selection, text='전체 해제', command=self.clear_ticket_selection).pack(side='left', fill='x', expand=True, padx=(6, 0))
        ttk.Button(left, text='새 티켓', command=self.new).pack(fill='x')
        ttk.Button(left, text='선택 복제', command=self.duplicate).pack(fill='x', pady=(6, 0))
        ttk.Button(left, text='목록 새로고침', command=self.refresh).pack(fill='x', pady=6)
        ttk.Button(left, text='선택 삭제', command=self.delete).pack(fill='x')
        ttk.Button(left, text='작업 큐 관리', command=self.open_queue_manager).pack(fill='x', pady=(6, 0))

        ttk.Label(right, text='케이스 설정', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        ttk.Label(right, text='감시할 케이스와 요청 시 받을 데이터를 등록하세요.').pack(anchor='w', pady=(4, 12))
        mode = ttk.Frame(right)
        mode.pack(fill='x', pady=(0, 8))
        mode.columnconfigure(1, weight=1)
        kind = self.field(mode, 0, 'task_type', '작업 종류',
                          choices={'single': '개별 작업', 'macro': '매크로 작업'})
        kind.bind('<<ComboboxSelected>>', self.task_type_changed)
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill='both', expand=True)
        basic = self.tab('기본 설정')
        rules = self.tab('종료·실패 판정')
        exports = self.tab('요청 데이터')
        queue_tab = self.tab('작업 큐')
        scripts = self.tab('전·후처리')

        for row, (stage, label) in enumerate((('preprocess', '전처리'), ('postprocess', '후처리'))):
            self.multiline(scripts, row, stage, label, height=2, columnspan=1,
                           hint=f'기본값: {DEFAULT_SCRIPTS[stage]} · 한 줄에 한 명령 · 빈칸이면 실행하지 않음')
            def reset_script(key=stage):
                self.texts[key].delete('1.0', 'end')
                self.texts[key].insert('1.0', DEFAULT_SCRIPTS[key])
            ttk.Button(scripts, text='기본값', command=reset_script).grid(row=row * 2, column=2, padx=(8, 0))
        ttk.Label(scripts, text='각 케이스 폴더에서 전처리 → 계산 → 정상 종료 후 후처리 순으로 실행합니다.\n'
                  '매크로에서는 모든 child에 같은 명령을 적용합니다.\n'
                  'Allclean은 기존 계산 결과를 정리합니다. 이어서 계산할 때는 전처리를 비우세요.',
                  wraplength=650).grid(row=4, column=0, columnspan=3, sticky='w', pady=16)

        self.case_entry = self.field(basic, 0, 'case_dir', 'Case directory *',
                                    hint='실제 OpenFOAM 케이스 폴더')
        ttk.Button(basic, text='폴더 선택…', command=self.choose_case).grid(row=0, column=2, padx=(8, 0))
        self.case_entry.bind('<FocusOut>', lambda _event: self.suggest_names())
        self.field(basic, 1, 'name', '표시 이름', hint='비워 두면 케이스 폴더 이름을 사용합니다.')
        self.multiline(basic, 2, 'logs', '로그 파일', height=3,
                       columnspan=1, hint='직접 입력하거나 파일 선택으로 여러 로그를 고를 수 있습니다. 한 줄에 하나.')
        ttk.Button(basic, text='파일 선택…', command=self.choose_logs).grid(row=4, column=2, padx=(8, 0), sticky='n', pady=(8, 0))
        self.field(basic, 3, 'residual_pattern', 'Residual PNG 경로',
                   hint='케이스 기준 상대 경로. 예: residual*.png 또는 plots/residual*.png · 최신 이미지 1개 전송')
        self.residual_picker = ttk.Button(basic, text='파일 선택…', command=self.choose_residual)
        self.residual_picker.grid(row=6, column=2, padx=(8, 0))
        self.field(basic, 4, 'end_time', 'End Time / Iteration',
                   hint='기본값: controlDict endTime. 매크로에서 비우면 각 child의 종료값을 사용합니다.')
        ttk.Label(basic, text='코어 수·CPU 범위는 ofps에서 같은 케이스 경로의 실제 값을 확인합니다.\n'
                             '예상 시간이 미설정이면 controlDict의 종료값과 최근 로그의 진행 속도로 계산합니다.',
                  wraplength=650).grid(row=10, column=0, columnspan=3, sticky='w', pady=18)
        event_frame = ttk.LabelFrame(basic, text='Telegram 알림', padding=8)
        event_frame.grid(row=11, column=0, columnspan=3, sticky='ew', pady=(12, 0))
        self.event_vars = self.event_checks(event_frame, list(EVENTS))

        role = self.field(queue_tab, 0, 'role', '개별 작업 소속', choices={'alone': 'alone · 독립 케이스', 'child': 'child · 매크로 소속'})
        role.bind('<<ComboboxSelected>>', lambda _event: self.update_execution_visibility())
        self.field(queue_tab, 1, 'macro_ticket', '매크로 티켓 경로',
                   hint='child만 사용합니다. 현재 티켓 기준 상대 경로. 예: macro-batch.json')
        self.execution_source = self.field(queue_tab, 2, 'execution_source', '실행 설정 방식',
                                          choices={'case': '케이스 설정 사용', 'ticket': '티켓에서 지정'})
        self.execution_source.bind('<<ComboboxSelected>>', lambda _event: self.update_execution_visibility())
        self.queue_profile = ttk.LabelFrame(queue_tab, text='이름 있는 대기열', padding=10)
        self.queue_profile.grid(row=5, column=0, columnspan=3, sticky='ew', pady=10)
        self.queue_profile.columnconfigure(1, weight=1)
        self.field(self.queue_profile, 0, 'queue_id', '대기열 이름',
                   hint='개수 제한 없음 · 매크로는 전용 이름을 사용합니다. quota는 실행할 케이스의 코어 수에서 자동 결정됩니다.')
        self.execution_note = ttk.Label(queue_tab, wraplength=650)
        self.execution_note.grid(row=6, column=0, columnspan=3, sticky='w', pady=8)
        self.common_execution = ttk.LabelFrame(queue_tab, text='실행 설정', padding=10)
        self.common_execution.grid(row=7, column=0, columnspan=3, sticky='ew', pady=10)
        self.common_execution.columnconfigure(1, weight=1)
        self.field(self.common_execution, 0, 'macro_cores', '코어 수 (NP)')
        policy = self.field(self.common_execution, 1, 'macro_cpu_policy', 'CPU 배정',
                           choices={'auto': '자동 배정 (권장)', 'manual': '고급: CPU 직접 지정'},
                           hint='전체 소켓의 빈 코어를 자동 배정합니다. 작업 간 코어 중복은 금지하며, 부족하면 대기합니다.')
        policy.bind('<<ComboboxSelected>>', lambda _event: self.update_execution_visibility())
        self.field(self.common_execution, 2, 'macro_command', '실행 명령', hint='예: ./Allrun · 지정한 NP/CPU_SET을 실행 시 케이스에 적용합니다.')
        dynamic = self.check(self.common_execution, 3, 'dynamic_cores',
                             '동적 코어 매크로 · 실행할 하위 케이스의 NP만큼 quota 자동 확보')
        dynamic.configure(command=lambda: (self.render_case_rows(), self.update_execution_visibility()))
        self.manual_execution = ttk.LabelFrame(self.common_execution, text='고급 수동 배정', padding=8)
        self.manual_execution.grid(row=6, column=0, columnspan=3, sticky='ew', pady=8)
        self.manual_execution.columnconfigure(1, weight=1)
        self.field(self.manual_execution, 0, 'macro_cpu_set', 'CPU 범위', hint='고정 배정이 필요한 경우만 사용하세요. 실행 전 ofps 검사 필수.')
        self.check(self.manual_execution, 1, 'macro_cross_socket', '여러 소켓/NUMA에 걸친 CPU 범위 허용')
        self.monitoring_execution = ttk.LabelFrame(queue_tab, text='계산 모니터링', padding=10)
        self.monitoring_execution.grid(row=8, column=0, columnspan=3, sticky='ew', pady=10)
        self.monitoring_execution.columnconfigure(1, weight=1)
        monitor_check = self.check(
            self.monitoring_execution, 0, 'monitoring_cpu', '모니터링 별도 코어 배치')
        monitor_check.configure(command=self.update_execution_visibility)
        self.monitoring_command_group = ttk.Frame(self.monitoring_execution)
        self.monitoring_command_group.grid(row=2, column=0, columnspan=3, sticky='ew')
        self.monitoring_command_group.columnconfigure(1, weight=1)
        self.monitoring_command = self.field(
            self.monitoring_command_group, 0, 'monitoring_command', '모니터링 스크립트',
            hint='기본값: ./Allmonitor · 계산 CPU와 겹치지 않는 물리 CPU 1개에서 실행합니다.')
        self.discovery_filters = ttk.LabelFrame(queue_tab, text='매크로 하위 케이스 이름 필터', padding=10)
        self.discovery_filters.grid(row=9, column=0, columnspan=3, sticky='ew', pady=10)
        self.discovery_filters.columnconfigure(1, weight=1)
        self.multiline(
            self.discovery_filters, 0, 'case_include_patterns', '포함 패턴', height=2,
            hint='선택 사항 · 폴더 이름이 하나 이상 일치해야 포함합니다. 한 줄에 하나. 예: DS_CART_NQ_*')
        self.multiline(
            self.discovery_filters, 1, 'case_exclude_patterns', '제외 패턴', height=2,
            hint='선택 사항 · 하나라도 일치하면 제외합니다. 한 줄에 하나. 예: DS_ID_LHS_vN-Q_fC*')
        ttk.Label(queue_tab, text='매크로: Case directory의 직계 하위 폴더만 검색합니다.\n'
                  '각 폴더 바로 아래에 Allrun이 있어야 하며, *-template과 실행 중인 케이스는 제외합니다.\n'
                  '이름 필터는 폴더 이름에 적용하며 제외 패턴이 포함 패턴보다 우선합니다.\n'
                  'postProcessing이 있는 케이스도 포함하며 노란색으로 표시합니다.\n'
                  '행 순서대로 실행합니다. 제외할 행의 삭제 버튼을 누른 뒤 저장하면 큐에 등록됩니다.',
                  wraplength=650).grid(row=10, column=0, columnspan=3, sticky='w', pady=12)
        self.scan_button = ttk.Button(queue_tab, text='직계 하위 케이스 검색', command=self.scan_cases)
        self.scan_button.grid(row=11, column=0, columnspan=3, sticky='w')
        self.case_rows = ttk.Frame(queue_tab)
        self.case_rows.grid(row=12, column=0, columnspan=3, sticky='ew', pady=8)
        self.case_rows.columnconfigure(0, weight=1)

        ttk.Label(
            rules,
            text='정상 종료는 controlDict의 stopAt=endTime과 로그의 최종 Time ≥ 지정 종료값으로 판정합니다.\n'
                 '기본 종료값은 controlDict endTime이며 기본 설정의 End Time / Iteration으로 지정할 수 있습니다.\n'
                 '프로세스가 그 전에 사라지면 실패이며, 아래 규칙은 추가 실패 원인을 찾습니다.',
            wraplength=650).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 10))
        preset = ttk.LabelFrame(rules, text='실패 패턴 템플릿', padding=8)
        preset.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(0, 10))
        preset.columnconfigure(0, weight=1)
        self.pattern_choice = tk.StringVar(value=DEFAULT_NAME)
        self.pattern_picker = ttk.Combobox(preset, textvariable=self.pattern_choice, state='readonly')
        self.pattern_picker.grid(row=0, column=0, sticky='ew')
        ttk.Button(preset, text='불러오기', command=self.apply_pattern).grid(row=0, column=1, padx=6)
        ttk.Button(preset, text='현재 패턴 저장…', command=self.save_pattern).grid(row=0, column=2)
        self.multiline(rules, 2, 'failure_patterns', '실패 로그 패턴', height=4,
                       hint='추가 실패 정규식, 한 줄에 하나. 템플릿으로 저장해 다른 케이스에 사용할 수 있습니다.')
        self.check(rules, 3, 'openfoam_defaults', 'OpenFOAM 기본 오류 감지 사용')
        self.multiline(rules, 4, 'updated_files', '실패 표식 파일', height=2,
                       hint='선택 사항. 갱신되면 실패로 판정할 파일의 상대 경로, 한 줄에 하나.')

        self.export_table = self.table(
            exports,
            'Telegram에서 /data → 케이스 → 데이터 이름을 선택하면 보낼 파일을 등록합니다.\n'
            '예: contour 이미지, 결과 CSV, 솔버 로그. Residual 이미지는 기본 설정에서 경로를 지정합니다.',
            ('name', 'pattern', 'kind', 'max_files'),
            ('요청 이름', '파일 경로 / 패턴', '전송 형식', '최대 파일 수'), 'exports')

        footer = ttk.Frame(right)
        footer.pack(fill='x', pady=(12, 0))
        ttk.Label(footer, text='티켓 파일명').pack(side='left')
        self.filename = tk.StringVar()
        ttk.Entry(footer, textvariable=self.filename, width=26).pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(footer, text='검증', command=self.validate).pack(side='right')
        self.run_button = ttk.Button(footer, text='즉시 실행', command=lambda: self.submit('run'))
        self.run_button.pack(side='right', padx=6)
        self.run_button.bind('<Destroy>', lambda _event: self.stop_execution_poll())
        self.queue_button = ttk.Button(footer, text='대기열 등록', command=lambda: self.submit('queue'))
        self.queue_button.pack(side='right', padx=6)
        ttk.Button(footer, text='저장', command=self.save).pack(side='right', padx=6)
        self.status = tk.StringVar()
        ttk.Label(right, textvariable=self.status, wraplength=720).pack(fill='x', pady=(6, 0))
        self.run_status = tk.StringVar()
        ttk.Label(right, textvariable=self.run_status, wraplength=720).pack(fill='x')
        root.bind('<Control-s>', lambda _event: self.save())
        self.new()
        self.refresh_patterns()
        self.poll_execution()

    def execution_runner(self):
        if self.runner is None:
            from .config import load_bot
            from .storage import Store
            from .ticket_run import TicketRunner
            config = load_bot(self.bot_config)
            self.runner = TicketRunner(self.service, config, Store(config['state_dir']))
        return self.runner

    def update_execution_button(self):
        if self.run_busy:
            self.run_button.configure(text='실행 상태 확인 중…', state='disabled')
            self.queue_button.configure(state='disabled')
            return
        if len(self.listbox.curselection()) > 1:
            self.run_button.configure(text='즉시 실행', state='disabled')
            self.queue_button.configure(state='disabled')
            self.run_status.set('실행할 티켓 한 개를 선택하세요.')
            return
        if not self.current:
            self.run_button.configure(text='저장 후 즉시 실행', state='normal')
            self.queue_button.configure(text='저장 후 대기열 등록', state='normal')
            self.run_status.set('티켓을 저장한 뒤 즉시 실행하거나 대기열에 등록할 수 있습니다.')
            return
        try:
            state = self.execution_runner().state(self.current.name)
            run_enabled = state.get('run_enabled', state.get('enabled', False))
            queue_enabled = state.get('queue_enabled', run_enabled)
            self.run_button.configure(text=state['label'], state='normal' if run_enabled else 'disabled')
            self.queue_button.configure(text=state.get('queue_label', '대기열 등록'),
                                        state='normal' if queue_enabled else 'disabled')
            self.run_status.set(state.get('availability_message') or
                                ('계산 중이거나 이미 대기 중인 티켓입니다.'
                                 if not run_enabled and not queue_enabled else
                                 '즉시 실행 또는 일반 대기열 등록을 선택하세요.'))
        except (OSError, ValueError) as exc:
            self.run_button.configure(text='실행 상태 확인 필요', state='disabled')
            self.queue_button.configure(state='disabled')
            self.run_status.set(str(exc))

    def poll_execution(self):
        self.update_execution_button()
        self._execution_poll = self.root.after(1000, self.poll_execution)

    def stop_execution_poll(self):
        if getattr(self, '_execution_poll', None):
            self.root.after_cancel(self._execution_poll)
            self._execution_poll = None

    def tab(self, title):
        """Scroll forms independently to keep fields reachable on small screens."""
        page = self.ttk.Frame(self.tabs)
        self.tabs.add(page, text=title)
        canvas = self.tk.Canvas(page, highlightthickness=0)
        scrollbar = self.ttk.Scrollbar(page, orient='vertical', command=canvas.yview)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        body = self.ttk.Frame(canvas, padding=14)
        window = canvas.create_window((0, 0), window=body, anchor='nw')
        body.columnconfigure(1, weight=1)
        body.bind('<Configure>', lambda _event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))

        def reveal(event):
            if not str(event.widget).startswith(str(body) + '.'):
                return
            self.root.update_idletasks()
            y = event.widget.winfo_rooty() - body.winfo_rooty()
            top = canvas.canvasy(0)
            if y < top or y + event.widget.winfo_height() > top + canvas.winfo_height():
                canvas.yview_moveto(max(0, y - 20) / max(body.winfo_height(), 1))
        self.root.bind('<FocusIn>', reveal, add='+')

        def wheel(event):
            if not str(event.widget).startswith(str(page)):
                return
            if isinstance(event.widget, (self.tk.Text, self.ttk.Treeview, self.ttk.Combobox)):
                return
            if body.winfo_height() > canvas.winfo_height():
                direction = -1 if event.num == 4 or event.delta > 0 else 1
                canvas.yview_scroll(direction * 3, 'units')
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.root.bind(sequence, wheel, add='+')
        return body

    def field(self, parent, row, key, label, *, hint='', choices=None):
        row *= 2
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 14), pady=(8, 0))
        variable = self.tk.StringVar(parent)
        self.variables[key] = (variable, choices)
        if choices:
            widget = self.ttk.Combobox(parent, textvariable=variable, values=list(choices.values()), state='readonly')
        else:
            widget = self.ttk.Entry(parent, textvariable=variable)
        widget.grid(row=row, column=1, sticky='ew', pady=(8, 0))
        if hint:
            self.ttk.Label(parent, text=hint, wraplength=500).grid(row=row + 1, column=1, columnspan=2,
                                                                 sticky='w', pady=(3, 0))
        return widget

    def multiline(self, parent, row, key, label, *, height=2, hint='', columnspan=2):
        row *= 2
        self.ttk.Label(parent, text=label).grid(row=row, column=0, sticky='nw', padx=(0, 14), pady=(8, 0))
        frame = self.ttk.Frame(parent)
        frame.grid(row=row, column=1, columnspan=columnspan, sticky='ew', pady=(8, 0))
        widget = self.tk.Text(frame, height=height, width=40, wrap='none', undo=True, font='TkFixedFont')
        widget.pack(side='left', fill='both', expand=True)
        scrollbar = self.ttk.Scrollbar(frame, command=widget.yview)
        scrollbar.pack(side='right', fill='y')
        widget.configure(yscrollcommand=scrollbar.set)

        def next_field(_event):
            widget.tk_focusNext().focus_set()
            return 'break'
        widget.bind('<Tab>', next_field)
        self.texts[key] = widget
        if hint:
            self.ttk.Label(parent, text=hint, wraplength=500).grid(row=row + 1, column=1, columnspan=2,
                                                                 sticky='w', pady=(3, 0))

    def check(self, parent, row, key, label):
        variable = self.tk.BooleanVar(parent)
        self.variables[key] = (variable, None)
        widget = self.ttk.Checkbutton(parent, text=label, variable=variable)
        widget.grid(row=row * 2, column=1, columnspan=2, sticky='w', pady=(8, 0))
        return widget

    def event_checks(self, parent, events):
        variables = {}
        for event in events:
            variable = self.tk.BooleanVar(parent)
            self.ttk.Checkbutton(parent, text=EVENTS[event], variable=variable).pack(side='left', padx=(0, 12))
            variables[event] = variable
        return variables

    def table(self, parent, explanation, columns, headings, kind):
        self.ttk.Label(parent, text=explanation, wraplength=650).pack(anchor='w', pady=(0, 10))
        frame = self.ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        table = self.ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse', height=12)
        for column, heading in zip(columns, headings):
            table.heading(column, text=heading)
            table.column(column, width=200 if column in ('command', 'pattern') else 100, minwidth=60)
        table.grid(row=0, column=0, sticky='nsew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        vertical = self.ttk.Scrollbar(frame, command=table.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        scrollbar = self.ttk.Scrollbar(frame, orient='horizontal', command=table.xview)
        scrollbar.grid(row=1, column=0, sticky='ew')
        table.configure(xscrollcommand=scrollbar.set, yscrollcommand=vertical.set)
        table.bind('<Double-1>', lambda _event: self.edit_item(kind))
        buttons = self.ttk.Frame(parent)
        buttons.pack(fill='x', pady=10)
        for label, callback in [('추가', lambda: self.edit_item(kind, new=True)),
                                ('수정', lambda: self.edit_item(kind)),
                                ('삭제', lambda: self.remove_item(kind))]:
            self.ttk.Button(buttons, text=label, command=callback).pack(side='left', padx=(0, 6))
        return table

    def values(self):
        self.sync_case_cores()
        values = {}
        for key, (variable, choices) in self.variables.items():
            value = variable.get()
            values[key] = {v: k for k, v in choices.items()}[value] if choices else value
        values.update({key: widget.get('1.0', 'end-1c') for key, widget in self.texts.items()})
        values['events'] = [event for event, variable in self.event_vars.items() if variable.get()]
        values['exports'] = deepcopy(self.exports)
        values['_source'] = deepcopy(self.source)
        values['cases'] = deepcopy(self.macro_cases)
        if values.get('end_time') == self.auto_end:
            values['end_time'] = ''
        return values

    def set_form(self, data):
        values = form_values(data, self.tickets_dir)
        for key, (variable, choices) in self.variables.items():
            variable.set(choices[values[key]] if choices else values[key])
        for key, widget in self.texts.items():
            widget.delete('1.0', 'end')
            widget.insert('1.0', values[key])
            widget.edit_reset()
        for event, variable in self.event_vars.items():
            variable.set(event in values['events'])
        self.source = values['_source']
        self.case_core_vars = {}
        self.macro_cases = values['cases']
        self.render_case_rows()
        self.exports = values['exports']
        self.refresh_tables()
        self.auto_name = self.auto_filename = ''
        self.auto_end = ''
        self.refresh_end_default()
        self.update_execution_visibility()
        self.baseline = (self.filename.get(), self.values())

    def refresh_patterns(self):
        try:
            templates = self.pattern_library.load()
            self.pattern_picker.configure(values=list(templates))
        except (ValueError, OSError) as exc:
            self.messagebox.showerror('패턴 템플릿 읽기 실패', str(exc), parent=self.root)

    def task_type_changed(self, _event=None):
        self.macro_cases = []
        self.render_case_rows()
        self.filename.set('')
        self.suggest_names()
        self.update_execution_visibility()

    def update_execution_visibility(self):
        v = self.values()
        macro = v['task_type'] == 'macro'
        child = not macro and v['role'] == 'child'
        self.execution_source.configure(state='disabled' if macro or child else 'readonly')
        self.execution_note.configure(text=(
            '매크로의 모든 하위 케이스에 공통 적용합니다.' if macro else
            '매크로 공통 실행 설정을 상속합니다. 변경은 부모 매크로 티켓에서 하세요.' if child else
            '티켓에서 코어 수와 실행 명령을 지정할 수 있습니다. 케이스 설정 사용 시 기존 NP와 실행 설정을 유지합니다.'))
        def set_enabled(parent):
            for widget in parent.winfo_children():
                if widget.winfo_class() in ('TEntry', 'TCombobox', 'TCheckbutton'):
                    widget.configure(state='disabled' if child else
                                     'readonly' if widget.winfo_class() == 'TCombobox' else 'normal')
                set_enabled(widget)
        set_enabled(self.common_execution)
        set_enabled(self.queue_profile)
        set_enabled(self.monitoring_execution)
        if self.variables['macro_cpu_policy'][0].get() == '고급: CPU 직접 지정':
            self.manual_execution.grid()
        else:
            self.manual_execution.grid_remove()
        if macro or child or v['execution_source'] == 'ticket':
            self.common_execution.grid()
        else:
            self.common_execution.grid_remove()
        if macro:
            self.discovery_filters.grid()
        else:
            self.discovery_filters.grid_remove()
        if v['monitoring_cpu']:
            self.monitoring_command_group.grid()
        else:
            self.monitoring_command_group.grid_remove()
        self.monitoring_command.configure(state='disabled' if child else 'normal')
        self.residual_picker.configure(state='disabled' if macro else 'normal')
        self.render_case_rows()

    def refresh_end_default(self):
        variable = self.variables['end_time'][0]
        if variable.get() not in ('', self.auto_end):
            return
        root = (self.tickets_dir / Path(self.variables['case_dir'][0].get()).expanduser()).resolve()
        control = control_times({'_root': str(root)})
        self.auto_end = f"{control['end']:g}" if control and 'end' in control else ''
        variable.set(self.auto_end)

    def render_case_rows(self):
        self.sync_case_cores()
        self.case_core_vars = {}
        for child in self.case_rows.winfo_children():
            child.destroy()
        for index, row in enumerate(self.macro_cases):
            history = has_postprocessing(row['case_dir'])
            frame = self.ttk.Frame(self.case_rows, style='CaseHistory.TFrame' if history else 'TFrame')
            frame.grid(row=index, column=0, sticky='ew', pady=4)
            frame.columnconfigure(0, weight=1)
            state = STATES.get(row.get('state', 'waiting'), '계산대기')
            description = state + (' · ' + row['reason'] if row.get('reason') else '')
            self.ttk.Label(frame, text=f"{index + 1}. {row['case_dir']}\n{description}",
                           style='CaseHistory.TLabel' if history else 'TLabel',
                           wraplength=570).grid(row=0, column=0, sticky='ew')
            if self.variables.get('dynamic_cores', (None,))[0].get():
                cores = self.tk.StringVar(value=str(row.get('cores') or
                                                    self.variables['macro_cores'][0].get() or '1'))
                self.case_core_vars[index] = cores
                entry = self.ttk.Entry(frame, textvariable=cores, width=6)
                entry.grid(row=0, column=1, padx=6)
                self.ttk.Label(frame, text='cores').grid(row=0, column=2)
            self.ttk.Button(frame, text='삭제', command=lambda i=index: self.remove_case_row(i)).grid(row=0, column=3, padx=8)

    def remove_case_row(self, index):
        self.sync_case_cores()
        self.case_core_vars = {}
        self.macro_cases.pop(index)
        self.render_case_rows()

    def sync_case_cores(self):
        for index, variable in self.case_core_vars.items():
            if index < len(self.macro_cases):
                self.macro_cases[index]['cores'] = variable.get().strip()

    def scan_cases(self):
        if self.scan_in_progress:
            return
        if self.values()['task_type'] != 'macro':
            self.status.set('작업 종류를 매크로 작업으로 선택하세요.')
            return
        directory = self.variables['case_dir'][0].get().strip()
        if not directory:
            self.status.set('검색할 상위 Case directory를 지정하세요.')
            return
        from queue import Queue
        from threading import Thread
        from .config import load_bot
        from .processes import snapshot
        try:
            config = load_bot(self.bot_config)
            scan_values = self.values()
            end = scan_values['end_time']
            end = numeric(end, 'End Time / Iteration') if end.strip() else None
            include_text = scan_values['case_include_patterns']
            exclude_text = scan_values['case_exclude_patterns']
            include_patterns = lines(include_text)
            exclude_patterns = lines(exclude_text)
        except (OSError, ValueError) as exc:
            self.messagebox.showerror('검색 실패', str(exc), parent=self.root)
            return
        root = (self.tickets_dir / Path(directory).expanduser()).resolve()
        result = Queue()
        self.scan_in_progress = True
        self.scan_button.configure(state='disabled')
        self.status.set('ofps 실행 상태와 하위 케이스의 체크포인트·후처리를 확인 중입니다…')
        def work():
            try:
                observed = snapshot(config['ofps_command'])
                result.put(discover_cases(root, observed['cases'], end,
                                          include_patterns, exclude_patterns))
            except Exception as exc:
                result.put(exc)
        def finish():
            if result.empty():
                self.root.after(100, finish)
                return
            self.scan_in_progress = False
            self.scan_button.configure(state='normal')
            found = result.get()
            if isinstance(found, Exception):
                self.messagebox.showerror('검색 실패', str(found), parent=self.root)
            elif (self.variables['case_dir'][0].get().strip() == directory
                  and self.values()['task_type'] == 'macro'
                  and self.values()['case_include_patterns'] == include_text
                  and self.values()['case_exclude_patterns'] == exclude_text):
                self.case_core_vars = {}
                self.macro_cases, skipped = found
                if self.variables['dynamic_cores'][0].get():
                    default = self.variables['macro_cores'][0].get() or '1'
                    for row in self.macro_cases:
                        row['cores'] = default
                self.render_case_rows()
                self.status.set(f'선택 대상 {len(self.macro_cases)}개 · 검색 제외 {len(skipped)}개. 삭제 버튼으로 선택을 조정하세요.')
        Thread(target=work, daemon=True).start()
        self.root.after(100, finish)

    def apply_pattern(self):
        try:
            rules = self.pattern_library.load()[self.pattern_choice.get()]
            for key in ('failure_patterns',):
                self.texts[key].delete('1.0', 'end')
                self.texts[key].insert('1.0', '\n'.join(rules[key]))
            for key in ('openfoam_defaults',):
                variable, choices = self.variables[key]
                variable.set(choices[rules[key]] if choices else rules[key])
            self.status.set(f'패턴 적용: {self.pattern_choice.get()} · 티켓 저장 시 반영됩니다.')
        except (ValueError, OSError, KeyError) as exc:
            self.messagebox.showerror('패턴 불러오기 실패', str(exc), parent=self.root)

    def save_pattern(self):
        from tkinter import simpledialog
        name = simpledialog.askstring('패턴 템플릿 저장', '다른 케이스에서도 사용할 템플릿 이름:', parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        try:
            templates = self.pattern_library.load()
            if name in templates and name != DEFAULT_NAME:
                if not self.messagebox.askyesno('패턴 덮어쓰기', f'{name} 템플릿을 덮어쓸까요?', parent=self.root):
                    return
            values = self.values()
            rules = {'failure_patterns': lines(values['failure_patterns'], strip=False),
                     'openfoam_defaults': values['openfoam_defaults']}
            self.pattern_library.save(name, rules)
            self.refresh_patterns()
            self.pattern_choice.set(name)
            self.status.set(f'패턴 템플릿 저장 완료: {name}')
        except (ValueError, OSError) as exc:
            self.messagebox.showerror('패턴 저장 실패', str(exc), parent=self.root)

    def refresh_tables(self):
        self.export_table.delete(*self.export_table.get_children())
        for index, item in enumerate(self.exports):
            self.export_table.insert('', 'end', iid=str(index), values=(
                item['name'], item['pattern'], '이미지' if item.get('kind', 'document') == 'photo' else '파일',
                item.get('max_files', 1)))

    def edit_item(self, kind='exports', new=False):
        table = self.export_table
        selection = table.selection()
        if not new and not selection:
            return
        index = None if new else int(selection[0])
        item = {} if new else deepcopy(self.exports[index])
        dialog = self.tk.Toplevel(self.root)
        dialog.title('요청 데이터' + (' 추가' if new else ' 수정'))
        dialog.transient(self.root)
        dialog.geometry('660x320')
        panel = self.ttk.Frame(dialog, padding=16)
        panel.pack(fill='both', expand=True)
        panel.columnconfigure(1, weight=1)
        fields = {}
        for row, (key, label, default) in enumerate([
                ('name', '요청 이름 (영문·숫자·_-)', ''),
                ('pattern', '파일 경로 / 패턴', ''),
                ('kind', '전송 형식', 'document'),
                ('max_files', '최대 파일 수 (1–10)', 1)]):
            self.ttk.Label(panel, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=6)
            variable = self.tk.StringVar(panel, value=str(item.get(key, default)))
            fields[key] = variable
            if key == 'kind':
                widget = self.ttk.Combobox(panel, textvariable=variable, values=['photo', 'document'], state='readonly')
            else:
                widget = self.ttk.Entry(panel, textvariable=variable)
            widget.grid(row=row, column=1, sticky='ew', pady=6)
            if row == 0:
                widget.focus_set()
        self.ttk.Label(panel, text='케이스 기준 상대 경로. 예: postProcessing/contour*.png\n'
                                  'photo = 이미지, document = 파일. 최신 파일부터 요청한 개수만큼 보냅니다.',
                       wraplength=600).grid(row=4, column=0, columnspan=2, sticky='w', pady=12)

        def apply():
            try:
                result = {key: variable.get().strip() for key, variable in fields.items()}
                directory = self.variables['case_dir'][0].get().strip()
                result = validate_export(result, self.exports,
                                         self.tickets_dir / Path(directory).expanduser(), index)
                if index is None:
                    self.exports.append(result)
                else:
                    self.exports[index] = result
                self.refresh_tables()
                table.selection_set(str(len(self.exports) - 1 if index is None else index))
                dialog.destroy()
            except ValueError as exc:
                self.messagebox.showerror('입력 확인', str(exc), parent=dialog)

        buttons = self.ttk.Frame(panel)
        buttons.grid(row=5, column=0, columnspan=2, sticky='e', pady=8)
        self.ttk.Button(buttons, text='취소', command=dialog.destroy).pack(side='left', padx=6)
        self.ttk.Button(buttons, text='적용', command=apply).pack(side='left')
        dialog.bind('<Escape>', lambda _event: dialog.destroy())
        dialog.grab_set()

    def remove_item(self, kind='exports'):
        if self.export_table.selection():
            self.exports.pop(int(self.export_table.selection()[0]))
            self.refresh_tables()

    def refresh(self):
        self.files = [self.tickets_dir / name for name in self.service.listing()]
        self.listbox.delete(0, 'end')
        for index, path in enumerate(self.files):
            self.listbox.insert('end', path.name)
            if path == self.current:
                self.listbox.selection_set(index)
                self.listbox.selection_anchor(index)
                self.listbox.activate(index)
        if hasattr(self, 'run_button'):
            self.update_execution_button()

    def confirm_switch(self):
        if self.baseline is None or self.baseline == (self.filename.get(), self.values()):
            return True
        answer = self.messagebox.askyesnocancel('변경 내용 저장', '수정한 티켓을 저장할까요?', parent=self.root)
        return self.save() if answer else answer is not None

    def new(self):
        if not self.confirm_switch():
            return
        self.current = None
        self.file_revision = None
        self.filename.set('')
        self.set_form(TEMPLATE)
        self.refresh()
        self.tabs.select(0)
        self.case_entry.focus_set()
        self.status.set('새 티켓 · Case directory를 입력하세요.')

    def open_selected(self, _event=None):
        picked = self.listbox.curselection()
        if not picked:
            return
        if len(picked) > 1:
            self.status.set(f'{len(picked)}개 선택 · 선택 삭제로 한 번에 삭제합니다.')
            return
        path = self.files[picked[0]]
        if path == self.current:
            return
        if not self.confirm_switch():
            self.refresh()
            return
        try:
            draft = self.service.open(path.name)
            data = draft['values']['_source']
            self.file_revision = draft['revision']
            self.filename.set(path.name)
            self.set_form(data)
            self.current = path
            self.tabs.select(0)
            self.status.set(f'편집 중: {path.name}')
        except (ValueError, OSError) as exc:
            self.messagebox.showerror('티켓 열기 실패', str(exc), parent=self.root)
        self.refresh()

    def duplicate(self):
        picked = self.listbox.curselection()
        if len(picked) != 1:
            self.status.set('복제할 티켓 한 개를 목록에서 선택하세요.')
            return
        path = self.files[picked[0]]
        was_current = path == self.current
        if not self.confirm_switch():
            self.refresh()
            return
        # Saving pending edits may have renamed the selected source ticket.
        if was_current:
            path = self.current
        try:
            draft = self.service.duplicate(path.name)
            data, name = draft['values']['_source'], draft['filename']
        except (ValueError, OSError) as exc:
            self.messagebox.showerror('티켓 복제 실패', str(exc), parent=self.root)
            return
        self.current = None
        self.file_revision = None
        self.filename.set('')
        self.set_form(data)
        # Set the filename after the baseline so this draft counts as unsaved.
        self.filename.set(name)
        self.refresh()
        self.tabs.select(0)
        self.case_entry.focus_set()
        self.case_entry.selection_range(0, 'end')
        self.status.set(f'복제본: {path.name} · Case directory를 변경한 뒤 저장하세요.')

    def suggest_names(self):
        directory = self.variables['case_dir'][0].get().strip()
        if not directory:
            return
        case_path = Path(directory).expanduser()
        if not case_path.is_absolute():
            case_path = self.tickets_dir / case_path
        name = case_path.resolve().name or 'case'
        variable = self.variables['name'][0]
        if variable.get() in ('', self.auto_name):
            variable.set(name)
            self.auto_name = name
        if self.filename.get() in ('', self.auto_filename):
            values = self.values()
            prefix = 'macro' if values['task_type'] == 'macro' else values['role']
            self.auto_filename = ticket_name(name + '.json', prefix)
            self.filename.set(self.auto_filename)
        self.refresh_end_default()

    def choose_case(self):
        initial = case_browser_start(self.variables['case_dir'][0].get(), self.tickets_dir)
        selected = self.filedialog.askdirectory(title='OpenFOAM 케이스 디렉터리', parent=self.root,
                                                initialdir=str(initial), mustexist=True)
        if selected:
            self.variables['case_dir'][0].set(selected)
            self.suggest_names()

    def choose_residual(self):
        directory = self.variables['case_dir'][0].get().strip()
        if not directory:
            self.messagebox.showerror('케이스 경로 확인', 'Case directory를 먼저 입력하세요.', parent=self.root)
            return
        root = (self.tickets_dir / Path(directory).expanduser()).resolve()
        selected = self.filedialog.askopenfilename(title='Residual PNG 선택', parent=self.root,
                                                  initialdir=str(root), filetypes=[('PNG 이미지', '*.png')])
        if selected:
            try:
                relative = Path(selected).resolve().relative_to(root)
            except ValueError:
                self.messagebox.showerror('경로 확인', '케이스 디렉터리 안의 PNG 파일을 선택하세요.', parent=self.root)
                return
            self.variables['residual_pattern'][0].set(str(relative))

    def choose_logs(self):
        directory = self.variables['case_dir'][0].get().strip()
        if not directory:
            self.messagebox.showerror('케이스 경로 확인', 'Case directory를 먼저 입력하세요.', parent=self.root)
            return
        root = (self.tickets_dir / Path(directory).expanduser()).resolve()
        if self.values()['task_type'] == 'macro':
            if not self.macro_cases:
                self.status.set('하위 케이스 검색 후 기준 케이스의 로그를 선택하거나 공통 상대 경로를 직접 입력하세요.')
                return
            root = Path(self.macro_cases[0]['case_dir'])
        selected = self.filedialog.askopenfilenames(title='로그 파일 선택 (여러 개 선택 가능)',
                                                   parent=self.root, initialdir=str(root),
                                                   filetypes=[('모든 파일', '*'), ('로그 파일', 'log*')])
        if not selected:
            return
        try:
            names = list(dict.fromkeys(str(Path(path).resolve().relative_to(root)) for path in selected))
        except ValueError:
            self.messagebox.showerror('경로 확인', '케이스 디렉터리 안의 로그 파일을 선택하세요.', parent=self.root)
            return
        self.texts['logs'].delete('1.0', 'end')
        self.texts['logs'].insert('1.0', '\n'.join(names))

    def document(self):
        if self.scan_in_progress:
            raise ValueError('하위 케이스 검색이 끝난 뒤 저장하세요.')
        return self.service.validate(self.values(), self.filename.get(),
                                     self.current.name if self.current else None)

    def validate(self):
        try:
            data = self.document()
            self.status.set(f'검증 정상: {data["name"]}')
            return True
        except (ValueError, OSError) as exc:
            self.status.set('검증 실패 · 입력 내용을 확인하세요.')
            self.messagebox.showerror('티켓 검증 실패', str(exc), parent=self.root)
            return False

    def save(self):
        try:
            self.suggest_names()
            values = self.values()
            name = self.service.filename(values, self.filename.get())
            self.filename.set(name)
            self.document()
            destination = self.tickets_dir / name
            overwrite = False
            if destination != self.current and destination.exists():
                overwrite = self.messagebox.askyesno('티켓 덮어쓰기', f'{name}이 이미 있습니다. 덮어쓸까요?', parent=self.root)
                if not overwrite:
                    return False
            name, data = self.service.save(
                values, name, self.current.name if self.current else None,
                submit=getattr(self, 'submit_requested', False), overwrite=overwrite,
                expected_revision=self.file_revision)
            self.current = self.tickets_dir / name
            self.file_revision = self.service.revision(name)
            self.set_form(data)
            queued = data.get('queue', {}).get('submit')
            self.status.set(f'저장 완료: {name}' + (' · 봇의 큐 접수 대기' if queued else ''))
            self.refresh()
            return True
        except (ValueError, OSError) as exc:
            self.messagebox.showerror('저장 실패', str(exc), parent=self.root)
            return False

    def submit(self, mode='run'):
        if self.run_busy:
            return
        if len(self.listbox.curselection()) > 1:
            self.status.set('실행할 티켓 한 개를 선택하세요.')
            return
        if self.current is None or self.baseline != (self.filename.get(), self.values()):
            if not self.save():
                return
        from queue import Queue
        from threading import Thread
        try:
            runner = self.execution_runner()
        except (OSError, ValueError) as exc:
            self.messagebox.showerror('실행 실패', str(exc), parent=self.root)
            return
        name, revision = self.current.name, self.file_revision
        self.run_busy = True
        self.update_execution_button()
        result = Queue()
        def work():
            try:
                result.put(runner.request(name, expected_revision=revision, mode=mode))
            except Exception as exc:
                result.put(exc)
        def finish():
            if result.empty():
                self.root.after(100, finish)
                return
            self.run_busy = False
            outcome = result.get()
            if isinstance(outcome, Exception):
                self.messagebox.showerror('실행 실패', str(outcome), parent=self.root)
            else:
                message = ('이미 큐에 등록되어 있습니다.' if outcome['already_queued'] else
                           '즉시 실행 요청 완료' if mode == 'run' else '대기열 등록 완료')
                self.status.set(f'{name}: {message}')
            self.update_execution_button()
        Thread(target=work, daemon=True).start()
        self.root.after(100, finish)

    def select_all_tickets(self):
        if self.listbox.size():
            self.listbox.selection_set(0, 'end')

    def clear_ticket_selection(self):
        self.listbox.selection_clear(0, 'end')

    def open_queue_manager(self):
        if self.queue_window is not None and self.queue_window.winfo_exists():
            self.refresh_queue_manager()
            self.queue_window.lift()
            return
        try:
            from .config import load_bot
            from .storage import Store
            self.queue_config = load_bot(self.bot_config)
            self.queue_store = Store(self.queue_config['state_dir'])
        except (OSError, ValueError) as exc:
            self.messagebox.showerror('작업 큐 열기 실패', str(exc), parent=self.root)
            return
        window = self.tk.Toplevel(self.root)
        self.queue_window = window
        window.title('CFD bot 작업 큐')
        window.geometry('840x820')
        window.minsize(660, 640)
        window.transient(self.root)
        frame = self.ttk.Frame(window, padding=12)
        frame.pack(fill='both', expand=True)
        self.ttk.Label(frame, text='작업 큐와 실행 이력', font=('TkDefaultFont', 14, 'bold')).pack(anchor='w')
        self.queue_macro_status = self.tk.StringVar(value='실행 중인 매크로 없음')
        self.ttk.Label(frame, textvariable=self.queue_macro_status, wraplength=790).pack(
            anchor='w', pady=(5, 10))
        self.ttk.Label(frame, text='1. 대기 작업 선택', font=('TkDefaultFont', 11, 'bold')).pack(anchor='w')
        self.ttk.Label(frame, text='Ctrl / Shift 또는 아래 전체 선택 버튼으로 여러 작업을 선택하세요.').pack(
            anchor='w', pady=(4, 10))
        self.queue_notebook = self.ttk.Notebook(frame)
        self.queue_notebook.pack(fill='x')
        self.queue_listboxes = {}
        self.queue_jobs_by_lane = {}
        self.ttk.Button(frame, text='새로고침', command=self.refresh_queue_manager).pack(anchor='e')
        self.ttk.Label(frame, text='2. 실행 중', font=('TkDefaultFont', 11, 'bold')).pack(
            anchor='w', pady=(14, 4))
        self.queue_active_listbox = self.tk.Listbox(frame, exportselection=False, selectmode='extended', height=5)
        self.queue_active_listbox.pack(fill='x')
        active_controls = self.ttk.Frame(frame)
        active_controls.pack(fill='x', pady=(8, 0))
        self.ttk.Button(active_controls, text='전체 선택', command=self.select_all_active_jobs).pack(side='left')
        self.ttk.Button(active_controls, text='전체 해제',
                        command=lambda: self.queue_active_listbox.selection_clear(0, 'end')).pack(side='left', padx=6)
        self.ttk.Button(active_controls, text='선택 작업 중단', command=self.interrupt_active_job).pack(side='right')
        self.ttk.Label(frame, text='3. 실행 이력', font=('TkDefaultFont', 11, 'bold')).pack(
            anchor='w', pady=(14, 4))
        self.ttk.Label(frame, text='[추적 가능]으로 표시된 실행 이력에서 결과 데이터를 열 수 있습니다.').pack(
            anchor='w', pady=(0, 6))
        results = self.ttk.Frame(frame)
        results.pack(fill='both', expand=True)
        self.queue_result_listbox = self.tk.Listbox(results, exportselection=False, height=9)
        self.queue_result_listbox.pack(side='left', fill='both', expand=True)
        result_scroll = self.ttk.Scrollbar(results, command=self.queue_result_listbox.yview)
        result_scroll.pack(side='right', fill='y')
        self.queue_result_listbox.configure(yscrollcommand=result_scroll.set)
        self.queue_result_listbox.bind('<Double-Button-1>', self.open_queue_result_data)
        self.ttk.Button(frame, text='결과 요청 데이터 열기', command=self.open_queue_result_data).pack(
            anchor='e', pady=(8, 0))
        self.queue_status = self.tk.StringVar()
        self.ttk.Label(frame, textvariable=self.queue_status).pack(anchor='w', pady=(10, 0))
        self.refresh_queue_manager()

    def refresh_queue_manager(self):
        if self.queue_window is None or not self.queue_window.winfo_exists():
            return
        selected = {self.queue_jobs_by_lane[lane][index]['id']
                    for lane, box in self.queue_listboxes.items()
                    for index in box.curselection()
                    if index < len(self.queue_jobs_by_lane[lane])}
        self.queue_jobs = self.queue_store.jobs(('queued',))
        for lane in sorted({job_queue_id(job) for job in self.queue_jobs}):
            if lane in self.queue_listboxes:
                continue
            tab = self.ttk.Frame(self.queue_notebook, padding=6)
            self.queue_notebook.add(tab, text=lane)
            listing = self.ttk.Frame(tab)
            listing.pack(fill='x')
            box = self.tk.Listbox(listing, exportselection=False, selectmode='extended', height=8)
            box.pack(side='left', fill='both', expand=True)
            scroll = self.ttk.Scrollbar(listing, command=box.yview)
            scroll.pack(side='right', fill='y')
            box.configure(yscrollcommand=scroll.set)
            self.queue_listboxes[lane] = box
            self.queue_jobs_by_lane[lane] = []
            self.queue_listbox = next(iter(self.queue_listboxes.values()))
            controls = self.ttk.Frame(tab)
            controls.pack(fill='x', pady=(6, 0))
            for label, callback in [('전체 선택', self.select_all_queue),
                                    ('전체 해제', self.clear_queue_selection),
                                    ('선택 취소', self.cancel_queue_selection)]:
                self.ttk.Button(controls, text=label,
                                command=lambda q=lane, cb=callback: cb(q)).pack(side='left', padx=3)
        for lane, box in self.queue_listboxes.items():
            self.queue_jobs_by_lane[lane] = [job for job in self.queue_jobs if job_queue_id(job) == lane]
            box.delete(0, 'end')
            for index, job in enumerate(self.queue_jobs_by_lane[lane]):
                box.insert('end', f"{job_queue_id(job)} · {index + 1}. {job['case']['name']} · {job['id']} · {job['case_root']}")
                if job['id'] in selected:
                    box.selection_set(index)
        self.queue_status.set(f'대기 작업 {len(self.queue_jobs)}개')
        from .config import cases_for, tickets_for
        cases = cases_for(self.queue_config)
        self.queue_registry = tracking_registry(cases)
        jobs = self.queue_store.jobs()
        active = [job for job in jobs if job['status'] in
                  ('starting', 'running', 'postprocessing', 'stopping')]
        selected_active = {self.queue_active_jobs[i]['id'] for i in self.queue_active_listbox.curselection()
                           if i < len(self.queue_active_jobs)}
        self.queue_active_jobs = [job_view(job, self.queue_registry) for job in active]
        self.queue_active_listbox.delete(0, 'end')
        for i, item in enumerate(self.queue_active_jobs):
            cpus = item.get('actual_cpu_list') or 'CPU 배정 중'
            self.queue_active_listbox.insert(
                'end', f"{item['name']} · {item['status']} · {cpus} · {item['case_dir']}")
            if item['id'] in selected_active:
                self.queue_active_listbox.selection_set(i)
        selected_result = None
        if self.queue_result_listbox is not None and self.queue_result_listbox.curselection():
            index = self.queue_result_listbox.curselection()[0]
            if index < len(self.queue_result_jobs):
                selected_result = self.queue_result_jobs[index]['id']
        history = [job for job in jobs if job['status'] not in
                   ('queued', 'starting', 'running', 'postprocessing', 'stopping')]
        self.queue_result_jobs = [job_view(job, self.queue_registry) for job in reversed(history)][:100]
        self.queue_result_listbox.delete(0, 'end')
        for index, item in enumerate(self.queue_result_jobs):
            tracking = '추적 가능' if item['trackable'] else '티켓 없음'
            self.queue_result_listbox.insert(
                'end', f"[{tracking}] {item['name']} · {item['status']} · {item['case_dir']}")
            if item['id'] == selected_result:
                self.queue_result_listbox.selection_set(index)
        macros = running_macro_views(
            [ticket for ticket in tickets_for(self.queue_config) if ticket['task_type'] == 'macro'],
            cases, jobs, self.queue_store)
        if macros:
            summaries = []
            for macro in macros:
                remaining = self._queue_duration(macro['remaining_seconds'])
                summaries.append(f"{macro['name']}: {macro['completed']}/{macro['target']} · "
                                 f"경과 {self._queue_duration(macro['elapsed_seconds'])} · 남은 시간 {remaining}")
            self.queue_macro_status.set('실행 중인 매크로\n' + '\n'.join(summaries))
        else:
            self.queue_macro_status.set('실행 중인 매크로 없음')

    @staticmethod
    def _queue_duration(seconds):
        if seconds is None:
            return '알 수 없음'
        seconds = max(0, int(seconds))
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        return f'{hours}시간 {minutes}분' if hours else f'{minutes}분 {seconds}초'

    def select_all_active_jobs(self):
        self.queue_active_listbox.selection_clear(0, 'end')
        for i, job in enumerate(self.queue_active_jobs):
            if job['status'] in ('starting', 'running', 'postprocessing'):
                self.queue_active_listbox.selection_set(i)

    def interrupt_active_job(self):
        indexes = (self.queue_active_listbox.curselection()
                   if self.queue_active_listbox is not None else ())
        if not indexes:
            self.queue_status.set('중단할 실행 작업을 선택하세요.')
            return
        jobs = [self.queue_active_jobs[i] for i in indexes]
        names = '\n'.join(job['name'] for job in jobs[:20])
        if not self.messagebox.askyesno(
                '실행 작업 중단', f"선택한 실행 작업 {len(jobs)}개를 중단할까요?\n\n{names}",
                parent=self.queue_window):
            return
        try:
            result = interrupt_running_jobs(self.queue_store, [job['id'] for job in jobs])
            self.refresh_queue_manager()
            self.queue_status.set(f"중단 요청: {len(result['interrupted'])}개 · 이미 종료/변경 {len(result['unavailable'])}개")
        except (OSError, ValueError) as exc:
            self.messagebox.showerror('작업 중단 실패', str(exc), parent=self.queue_window)

    def open_queue_result_data(self, _event=None):
        indexes = self.queue_result_listbox.curselection() if self.queue_result_listbox is not None else ()
        if len(indexes) != 1:
            self.queue_status.set('결과 데이터를 열 작업 하나를 선택하세요.')
            return
        item = self.queue_result_jobs[indexes[0]]
        if not item['trackable']:
            self.queue_status.set('현재 조회되는 티켓 JSON이 없어 결과 데이터를 추적할 수 없습니다.')
            return
        record = self.queue_registry.get(item['case_dir'])
        if record is None:
            self.queue_status.set('티켓 목록이 변경되었습니다. 새로고침하세요.')
            return
        case = record['case']
        lines = []
        if case.get('residual_pattern'):
            try:
                paths = [entry['path'] for entry in residual_files(case)]
                lines.append('Residual · ' + case['residual_pattern'])
                lines.extend('  ' + str(path) for path in paths)
            except (OSError, ValueError) as exc:
                lines.append('Residual · ' + str(exc))
        for export in case['exports']:
            try:
                paths = export_files(case, export)
                lines.append(f"{export['name']} · {export['pattern']}")
                lines.extend('  ' + str(path) for path in paths)
            except (OSError, ValueError) as exc:
                lines.append(f"{export['name']} · {exc}")
        if not lines:
            lines.append('티켓에 Residual 또는 요청 데이터 경로가 없습니다.')
        window = self.tk.Toplevel(self.queue_window)
        window.title('결과 요청 데이터 · ' + case['name'])
        window.geometry('760x480')
        frame = self.ttk.Frame(window, padding=12)
        frame.pack(fill='both', expand=True)
        self.ttk.Label(frame, text=case['name'], font=('TkDefaultFont', 14, 'bold')).pack(anchor='w')
        self.ttk.Label(frame, text=case['_root'], wraplength=720).pack(anchor='w', pady=(3, 10))
        listing = self.tk.Listbox(frame)
        listing.pack(fill='both', expand=True)
        for line in lines:
            listing.insert('end', line)

    def select_all_queue(self, queue_id=None):
        for lane, box in self.queue_listboxes.items():
            if queue_id is not None and lane != queue_id:
                continue
            if box.size():
                box.selection_set(0, 'end')

    def clear_queue_selection(self, queue_id=None):
        for lane, box in self.queue_listboxes.items():
            if queue_id is not None and lane != queue_id:
                continue
            box.selection_clear(0, 'end')

    def cancel_queue_selection(self, queue_id=None):
        ids = [self.queue_jobs_by_lane[lane][index]['id']
               for lane, box in self.queue_listboxes.items()
               if queue_id is None or lane == queue_id
               for index in box.curselection()]
        if not ids:
            self.queue_status.set('취소할 대기 작업을 하나 이상 선택하세요.')
            return
        if not self.messagebox.askyesno('선택 작업 취소', f'선택한 대기 작업 {len(ids)}개를 취소할까요?',
                                        parent=self.queue_window):
            return
        try:
            result = cancel_queued_jobs(self.queue_store, ids)
        except (OSError, ValueError) as exc:
            self.messagebox.showerror('작업 취소 실패', str(exc), parent=self.queue_window)
            return
        self.refresh_queue_manager()
        self.queue_status.set(f"선택 취소 완료: {len(result['cancelled'])}개 · "
                              f"이미 시작/변경 {len(result['unavailable'])}개")

    def delete(self):
        names = [self.files[index].name for index in self.listbox.curselection()]
        if not names:
            self.status.set('삭제할 티켓을 목록에서 선택하세요. Ctrl / Shift로 여러 개를 선택할 수 있습니다.')
            return
        try:
            revisions = {self.current.name: self.file_revision} if self.current and self.file_revision else None
            plan = self.service.deletion_preview(names, revisions)
            text = (f'선택 {len(names)}개 · 하위 child 포함 총 {len(plan["names"])}개 티켓을 삭제할까요?\n\n'
                    + '\n'.join(plan['names'][:20])
                    + ('\n…' if len(plan['names']) > 20 else '')
                    + '\n\n케이스 폴더와 계산 결과는 유지됩니다.')
            if not self.messagebox.askyesno('티켓 삭제', text, parent=self.root):
                return
            deleted = self.service.delete_many(plan['names'], plan['revisions'])
        except (OSError, ValueError) as exc:
            self.messagebox.showerror('삭제 실패', str(exc), parent=self.root)
            return
        if self.current and self.current.name in deleted:
            self.baseline = None
            self.new()
        else:
            self.refresh()
        self.status.set(f'{len(deleted)}개 티켓 삭제 완료')

    def close(self):
        if self.confirm_switch():
            self.stop_execution_poll()
            self.root.destroy()


def launch(tickets_dir, bot_config=None):
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError('GUI 디스플레이에 연결할 수 없습니다. 데스크톱 세션에서 실행하거나 SSH X11 forwarding을 사용하세요.') from exc
    TicketEditor(root, tickets_dir, bot_config)
    root.mainloop()


def main():
    launch(Path.cwd() / 'tickets')
