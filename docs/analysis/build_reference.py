"""Rebuild source traceability and the platform sequence-diagram galleries."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[2]
DOC=ROOT/'docs'
spec=importlib.util.spec_from_file_location('flows',DOC/'diagrams/build_flows.py')
flows=importlib.util.module_from_spec(spec);spec.loader.exec_module(flows)
source_rows=[];symbols={}
class Definitions(ast.NodeVisitor):
    def __init__(self,path,source):self.path=path;self.source=source;self.scope=[]
    def visit_ClassDef(self,n):
        self.scope.append(n.name)
        for child in n.body:self.visit(child)
        self.scope.pop()
    def visit_FunctionDef(self,n):
        qualified='.'.join([self.path.stem,*self.scope,n.name]);symbols[qualified]=(self.path,n.lineno)
        params=ast.unparse(n.args)
        calls=[]
        class Calls(ast.NodeVisitor):
            def visit_FunctionDef(self,node):pass
            def visit_Lambda(self,node):pass
            def visit_Call(self,node):
                target=ast.unparse(node.func)
                if target not in calls:calls.append(target)
                self.generic_visit(node)
        visitor=Calls()
        for body in n.body:visitor.visit(body)
        source_rows.append((qualified,self.path,n.lineno,params,calls))
        self.scope.append(n.name)
        for child in n.body:self.visit(child)
        self.scope.pop()
    visit_AsyncFunctionDef=visit_FunctionDef

files=sorted((ROOT/'cfd_bot').glob('*.py'))
for path in files:
    source=path.read_text();Definitions(path,source).visit(ast.parse(source))
head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
lines=['# 함수·호출식 소스 색인','',f'기준 HEAD `{head}` + 2026-10-07 작업 트리. 실제 검토 파일 SHA-256은 [source-manifest.json](source-manifest.json)에 기록했다.','',
       '이 색인은 코드 AST의 정의·시그니처·호출식을 추출한다. `self.*` 등은 원문 그대로이며 동적 dispatch를 모두 해석한 call graph가 아니다. 사용자 요청/반환 및 순서는 [유즈케이스 그림](../lld/flows.md)과 [LLD](../LLD.md)를 기준으로 읽는다. nested function은 부모 이름으로 구분한다.','']
last=None
for qual,path,line,args,calls in source_rows:
    if path!=last:
        lines+=['## '+path.name,'','| 함수 (소스 위치) | 시그니처 | 직접 호출식 (정적 원문) |','|---|---|---|'];last=path
    args=args.replace('|','&#124;')
    calltext=', '.join('`'+c.replace('|','&#124;')+'`' for c in calls) or '없음'
    lines.append(f'| [{qual}](../../{path.relative_to(ROOT)}#L{line}) | `{args}` | {calltext} |')
(DOC/'analysis/function-index.md').write_text('\n'.join(lines)+'\n')
manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [*files,ROOT/'cfd_bot/web_static/app.js',ROOT/'bin/ofps',ROOT/'bin/cfd-web-tunnel',*sorted((ROOT/'clients').rglob('*.sh')),*sorted((ROOT/'clients').rglob('*.ps1')),ROOT/'clients/macos/launch.command']}
(DOC/'analysis/source-manifest.json').write_text(json.dumps(dict(head=head,date='2026-10-07',working_tree=True,sha256=manifest),ensure_ascii=False,indent=2)+'\n')

intro={
 'telegram':'''# Telegram LLD — 유즈케이스별 함수 요청·응답

[전체 그림 목록](flows.md) · [공용 계약](../LLD.md) · [실행 경계](../HLD.md)

<a id="entry"></a>
## 진입·thread·응답 규칙

`bot.serve → Telegram.updates(offset) → for update → Bot.handle`이 main thread에서 순차 실행된다. callback ACK는 `start_callback_ack → acknowledge_callback → Telegram.call(answerCallbackQuery)`의 별도 daemon thread다. 인증은 sender와 chat을 모두 검사한다. ACK와 실제 사용자 화면 응답은 별개다.

`TicketChat.handle`은 일반 Bot.dispatch보다 먼저 호출된다. `/tickets`·`/ticket`·`tickets`·`ticketopen`·`/cancel`, 편집 callback 및 pending field 입력을 소비하면 True를 반환한다. 나머지는 일반 dispatcher로 넘긴다. session은 `ticket-editor:<chat>:<user>`에 저장한다. render마다 token을 교체하며 다른 사용자/오래된 버튼은 거절한다.

TicketChat의 RLock은 handle과 검색 결과 반영, render의 Telegram API까지 포함한다. render는 먼저 새 token을 저장하고 editMessageText를 시도하며 TelegramError면 새 메시지로 fallback한다. 일반 Bot.send/file은 outbox를 거치지 않는다. `Bot.handle`은 일반 dispatch의 ValueError/OSError를 메시지로 바꾸고, 그 밖의 예외는 serve에서 경고 후 offset을 진행한다. 모든 실패가 사용자 오류 응답을 보장하는 것은 아니다.

<a id="routing"></a>
## 전체 callback 분기와 UC

| callback / 명령 | UC |
|---|---|
| home, /start, /help, 알 수 없는 일반 입력 | UC-01 |
| status, /stat | UC-02 |
| cases:page, case:id, /data, /cases | UC-03 |
| detail:id / residual:id / export:id:name | UC-04 / 05 / 06 |
| tickets, /tickets, /ticket, list, bulk, bulkpage, btoggle, ball, bnone | UC-07 |
| new / open, ticketopen, card / duplicate | UC-08 / 09 / 13 |
| basic, rules, scripts, scriptdefault, field, clear, event | UC-10 |
| validate / review, save | UC-11 / 12 |
| delete, breview, deleteyes, bdelete | UC-14 |
| scan, stopscan / members, remove | UC-15 / 16 |
| queue(편집 화면), mode, setmode, role, execsource, cpupolicy, toggle | UC-17 |
| runstate, runreview, runyes / prepare:cid, enqueue:cid | UC-18 / UC-18-legacy |
| /queue, queue(일반 dispatcher), pause, resume | UC-19 / 20 |
| qselect,qback,qpage,qtoggle,qall,qnone,qcancel,qcancelyes,cancel:id | UC-21 |
| templates,template,savetemplate | UC-22 |
| browse,bp,bd,bup,bf,bapply,bcancel; exports,xe,xnew,xkind,xapply,xdelete | UC-23 / UC-10 exports |
| /cancel,backinput,discard,switch | UC-24 |
| clean,/clean | UC-25 |

`te:<token>:queue`는 실행 설정 화면이고 일반 `queue`는 실제 DB 큐다. 동일 단어라도 처리 함수가 다르다. 그림에는 action 조건을 표시한다. 논리적 UC가 확인 버튼까지 여러 update를 필요로 하면 각각의 action 분기를 시간 순서로 나열한다.
''',
 'gui':'''# cfd-ticket-gui LLD — 유즈케이스별 함수 요청·응답

[전체 그림 목록](flows.md) · [공용 계약](../LLD.md)

<a id="entry"></a>
## 진입·thread·응답 규칙

`cli.main(gui) → gui.launch → Tk → TicketEditor → root.mainloop` 또는 GUI entrypoint로 시작한다. 일반 편집·열기·검증·저장·삭제·큐 조회는 Tk main thread에서 동기 실행된다. 저장 반환 bool이 화면 전환 허용 여부를 결정한다. 파일/입력 오류는 messagebox, 일반 결과는 status/위젯에 반영한다.

`scan_cases`와 `submit`은 worker thread → Queue → `root.after(100,finish)`로 결과를 전달한다. 작업 thread에서 Tk widget을 직접 갱신하지 않는다. `poll_execution`은 1초 후 다시 예약하고 cached `TicketRunner.state`로 버튼만 갱신한다. 이는 ofps fresh scan 주기가 아니다.

전체 CASE 대시보드(UC-02), 큐 pause/resume(UC-20), Telegram clean(UC-25), 파일 preview/다운로드는 N/A다. 결과 창은 현재 티켓으로 추적 가능한 DB 작업의 파일 경로만 보여준다. macro 행 삭제는 있지만 순서 이동 버튼은 없다. 공용 서비스에 기능이 있다는 이유로 GUI 버튼이 있다고 간주하지 않는다.

| 사용자 이벤트 | 함수 |
|---|---|
| 파일 목록/선택 | refresh, open_selected, select_all_tickets, clear_ticket_selection |
| 새 초안/복제/이동/종료 | new, duplicate, confirm_switch, close |
| 필드와 실행 탭 | values, set_form, task_type_changed, update_execution_visibility, refresh_end_default |
| 파일 선택·export 편집 | choose_case, choose_logs, choose_residual, edit_item, remove_item, refresh_tables |
| 검색·멤버 제거 | scan_cases, render_case_rows, remove_case_row |
| 검증·저장·실행 | document, validate, save, submit, execution_runner, update_execution_button |
| 큐·결과 | open_queue_manager, refresh_queue_manager, open_queue_result_data |
| 대기 취소 | select_all_queue, clear_queue_selection, cancel_queue_selection |
| 템플릿 | refresh_patterns, apply_pattern, save_pattern |

실제 display 기반 테스트가 환경에서 생략되는 경우 [검증 결과](../analysis/validation.md)에 표시한다.
''',
 'web':'''# Web LLD — 브라우저/HTTP별 함수 요청·응답

[전체 그림 목록](flows.md) · [공용 계약](../LLD.md)

<a id="entry"></a>
## 진입·HTTP·화면 응답 규칙

`app.js:start → api(bootstrap) → api(cases) → render → refresh → 10초 timer`다. visible 상태이며 S.busy가 아닐 때 자동 갱신한다. refresh의 S.refreshing은 같은 탭 중복만 막는다. click은 S.busy/content.inert로 조작을 제한한다. input은 draft만 변경하며 API mutation과 다르다.

`Handler.do_GET/do_POST → handle_request → trusted → WebApp.get/post → respond → wfile.write`다. 각 HTTP 요청은 별도 thread지만 snapshot lock은 같은 web process 안에서 공유한다. `/api/file`만 Handler가 `WebApp.file`을 직접 호출한다. 다운로드 ZIP과 정적 파일은 allowlist로 제공한다.

| HTTP | 실제 분기 / 반환 | UC |
|---|---|---|
| GET /api/health | app/version/hostname | 27 |
| GET /api/bootstrap | csrf/default_directory/patterns | 01 |
| GET /api/overview | fresh + sync + live/tickets/queue/history/macros | 02,07,19 |
| GET /api/ticket | TicketService.open + cached state + postprocessed | 09 |
| GET /api/cases | case_rows | 03 |
| GET /api/detail | log/control/ETA/latest/history | 04 |
| GET /api/artifacts | groups/files/URL | 05,06 |
| GET /api/file | binary inline/attachment; 선언된 파일 재검증 | 05,06 |
| GET /api/browse /control | 목록(최대 1000) / control_times | 23,10 |
| POST /api/new /duplicate | 새 Draft / 복제 Draft | 08,13 |
| POST /api/validate /save | {valid:true} / 저장 후 Draft | 11,12 |
| POST /api/exports/validate | 정규화 Export | 10,11 |
| POST /api/delete/preview /delete | Plan / deleted names | 14 |
| POST /api/run | RunResult | 18 |
| POST /api/discover | cases/skipped/postprocessed | 15 |
| POST /api/patterns | 갱신 templates | 22 |
| POST /api/queue | pause/resume {ok:true}; cancel CancelResult | 20,21 |

403=Host/Origin/Fetch-Site/CSRF, 413=body 크기, 400=입력 타입·ValueError/OSError, 404=LookupError, 500=그 밖 예외다. browser.api는 non-2xx에서 Error를 던지고 click handler가 toast로 표시한다. overview 수집 실패는 HTTP 200+error와 이전 snapshot을 반환할 수 있다. refresh는 오류 표시를 유지한다. 클라이언트 fetch 자체에 timeout/AbortController는 없다. 서버 socket timeout 60초가 모든 domain 연산을 60초 안에 중단하는 것은 아니다.

stableOverview는 동일 실행의 일시적으로 사라진 ETA/progress를 유지한다. shape가 같으면 updateOverview가 기존 DOM을 수정한다. form은 polling 때문에 재생성하지 않는다. loadData는 detail/artifacts를 Promise.all로 요청하고 현재 선택 ID가 바뀌었으면 이전 응답을 버린다.
''',
 'runtime':'''# Runtime LLD — CLI·scanner·백그라운드 요청·응답

[전체 그림 목록](flows.md) · [공용 계약](../LLD.md) · [HLD 실행 단위](../HLD.md)

<a id="cli"></a>
## CLI 분기

`__main__ → cli.main(argv) → parser`에서 분기한다. `_worker`와 `gui`는 일반 config/store 경로 전에 처리한다. check는 load_bot/cases_for(force=True) 전역 검증 후 stdout과 0을 반환한다. `--execution-env`는 깨끗한 환경에서 OpenFOAM 도구 -help를 확인하며, `--mpi-probe`는 명시한 경우에만 실행한다. 이번 문서 검증은 기본 check만 사용했다.

identify는 getMe/getUpdates로 식별 정보를 읽는다. status는 fresh scan+sync, --json이면 jobs/observed/estimate를 직렬화한다. enqueue는 Store.enqueue 직접 호출이며 TicketRunner.request가 아니다. cancel은 공용 cancel_queued_jobs다. pause/resume은 kv 플래그다. monitor/serve는 DaemonLock으로 같은 state의 중복 daemon을 거절한다. monitor --once도 Scheduler를 호출하므로 운영 환경에서 안전한 읽기 전용 benchmark 명령이 아니다.

<a id="launcher"></a>
## 접속 launcher

Windows `Start CFD.cmd → cfd-client.ps1 → Get-SshAliases`와 macOS `CFDControlRoom → Terminal launch.command → ssh_config_hosts`가 Host 목록을 읽는다. Include는 깊이 제한·중복 방지로 처리하며 wildcard/부정 Host는 선택 목록에서 제외한다. OpenSSH가 User/Port/IdentityFile/ProxyJump를 해석한다. loopback forwarding과 포트 열림 확인 뒤 브라우저를 연다. HTTP 애플리케이션이 정상이라는 확인과 단순 TCP 포트 확인은 다르다. 연결 창 종료 시 자신이 만든 SSH PID만 정리한다. `bin/cfd-web-tunnel`은 목적지/포트를 검증한 뒤 exec ssh 한다.

## 실행·상태 전이

`queued → starting → running(phase=preprocess/solver) → postprocessing? → succeeded/failed`. 취소는 queued에서만 cancelled다. 외부 observed는 running에서 missing 임계값 이후 수치 판정으로 이동한다. `interrupted`는 저장/표시 계약에 있지만 현재 decide는 일반적으로 succeeded/failed를 반환한다.

Monitor.tick은 ticket_index → fresh snapshot → accept_submissions → recover → managed 관측 보강 → 현재 roots ∪ 영속 tracked roots 감시 → Scheduler.tick → 증분 sync_ticket_states 순서다. 등록 전체 CASE를 observe하지 않는다. Scheduler.tick 내부도 recover를 호출한다. Monitor 한 주기에서 recover가 두 번 실행되는 점은 현행 코드 그대로다.

Scheduler는 active child가 없는 즉시 실행 batch마다 첫 queued child 하나만 보고, 이어서 active job이 없는 `queue_id`별 FIFO 선두를 각각 검토한다. 일반 head의 실제 NP와 선택적 monitor가 quota 크기이며 Scheduler가 겹치지 않는 CPU 위치를 자동 배정한다. 동적 macro는 `priority=run|queue` 모두 현재 child NP만큼 미예약 CPU를 먼저 쓰고 부족분은 작은 quota donor부터 drain한다. 뒤 child는 앞 child가 끝나기 전 candidate나 drain claim을 얻지 않는다. donor 작업은 자연 종료하고, 동적 작업 뒤 donor별 FIFO head에 한 번씩 우선권을 준다. automatic/monitor opt-in이면 추가 fresh snapshot, solver CPU check, monitor면 추가 CPU check를 수행한다. check_cpus는 SNAPSHOT_LOCK을 사용하지 않는다. worker는 fresh scan 자체를 주기적으로 하지 않고 로그를 0.5초 간격으로 읽는다.

worker의 .process-core 반영 → 전처리 → 로그 cursor 수집 → solver (+ opt-in monitor) → 최종 log drain → decide → 성공 시 후처리 → artifact freeze → event payload 저장 → terminal state 저장 순서를 지킨다. monitor 종료는 최대 30초 기다린 뒤 정리하고 monitor/postprocess 오류는 solver verdict와 별도 기록한다. terminal state 전에 파일을 freeze한다. worker는 종료 payload를 kv에 저장하며 이후 Scheduler.terminal_event가 outbox에 넣는다.

Delivery는 한 batch 최대 10개 recipient row를 순차 처리한다. message_index/file_index를 각 성공 뒤 저장하고 실패는 retry_after+backoff로 재시도한다. API 성공 직후 checkpoint 전 crash는 재전송될 수 있다. API 응답 소요가 느리면 같은 batch 뒤 recipient도 기다린다.
''',
 'domain':'''# 공용 도메인 LLD — 함수 요청·응답 상세

[유즈케이스 그림](flows.md)에서 연결되는 공용 내부 시퀀스다. 각 함수의 타입·예외·저장 효과는 [LLD 계약](../LLD.md)에 정리했다. 실제 외부 ofps 실행과 Telegram API 호출은 플랫폼 그림에서도 생략하지 않는다.
'''
}
uc_tests={2:'test_core.py / test_web.py',5:'test_residual.py',6:'test_web.py / test_core.py',11:'test_gui.py',12:'test_ticket_chat.py / test_gui.py / test_web.py',14:'test_ticket_chat.py',15:'test_queue_tickets.py',16:'test_queue_tickets.py',17:'test_execution_environment.py / test_gui.py / test_named_queues.py',18:'test_ticket_run.py / test_named_queues.py',19:'test_run_views.py / test_named_queues.py',20:'test_core.py',21:'test_core.py / test_web.py',22:'test_gui.py',23:'test_ticket_chat.py / test_web.py',24:'test_ticket_chat.py / test_gui.py',25:'test_core.py',26:'test_clients.py / test_web_tunnel.py'}

def page_for(c):
    key=c['key']
    if key.startswith('D-'):return 'domain'
    if key.endswith('-tg'):return 'telegram'
    if key.endswith('-gui'):return 'gui'
    if key.endswith('-web'):return 'web'
    return 'runtime'

def names(n):
    yield n['name']
    for child in n['children']:yield from names(child)

for page in intro:
    items=[c for c in flows.charts if page_for(c)==page]
    lines=[intro[page]]
    lines += ['\n내부 반복·파일 접근·잠금 범위는 [catalog LLD](../LLD.md#catalog), 현재 921행 매크로의 함수별 시간과 큐 응답량은 [운영 데이터 분석](../analysis/live-bottlenecks.md)에 있다. 그림의 보라색 loop는 함수 내부 반복이며 추가 함수가 아니다.\n']
    lines += ['\n## 그림 바로가기\n']
    for c in items:
        anchor=re.sub(r'-(tg|gui|web)$','',c['key']).lower()
        lines.append(f'- [{c["key"]} — {c["title"]}](#{anchor})')
    for c in items:
        key=c['key'];anchor=re.sub(r'-(tg|gui|web)$','',key).lower()
        lines += ['',f'<a id="{anchor}"></a>',f'## {key} — {c["title"]}','',f'진입: `{c["trigger"]}`.','',f'![{key} 함수 요청·응답](../diagrams/{key}.svg)','',f'[SVG 원본 확대](../diagrams/{key}.svg)','',f'**정상 결과:** {c["outcome"]}.',f'**실패/취소:** {c["error"]}.','']
        details={
            'D-01':'정규화된 전체 경로의 공용 색인을 사용한다. 최초·check·변경 감지 손실은 전역 검증하고, 이후 변경 JSON만 재검증한다. macro membership은 set 조회다. [반복 횟수·예외·개선 조건](../LLD.md#catalog).',
            'D-13':'queued 전체의 history/ETA를 준비하고 본문도 전부 생성한다. 취소 버튼 20개 제한은 본문 제한이 아니다. [연결 횟수와 35개 메시지 분할 근거](../analysis/live-bottlenecks.md).',
            'BG-02':'감시 대상은 현재 CASE와 이전 실행·종료 확인 중 CASE다. DB 변화는 ticket_changes journal을 통해 대상 티켓과 부모만 반영한다. run_once가 끝난 뒤 5초 대기하므로 5초 고정 주기가 아니다. [호출별 반복 표](../LLD.md#catalog).',
            'BG-04':'`scheduling_candidates(jobs,active)`가 active child가 없는 즉시 실행 batch마다 첫 queued child 하나만 고르고, active job이 없는 queue id만 `queue_heads`에 넘긴다. `_assign_queue_profiles`는 일반 head NP에서 quota와 CPU 위치를 정한다. `borrowing_plan`은 `priority=run|queue` 동적 현재 child의 NP와 donor를 계산한다. drain claim은 하나이며 동적 작업 뒤 donor별 다음 head에 1회 우선권을 준다.',
            'UC-02-tg':'ofps 실행 시간 제한 45초는 뒤의 catalog/membership 시간 제한이 아니다. 현재 표본은 scan 약 1.06초, membership만 16.56~19.68초. [측정 범위](../analysis/live-bottlenecks.md).',
            'UC-19-tg':'전체 큐 본문은 별도 페이지 제한 없이 순차 sendMessage한다. 마지막 조각에만 keyboard가 붙는다. [큐 내부 상세](../diagrams/D-13.svg).',
        }
        if key in details:lines+=['**내부 로직·비용:** '+details[key],'']
        refs=[]
        for name in dict.fromkeys(names(c['root'])):
            if name in symbols:
                path,line=symbols[name];refs.append(f'[{name}](../../{path.relative_to(ROOT)}#L{line})')
        lines += ['**코드 연결:** '+', '.join(refs)+'.','']
        if key.startswith('UC-'):
            number=int(key[3:5]);tests=uc_tests.get(number,'test_ticket_chat.py / test_gui.py / test_web.py')
        else:tests='test_core.py / test_queue_tickets.py / test_scripts.py'
        lines+=['**관련 검증:** '+', '.join(f'[{name.strip()}](../../tests/{name.strip()})' for name in tests.split('/'))+'.','']
    (DOC/'lld'/f'{page}.md').write_text('\n'.join(lines).rstrip()+'\n')

lines=['# 전체 유즈케이스 × 플랫폼 — 함수 요청·응답 그림','',
       '각 링크는 **가로 participant·세로 lifeline·함수 요청/반환 화살표**로 그린 SVG다. 각 플랫폼 LLD에는 같은 그림과 함수 소스 링크를 함께 삽입했다. `ofps` 실행과 외부 Telegram API 경계도 그림 안에서 표시한다.','',
       '[Telegram 전체](telegram.md) · [GUI 전체](gui.md) · [Web 전체](web.md) · [Runtime 전체](runtime.md) · [공용 함수 상세](domain.md)','',
       '| UC | 사용자 동작 | Telegram | GUI | Web | 운영/접속 |','|---|---|---|---|---|---|']
for i in range(1,28):
    prefix=f'UC-{i:02}';group=[c for c in flows.charts if c['key'].startswith(prefix)]
    cells=[]
    for suffix in ('tg','gui','web','runtime'):
        matches=[c for c in group if (c['key'].endswith('-'+suffix) if suffix!='runtime' else page_for(c)=='runtime')]
        cells.append(' / '.join(f'[{c["key"]}](../diagrams/{c["key"]}.svg)' for c in matches) or 'N/A¹')
    lines.append('| '+prefix+' | '+group[0]['title']+' | '+' | '.join(cells)+' |')
lines+=['','¹ N/A의 구체적 이유와 부분 지원 범위는 [ARCHITECTURE 플랫폼 표](../ARCHITECTURE.md#2-전체-사용자-유즈케이스--플랫폼)에 있다. GUI의 데이터 조회는 경로 표시이며 다운로드가 아니다. Telegram legacy 실행은 별도 그림으로 분리했다.','',
        '## 백그라운드와 공용 함수','', '| ID | 호출/응답 그림 |','|---|---|']
for c in flows.charts:
    if c['key'].startswith(('D-','BG-')):lines.append(f'| {c["key"]} | [{c["title"]}](../diagrams/{c["key"]}.svg) |')
lines+=['','## 소스와 재생성','', '그림 원본은 [build_flows.py](../diagrams/build_flows.py)의 함수 호출/반환 명세다. 생성된 SVG를 직접 수정하지 않고 명세를 고친다. 외부 폰트/CDN/이미지/JavaScript에 의존하지 않는다.','', '```bash','python3 docs/diagrams/build_flows.py','python3 docs/analysis/build_reference.py','```','',f'현재 {len(flows.charts)}개 SVG. [코드 함수 색인](../analysis/function-index.md), [검증 결과](../analysis/validation.md).']
(DOC/'lld/flows.md').write_text('\n'.join(lines).rstrip()+'\n')
print('Generated reference for',len(symbols),'Python callables;',len(flows.charts),'sequence diagrams linked in galleries')
