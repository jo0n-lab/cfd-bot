"""Regenerate documentation-only function request/response flowcharts (standalone SVG)."""
from pathlib import Path
import json
import subprocess
import textwrap

HERE = Path(__file__).resolve().parent
# A node is a real callable; children are calls made by that callable.
# Calls are numbered locally. Conditional calls explicitly name their condition.
def fn(name, request='', response='None', *children, note='', detail=''):
    return dict(name=name, request=request, response=response, children=list(children), note=note, detail=detail)

def loop(label, *children):
    # A control-flow frame, not an invented product function.
    return dict(name='', request='', response='', children=list(children), note='', detail='', loop=label)

def service(name, request='', response='None', detail=''):
    return fn('editor.TicketService.'+name, request, response, detail=detail)

def runner(name='state', request='name, fresh=False', response='RunState'):
    return fn('ticket_run.TicketRunner.'+name, request, response, detail='D-03' if name=='request' else '')

def db(name, request='', response='None'):
    return fn('storage.Store.'+name, request, response)

def snap():
    return fn('processes.snapshot', 'ofps_command', '{raw, cases}', detail='D-04')

def tg_send():
    return fn('bot.Bot.send', 'chat, text, keyboard', 'list[Message]',
              fn('telegram.Telegram.send','chat, text, keyboard','list[Message]',
                 fn('telegram.Telegram.call','sendMessage; 30 s/call','Message',note='문자열 분할마다 동기 네트워크')),
              fn('bot.Bot._remember','chat, result','None',db('remember_message','chat, message_id, date')))

def panel():
    return fn('ticket_chat.TicketChat.render','chat, user, session, text, rows, view','None',
              fn('ticket_chat.TicketChat.persist','새 token / view','None',db('put','session key, session')),
              fn('telegram.Telegram.call','panel 존재: editMessageText','Message',note='실패하면 Bot.send로 새 메시지'),
              fn('bot.Bot.send','panel 없거나 edit 실패: text, markup','list[Message]'),note='TicketChat RLock 안의 동기 전송')

def chart(key,title,platform,trigger,root,outcome,error='ValueError/OSError → UI 오류; 세부 catch는 플랫폼 LLD 참조'):
    charts.append(dict(key=key,title=title,platform=platform,trigger=trigger,root=root,outcome=outcome,error=error))

def tg(uc,title,trigger,*calls,editor=False,error=None):
    if editor:
        handler=fn('ticket_chat.TicketChat.handle','update','bool: consumed',
                   fn('ticket_chat.TicketChat.action','chat, user, session, op, arg','None',*calls,
                      note='token 확인 후 op 분기; RLock 유지'),note='진입/입력 명령은 handle에서 직접 분기')
    else:
        handler=fn('bot.Bot.dispatch','chat, action, request_key, user','None',*calls)
    root=fn('bot.Bot.handle','update','None',handler,note='sender/chat 인증; callback ACK는 별도 thread')
    chart(f'{uc}-tg',title,'Telegram',trigger,root,'메시지/패널 반영 → handle 반환 → serve가 offset 저장',error or '입력/파일 오류 → 오류 메시지; TelegramError 등은 serve 경고 후 offset 진행')

def gui(uc,title,method,*calls,note='',outcome='Tk status / widget / dialog 반영',error='ValueError/OSError → messagebox; 초안 유지'):
    chart(f'{uc}-gui',title,'cfd-ticket-gui',f'Tk event → {method}',fn('gui.TicketEditor.'+method,'event 또는 현재 폼',{'values':'FormValues','save':'bool','validate':'bool','confirm_switch':'bool'}.get(method,'None'),*calls,note=note),outcome,error)

def web(uc,title,js,path,*calls,post=False,note='',outcome='JSON → api Promise → 해당 DOM / toast / modal 반영'):
    handler=fn('web.WebApp.post' if post else 'web.WebApp.get',('path, data' if post else 'path, query'),'JSON 직렬화 값',*calls)
    root=fn('web_static.app.js:'+js,'사용자 event / polling','Promise / UI 상태',
            fn('web_static.app.js:api',path,'parsed JSON',
               fn('web.Handler.handle_request','POST' if post else 'GET','HTTP response',handler,
                  note='Host/Origin 검사; POST는 CSRF/JSON/2 MiB 검사')),note=note)
    chart(f'{uc}-web',title,'web',js+' → '+path,root,outcome,'403 권한/출처; 400 입력/파일; 404 경로; 500 기타 → api throw → toast/오류 화면')

charts=[]
# Shared domains. Each function box can link here from platform charts.
chart('D-01','공용 티켓 색인 · 변경된 JSON 검증','공용','cases_for / tickets_for / ticket_index',
 fn('catalog.ticket_index','config; check만 force=True','TicketIndex',
    fn('catalog.TicketIndex.refresh','force=False','검증된 index',
       fn('tickets.ticket_lock','정렬한 ticket folders','context; 재진입 가능'),
       fn('catalog.FolderWatch.drain','','(changed paths, trust_lost)',note='Linux inotify; unsupported/dynamic glob은 metadata 비교'),
       loop('최초 / check / 이벤트 손실: 전체, 그 외 변경 파일만',
            fn('config.load_case','대상 JSON','검증된 Ticket'),fn('config.read_json','대상 JSON','raw document / revision')),
       fn('catalog.active_cases','cached tickets','Case[]',note='macro별 (ticket full path, root) set; child membership O(1). 변경 없는 refresh는 이 단계 생략')),
    fn('catalog.TicketIndex.cases','roots=None 또는 현재 snapshot roots','독립된 Case 사본',note='lookup(root)은 전체 경로 key로 단일 티켓 조회')),
 'cold rebuild / 변경분 재검증; warm 조회는 JSON load_case 0회','잘못된 등록 문서는 fail closed; 편집용 folder_index는 invalid entry를 별도 표시')
chart('D-02','저장·revision·원자적 파일 반영','공용','TicketService.save(values, ...)',
 service('save','values, name, current, submit, overwrite, expected_revision, request_id','(name, document)',
         detail=''), '저장된 document; 실행 제출은 submit 호출 인자로만 결정','revision/중복/실행 설정 guard 실패 → ValueError; 파일 실패 → OSError')
charts[-1]['root']['children']=[fn('tickets.ticket_lock','tickets folder','context exit',note='이하 검증·쓰기 전체에 유지'),service('revision','current 존재 및 expected_revision','SHA-256'),service('validate','values, name, current','document',detail='D-11'),fn('tickets.publish_macro','macro: destination, data, original','macro document',detail='D-09'),fn('tickets.atomic_json','single: destination (+ child parent), data','None',note='temp → flush/fsync → replace; 이름 변경 시 원본 unlink')]
charts[-1]['root']['note']='request_id 중복이면 기존 결과 반환. single running: script/execution 변경 금지'
chart('D-03','공용 실행 요청','공용','TicketRunner.request(name, revision, request_id, mode)',
 fn('ticket_run.TicketRunner.request','name, expected_revision, request_id, mode=run|queue','{already_queued, request_id, count?, mode, queue_id}',
    fn('ticket_run.TicketRunner._snapshot','config','Snapshot',snap(),db('put','snapshot, current'),note='flock 이전; scan 실패를 ValueError로 변환'),
    fn('tickets.ticket_lock','service.folder','context exit',note='이하 멤버·상태·revision 검사와 JSON 쓰기 동안 유지'),
    fn('ticket_run.TicketRunner._members','name','(Ticket, list[Case])',fn('config.load_case','ticket 및 child paths','Ticket')),
    service('revision','expected_revision 있을 때 name','SHA-256'),
    fn('ticket_run.TicketRunner._state','ticket, members, observed','RunState',db('jobs','LIVE + queued','list[Job]'),db('get','각 observed:root','Observed|None'),fn('ticket_run.TicketRunner._capacity','members, observed, active','head required + max required',note='동적 macro: 첫 child만 즉시 실행 판정; 전체 최대 NP는 51-core 한도 검사'),note='running 거절; queued/동일 request_id는 already_queued'),
    fn('tickets.atomic_json','child들, 마지막 부모 queue.submit=True + mode/queue profile','None',note='backup 후 파일별 교체; OSError rollback')),
 '동적 macro는 첫 child가 가용하면 즉시 제출; Monitor.accept_submissions가 나중에 DB 큐 접수','scan/revision/running/command/member 오류 또는 child 최대 NP가 관리 한도 초과 → ValueError; 쓰기 실패 → rollback')
chart('D-04','fresh snapshot과 lock 대기','공용','processes.snapshot(command)',
 fn('processes.snapshot','list[str] command','{raw: str, cases: dict}',
    fn('subprocess.run','command, env=managed_scan_environment(), timeout=45','CompletedProcess',note='SNAPSHOT_LOCK 안: 대기 시간 제한 없음; run 시간만 45초'),
    fn('processes.parse_snapshot','stdout; lock 해제 후','dict[root, ProcessRecord]',
       fn('processes.identity','PID','boot:pid:starttick | None'),
       fn('processes.owner_label','supervisor 또는 첫 PID','str',note='/proc 조상 최대 64단계, 환경/TTY 확인'),
       fn('processes.cpu_layout','같은 root의 solver + Monitor processes','(합집합 core_count, cpu_ranges)')),
    note='returncode/빈 stdout/형식/CASE 파싱 결과 검사'),
 '호출 시점 모든 CASE; 티켓 상태 동기화는 수행하지 않음','TimeoutExpired/OSError/RuntimeError → caller; caller별 오류 처리 다름')
chart('D-05','삭제 preview와 최종 삭제','공용','deletion_preview → 사용자 확인 → delete_many',
 service('delete_many','names, expected_revisions','list[deleted name]'),
 '티켓 JSON만 삭제; 케이스/결과 유지','busy macro/child/running/queued/revision 충돌은 쓰기 전 거절; OSError rollback')
charts[-1]['root']['children']=[fn('tickets.ticket_lock','folder','context exit'),service('_deletion_plan','names, revisions','dict[name, document]'),fn('pathlib.Path.unlink','검사 완료된 각 ticket','None',note='모든 backup 확보 후 삭제; 실패 시 제거 파일 복구')]
charts[-1]['root']['note']='preview도 동일 _deletion_plan + revision 사용; 확인 뒤 최종 plan 재검사'
chart('D-06','직계 하위 케이스 검색','공용','discover_cases(root, observed, end, include, exclude)',
 fn('tickets.discover_cases','root, observed cases, end_time?, include[], exclude[]','(rows, skipped)',
    fn('config.glob_patterns','include/exclude','None: 유효; 실패 ConfigError'),
    fn('control.control_times','각 후보 case, end_time','limits|None'),
    fn('tickets.postprocess_time','candidate root','float|None'),
    fn('tickets.checkpoint_time','candidate root','float'),note='직계 디렉터리+Allrun; hidden/symlink/template 제외; exclude 우선; observed 실행 제외'),
 '정렬된 후보 rows와 제외 이유; 완주 케이스도 선택 가능','root/패턴/파일 오류 → caller; 깊은 재귀 case 탐색 없음')
chart('D-07','로그·진행률·ETA','공용','recent_case_log / estimate',
 fn('logs.recent_case_log','case, state?, final=False','(telemetry, Path)',
    fn('logs.select_case_log','case','Path|None',note='configured logs의 mtime/순서'),
    fn('logs.recent_log','path, state, final, watcher','telemetry',
       fn('logs.read_log','cursor / bounded tail','telemetry',fn('logs.feed','각 완전한 log line','None; state 변경'))),note='단계/rotation 변경 시 cursor/rate 초기화'),
 'caller가 estimate(case, telemetry, elapsed, history)로 ETA 산출','missing/실제 backlog는 live rate 보류; 최종 판정은 D-12')
chart('D-08','다중 대기 취소','공용','cancel_queued_jobs(store, ids)',
 fn('queue_control.cancel_queued_jobs','store, ids, ui?','{cancelled: ids, unavailable: ids}',
    db('cancel_queued','중복 제거된 ids','cancelled/unavailable'),note='Store.cancel_queued: 한 BEGIN IMMEDIATE에서 상태 재검사; queued만 변경'),
 '시작되었거나 사라진 ID는 unavailable; solver kill 없음','빈/잘못된 선택 ValueError; SQLite 실패 caller로 전달')
chart('D-09','매크로 발행·멤버 재구성','공용','publish_macro(path, data, previous, submit)',
 fn('tickets.publish_macro','path, data, previous, request_id, submit, locked','macro document',
    fn('config.load_case','folder의 다른 티켓 전체','Ticket'),
    fn('tickets.clone_document','선택 row별 root, common data','child document'),
    fn('tickets.atomic_json','staged children → 마지막 macro','None'),
    fn('config.load_case','각 방금 쓴 파일','검증된 Ticket'),
    note='running child 보호; 바쁜 macro 순서 변경 거절; 분리 child→alone; backup/rollback'),
 'submit 호출값에 따라 저장과 실행 제출을 분리; 기존 편집은 제출 상태 유지','검증 실패/쓰기 실패 → 원본 복원; 다중 파일 crash-atomic 보장은 아님')
chart('D-10','선언된 artifact 찾기','공용','residual_files / export_files',
 fn('artifacts.residual_files','case, folder?, since?','list[FileItem], 최대 1',
    fn('artifacts.export_files','case, pattern, max_files=1, since','list[Path]',
       fn('config.inside','root, relative match','safe resolved Path'),
       note='glob 전체 수집 → mtime 정렬 → max_files 절단'),
    note='PNG/49 MiB 확인; folder 지정 시 copy2'),
 'Residual 파일 항목; export 요청은 export_files 직접 호출','path/symlink/확장자/크기 실패 ValueError; 파일 없음은 []')
chart('D-11','폼 변환과 티켓 검증','공용','TicketService.validate(values, name, current)',
 service('validate','values, name, current','document'),
 '정규화할 JSON 원문; 저장은 수행하지 않음','ConfigError/ValueError/OSError; validation .tmp는 finally 삭제')
charts[-1]['root']['children']=[fn('editor.form_document','FormValues','document',note='필드 변환·execution source·monitor opt-in·자동 exports 끄기'),fn('editor.validate_document','json text, tickets_dir','document',fn('config.load_case','숨겨진 .tmp 파일','검증된 Ticket'),note='임시 파일 실제 생성/삭제; *.json catalog에 미노출'),fn('catalog.folder_index','동일 type/root 중복 검사: cached documents','TicketIndex',detail='D-01')]
chart('D-12','수치 기반 종료 판정','공용','outcomes.decide(case, telemetry, started, ...)',
 fn('outcomes.decide','case, telemetry, started, returncode?, log_fresh','(status, reason)',
    fn('outcomes._fresh','failure.updated_files 각각','bool'),
    fn('control.control_times','case','{start,end,stop_at}|None'),
    note='nonzero → fresh failure → stale log → stopAt → finite Time >= end 순서'),
 'succeeded 또는 failed; End 문자열만으로 성공하지 않음','코드상 end_time override 존재; AGENTS 원칙과 차이를 ARCHITECTURE에 기록')
chart('D-13','큐 전체 ETA 조회 · 대량 응답 생성','Telegram 공용 report','report.queue_text(store, enabled)',
 fn('report.queue_text','store, enabled, ui','전체 큐 문자열',
    db('jobs','queued + LIVE; SQL limit 없음','Job[]'),
    loop('비 queued LIVE job마다: 먼저 전체 대기 시간 추정',
         db('runtime_history','job.case, actual_cores','samples',),
         fn('logs.estimate','case, telemetry, elapsed, history','Estimate')),
    loop('전체 jobs 재순회: queued job 각각, 화면 page 제한 없음',
         fn('storage.Store.runtime_history','case','samples',
            db('connect','history + succeeded jobs SQL','DB context'),
            fn('storage.Store.get','observed:root','Observed|None',db('connect','observed SELECT용 별도 연결','DB context')),
            note='runtime_history 직접 연결 + get 내부 연결: job당 SQLite 2회'),
         fn('logs.estimate','case, {}, 0, history','Estimate',note='controlDict 등 추가 파일 읽기 가능; 각 queued 행 생성. 버튼 20개 제한은 본문에 적용 안 됨'))),
 'caller가 macro 요약을 붙여 Bot.send → chunks → 순차 sendMessage; 관측 본문만 35조각',
 '생성/전송 오류는 caller로 전파; 중간 sendMessage 실패 시 이후 조각 전송 안 됨')
chart('D-14','매크로 진행 ETA · 큐 본문과 중복 조회','공용 run_views','running_macro_views(macros,cases,jobs,store)',
 fn('run_views.running_macro_views','macros, cases, jobs, store, now','MacroView[]',
    fn('statistics.median','현재 batch 성공 duration 최근 5개; 있을 때만','batch_expected',note='현재 row job_id로 jobs를 연결하고 completed/failed 집계'),
    loop('running macro의 미완료 row 각각; job/Case 없으면 미정, remaining_known=False이면 후속 queued 생략',
         fn('run_views._remaining','case, job, store, now','(prediction, remaining)',
            fn('logs.recent_case_log','job.status=running일 때만 case','(telemetry, path)'),
            fn('logs.estimate','case, telemetry, elapsed; 이력 없이','Estimate'),
            db('runtime_history','basis=unknown일 때만 case, actual_cores','samples'),
            fn('logs.estimate','basis=unknown이면 history 포함 재호출','Estimate'),
            note='반환 뒤 remaining=None이고 batch_expected가 있으면 batch 중앙값으로 보완')),
    note='ETA·완료율·active name·elapsed를 집계. Telegram은 이 호출 뒤 queue_text에서 history/ETA를 다시 조회'),
 'macro별 요약 list; 19:05 queue dispatcher 표본에서 이 함수 7.874초',
 '실행 중 로그의 OSError/ValueError는 _remaining이 무시; 나머지는 caller 오류 처리')
# User flows: all supported platform entry/return paths.
tg('UC-01','진입·도움말','/start /help home',fn('ui.UiCatalog.value','menus.home.keyboard','rows'),tg_send())
gui('UC-01','편집기 기동','__init__',service('listing','','list[str]'),fn('gui.TicketEditor.poll_execution','','None',fn('gui.TicketEditor.update_execution_button','','None',runner())),note='gui.launch → Tk 생성 → TicketEditor → root.mainloop')
web('UC-01','초기 화면','start','/api/bootstrap',fn('patterns.PatternLibrary.load','','templates'),fn('editor.case_browser_start','empty path, tickets folder','Path'),note='그 뒤 /api/cases → render → refresh → visible/idle 10초 timer')
tg('UC-02','실시간 현재 CASE','/stat / status',fn('bot.Bot.fresh_runs','','(snap,runs,error)',snap(),fn('catalog.ticket_index','config','index',detail='D-01'),fn('catalog.TicketIndex.cases','snapshot.cases keys','matching cases'),fn('bot.Bot.active_runs','snap,cases','runs')),fn('bot.Bot.status','snap,runs,error','text',db('runtime_history','등록 run 각각','samples'),fn('report.compact_status','run','line')),tg_send(),error='fresh_runs는 OSError/RuntimeError만 catch. TimeoutExpired는 serve catch로 가며 사용자 오류 응답이 없을 수 있음')
web('UC-02','대시보드·현황 갱신','refresh','/api/overview',fn('web.WebApp.overview','','Overview',fn('web.WebApp.fresh','','Snapshot',runner('_snapshot','','Snapshot'),fn('tickets.sync_ticket_states','config,store,snap','None')),fn('bot.Bot.cases','','dict[id,Case]'),fn('bot.Bot.active_runs','snapshot','runs'),fn('logs.recent_case_log','각 등록 live case','(telemetry,path)'),fn('web.WebApp.ticket_rows','','rows',fn('catalog.folder_index','tickets folder','index'),runner('states','cached tickets','states by filename')),db('jobs','','all jobs'),fn('run_views.running_macro_views','macros,cases,jobs,store','MacroView[]')),note='실패: 마지막 snapshot + error; 성공: stableOverview → updateOverview 또는 render')
tg('UC-03','등록 케이스 목록·선택','/data /cases cases:page case:id',fn('bot.Bot.show_cases','chat,page','None',fn('bot.Bot.cases','','dict[id,Case]'),tg_send()),fn('bot.Bot.case_menu','case 선택 시 chat,cid','None',runner(),tg_send()))
web('UC-03','등록 케이스 목록','start / reload-data','/api/cases',fn('web.WebApp.case_rows','','CaseRow[]',fn('bot.Bot.cases','','dict[id,Case]')))
tg('UC-04','상태 상세','detail:case-id',fn('bot.Bot.latest_run','case','Job|Observed|None',db('jobs','','all jobs'),db('get','observed:root','Observed|None'),db('runtime_history','case,cores','samples')),fn('report.render_run','run,templates,detail','text'),tg_send())
web('UC-04','상태 상세·ETA','loadData','/api/detail?case=id',fn('web.WebApp.detail','cid','Detail',fn('web.WebApp.case','cid','Case'),fn('logs.recent_case_log','case','(telemetry,path)'),fn('bot.Bot.latest_run','case','run|None'),fn('logs.estimate','case,telemetry,elapsed,history','Estimate'),db('runtime_history','case','samples')),note='/api/artifacts와 Promise.all; stale dataId이면 결과 폐기')
for uc,title,action,call in [('UC-05','Residual 조회','residual',fn('artifacts.residual_files','case','FileItem[]',detail='D-10')),('UC-06','결과 파일 조회','export',fn('artifacts.export_files','case,export','Path[]',detail='D-10'))]:
 tg(uc,title,action+':case-id[:name]',call,fn('bot.Bot.file','파일마다 chat,item','Message',fn('telegram.Telegram.file','chat,item','Message',fn('telegram.Telegram.call','sendPhoto/Document, timeout=90','Message'),note='photo 400이면 document 재시도')))
 gui(uc,title,'open_queue_result_data',fn('artifacts.residual_files' if uc=='UC-05' else 'artifacts.export_files','현재 registry Case','파일 목록',detail='D-10'),note='현재 ticket로 추적 가능한 running/종료 job만; Tk Listbox에 경로 표시',outcome='파일 경로 표시; 이미지 preview/다운로드는 구현 없음')
 web(uc,title,'loadData / download','/api/artifacts → /api/file',fn('web.WebApp.artifacts','cid','ArtifactGroup[]',fn('web.WebApp.artifact_paths','cid,source','(Case,Path[])')),note='후속 /api/file은 Handler가 WebApp.file을 직접 호출: 재매칭→inside→open→fd 확인→64 KiB stream',outcome='파일 목록 JSON; 후속 파일 HTTP는 binary inline/attachment')
tg('UC-07','티켓 목록·다중 선택','/tickets /ticket tickets',fn('ticket_chat.TicketChat.listing','page','None',service('listing','','names'),panel()),fn('ticket_chat.TicketChat.bulk_list','bulk/page/all/none/toggle','None',service('listing','','names'),panel()),editor=True)
gui('UC-07','티켓 목록·다중 선택','refresh',service('listing','','names'),fn('gui.TicketEditor.update_execution_button','','None',runner()),note='select_all_tickets / clear_ticket_selection은 Listbox 로컬 상태만 변경')
web('UC-07','티켓 목록·선택','refresh / ticketList','/api/overview',fn('web.WebApp.overview','','Overview',fn('web.WebApp.ticket_rows','','rows',service('listing','','names'),runner())),note='검색·선택·전체 선택/해제는 S.search/S.selected 로컬; 전체 overview 경로는 UC-02')
for uc,title,op,method in [('UC-08','새 티켓','new','new'),('UC-09','티켓 열기','open','open_selected'),('UC-13','티켓 복제','duplicate','duplicate')]:
 args='kind' if op=='new' else 'name'
 tg(uc,title,op,fn('ticket_chat.TicketChat.switch','action,arg','None',fn('ticket_chat.TicketChat.do_switch','action,arg','None',service(op,args,'Draft'),fn('ticket_chat.TicketChat.card','session','None',runner(),panel()))),editor=True)
 if op=='new':
  gui(uc,title,method,fn('gui.TicketEditor.confirm_switch','','bool'),fn('gui.TicketEditor.set_form','TEMPLATE','None',fn('editor.form_values','data,tickets_dir','FormValues')),fn('gui.TicketEditor.refresh','','None'),note='GUI new는 TicketService.new 대신 공용 TEMPLATE/form_values 사용')
 else:
  gui(uc,title,method,fn('gui.TicketEditor.confirm_switch','','bool'),service(op,'selected name','Draft'),fn('gui.TicketEditor.set_form','draft.values._source','None'),fn('gui.TicketEditor.refresh','','None'))
 web(uc,title,{'new':'newTicket','open':'openTicket','duplicate':'action(duplicate-ticket)'}[op],{'new':'/api/new','open':'/api/ticket?name=...','duplicate':'/api/duplicate'}[op],service(op,args,'Draft'),*([runner()] if op=='open' else []),post=op!='open',note='confirmDiscard은 dirty draft 폐기 확인; 브라우저 내에서만 유지')
tg('UC-10','기본·감시·알림·스크립트 편집','field / event / scripts / scriptdefault',fn('ticket_chat.TicketChat.field','field key','None',panel()),fn('ticket_chat.TicketChat.input','후속 text','None',fn('ticket_chat.TicketChat.apply_field','session,key,text','None',note='numeric/regex/path 값 검사; draft.dirty=True'),panel()),editor=True)
gui('UC-10','기본·감시·알림·스크립트 편집','values',fn('editor.form_values','set_form 시 document','FormValues'),note='Tk variables/text widgets → values(); save/validate 때 form_document; 입력마다 파일 저장 안 함')
web('UC-10','기본·감시·알림·스크립트 편집','controlHint','/api/control?path=...',fn('control.control_times','root','limits|None'),note='대부분 input/change는 S.draft.values→dirty→renderEditor; controlHint만 HTTP')
tg('UC-11','티켓 검증','validate',service('validate','draft.values,filename,current','document',detail='D-11'),fn('ticket_chat.TicketChat.card','검증 완료 notice','None',panel()),editor=True)
gui('UC-11','티켓 검증','validate',fn('gui.TicketEditor.document','','document',service('validate','values,filename,current','document',detail='D-11')),outcome='True/False + status/messagebox')
web('UC-11','티켓 검증','action(validate)','/api/validate',service('validate','values,filename,current','document',detail='D-11'),post=True,note='먼저 validateExports: 각 export별 POST /api/exports/validate 순차 왕복')
tg('UC-12','저장·이름 변경','review → save',fn('ticket_chat.TicketChat.review','mode','None',service('validate','draft values','document'),panel()),service('save','revision,request_id,overwrite; submit=False','(name,data)',detail='D-02'),service('open','saved name','Draft'),fn('ticket_chat.TicketChat.card','saved notice','None',panel()),editor=True)
gui('UC-12','저장·이름 변경','save',fn('gui.TicketEditor.document','','document',service('validate','values','document')),service('save','values,filename,current,revision,overwrite','(name,data)',detail='D-02'),service('revision','saved name','SHA-256'),fn('gui.TicketEditor.set_form','saved data','None'),note='동기 Tk callback; overwrite는 messagebox; 일반 저장 전 fresh scan 없음',outcome='True/False; 저장된 폼/status')
web('UC-12','저장·이름 변경','saveNow','/api/save',fn('web.WebApp.fresh','','Snapshot'),service('save','values,filename,current,revision,request_id','(name,data)',detail='D-02'),service('open','saved name','Draft'),post=True,note='validateExports 순차 → save → cases → overview 순차; 새 macro 확인 modal')
tg('UC-14','개별·다중 티켓 삭제','delete/breview → deleteyes/bdelete',fn('ticket_chat.TicketChat.delete_review','names','None',service('deletion_preview','names,revisions?','Plan'),panel()),fn('ticket_chat.TicketChat.delete_confirmed','session.delete_plan','None',service('delete_many','plan.names,plan.revisions','deleted names',detail='D-05'),fn('ticket_chat.TicketChat.listing','','None',panel())),editor=True)
gui('UC-14','개별·다중 티켓 삭제','delete',service('deletion_preview','selected names,current revision','Plan'),service('delete_many','확인 Yes: plan','deleted names',detail='D-05'),fn('gui.TicketEditor.refresh','','None'),note='preview와 delete 사이 messagebox 확인; 일반 삭제 전 fresh scan 없음')
web('UC-14','개별·다중 티켓 삭제','action(delete-selected)','/api/delete/preview → /api/delete',fn('web.WebApp.fresh','각 두 요청에서','',''),post=True,note='preview: deletion_preview → Plan; confirm: delete_many(names,revisions) → deleted[]; 다음 refresh')
# Correct the compact placeholder above with the two alternative calls.
charts[-1]['root']['children'][0]['children'][0]['children'][0]['children']=[fn('web.WebApp.fresh','','Snapshot'),service('deletion_preview','preview: names','Plan'),service('delete_many','confirm: names,revisions','deleted[]',detail='D-05')]
tg('UC-15','매크로 검색·필터·취소','scan / stopscan',fn('ticket_chat.TicketChat.scan','session filters','None',panel(),fn('ticket_chat.TicketChat.scan.work','daemon thread','None',snap(),fn('tickets.discover_cases','root,snap.cases,end,include,exclude','(rows,skipped)',detail='D-06'),fn('ticket_chat.TicketChat.load','lock 재획득 후 session','session'),fn('ticket_chat.TicketChat.members','scan_id/입력 일치 시','None',panel())),note='stopscan은 scan_id 무효화; 실행 중 subprocess를 kill하지 않음'),editor=True)
gui('UC-15','매크로 검색·필터','scan_cases',fn('gui.TicketEditor.scan_cases.work','daemon thread','Queue.put(result)',snap(),fn('tickets.discover_cases','root,observed,end,include,exclude','(rows,skipped)',detail='D-06')),fn('gui.TicketEditor.scan_cases.finish','root.after(100)','None',fn('gui.TicketEditor.render_case_rows','','None')),note='main thread 반환; finish는 root/type/filter 일치 검사, end_time 재검사는 없음')
web('UC-15','매크로 검색·필터','action(discover)','/api/discover',fn('web.WebApp.fresh','','Snapshot'),fn('tickets.discover_cases','root,observed,end,include,exclude','(rows,skipped)',detail='D-06'),post=True,note='S.busy 동안 폼 inert; HTTP 완료까지 대기; 검색 중단 API 없음')
tg('UC-16','매크로 구성원 선택','members / remove',fn('ticket_chat.TicketChat.members','session, page','None',fn('tickets.has_postprocessing','각 보이는 root','bool'),panel()),editor=True)
gui('UC-16','매크로 구성원 선택','remove_case_row',fn('gui.TicketEditor.render_case_rows','','None',fn('tickets.has_postprocessing','각 row root','bool')),note='macro_cases.pop(index); 순서 이동 버튼 없음')
chart('UC-16-web','매크로 구성원 순서·제거','web','remove-case / case-up / case-down',fn('web_static.app.js:action','button dataset','Promise',fn('web_static.app.js:dirty','','None'),fn('web_static.app.js:renderEditor','','None')), 'S.draft.values.cases 변경; 저장 시 D-09로 반영','바쁜 macro 구성 변경은 저장 시 publish_macro가 거절')
tg('UC-17','실행·queue 이름·동적 macro 설정','execsource / cpupolicy / toggle / field / macrocores',fn('ticket_chat.TicketChat.queue','draft','None',panel()),fn('ticket_chat.TicketChat.apply_field','queue id·dynamic·행별 cores','None'),editor=True)
gui('UC-17','실행·queue 이름·동적 macro 설정','update_execution_visibility',note='queue id, dynamic checkbox, macro 행별 NP 표시; quota 입력 없음; child는 상속 표시; 저장 D-11')
chart('UC-17-web','실행·queue 이름·동적 macro 설정','web','input/change → resources 탭',fn('web_static.app.js:renderEditor','','None',fn('web_static.app.js:editor','','HTML',fn('web_static.app.js:executionForm','draft.values','HTML'))),'queue id·dynamic·행별 cores를 draft에 반영 후 저장 D-11; quota는 NP에서 파생; child는 부모에서 수정','공용 TicketService가 queue 이름 중복·macro 전용 조건 검증')
tg('UC-18','즉시 실행·이름 있는 대기열 등록','runstate / runreview / runyes',runner('state','name,fresh=True','RunState + 현재 head capacity/전체 max 안내'),service('save','dirty draft: values,revision,submit=False','(name,data)',detail='D-02'),runner('request','name,revision,request_id,mode','RunResult'),service('open','name','Draft'),fn('ticket_chat.TicketChat.card','실행 또는 queue id notice','None',panel()),editor=True)
gui('UC-18','즉시 실행·이름 있는 대기열 등록','submit / enqueue',fn('gui.TicketEditor.save','dirty/new일 때; submit=False','bool'),fn('gui.TicketEditor.submit.work','daemon thread','Queue.put(result)',runner('request','name,revision,mode','RunResult')),fn('gui.TicketEditor.submit.finish','after(100)','None',fn('gui.TicketEditor.update_execution_button','','None',runner())),note='동적 macro는 첫 child가 가용하면 즉시 실행 활성화; 최대 child는 전체 관리 한도만 검사')
web('UC-18','즉시 실행·이름 있는 대기열 등록','requestRun(mode)','/api/run',runner('request','name,revision,request_id,mode','RunResult'),post=True,note='dirty/new면 submit 없는 saveNow 선행; 동적 macro 첫 child를 현재 admission하고 뒤 child는 자기 순서에서 다시 판정')
tg('UC-18-legacy','케이스 메뉴 실행(우회 경로)','prepare:cid → enqueue:cid',runner('state','name,fresh=True','RunState'),fn('execution.execution_case','prepare일 때 case','ExecutionCase'),db('enqueue','enqueue일 때 case,update_id','Job'),tg_send())
for uc,title,action in [('UC-19','큐·이력·매크로 진행','queue'),('UC-20','자동 시작 pause/resume','pause/resume')]:
 tg(uc,title,action,*([db('put','queue_paused, bool')] if uc=='UC-20' else []),fn('bot.Bot.cases','','cases'),db('jobs','','jobs'),fn('config.tickets_for','macro 목록용 cached catalog','Ticket[]'),fn('run_views.running_macro_views','macros,cases,jobs,store','MacroView[]'),fn('report.queue_text','store,enabled','text',detail='D-13',note='전체 queued job마다 runtime_history 2연결 + ETA; 18:51 표본 5.535초, 본문 35조각'),tg_send())
 web(uc,title,'action('+action+')' if uc=='UC-20' else 'refresh','/api/queue' if uc=='UC-20' else '/api/overview',*( [db('put','queue_paused, bool')] if uc=='UC-20' else [fn('web.WebApp.overview','','Overview')]),post=uc=='UC-20',note='pause/resume은 새 admission만 제어; 이미 실행된 계산은 유지')
gui('UC-19','큐·이력·매크로 진행','refresh_queue_manager',db('jobs','queued 및 전체','Job[]'),fn('config.cases_for','config','Case[]'),fn('run_views.tracking_registry','cases','registry'),fn('run_views.job_view','각 job,registry','JobView'),fn('run_views.running_macro_views','macros,cases,jobs,store','MacroView[]'),note='수동 새로고침; 결과 리스트는 현재 ticket 추적 가능 여부 표시')
tg('UC-21','대기 작업 선택·취소','qselect/qall/qnone/qcancel/qcancelyes/cancel:id',fn('bot.Bot.queue_selection','chat,user','(jobs,selected)',db('jobs','queued','Job[]'),db('get','selection key','ids')),fn('queue_control.cancel_queued_jobs','확인 시 selected ids','CancelResult',detail='D-08'),fn('bot.Bot.show_queue_selection','result notice','None',tg_send()))
gui('UC-21','대기 작업 선택·취소','cancel_queue_selection',fn('queue_control.cancel_queued_jobs','확인 Yes: selected ids','CancelResult',detail='D-08'),fn('gui.TicketEditor.refresh_queue_manager','','None'))
web('UC-21','대기 작업 선택·취소','action(cancel-selected-jobs / cancel-job)','/api/queue',fn('queue_control.cancel_queued_jobs','ids 또는 [id]','CancelResult',detail='D-08'),post=True,note='bulk는 unavailable 반환; 단일 cancel 실패는 400')
tg('UC-22','실패 패턴 템플릿','templates/template/savetemplate',fn('patterns.PatternLibrary.load','','templates'),fn('ticket_chat.TicketChat.save_template','입력 이름','None',fn('patterns.PatternLibrary.save','name,rules','None'),fn('ticket_chat.TicketChat.rules','session','None',panel())),editor=True)
gui('UC-22','실패 패턴 템플릿','save_pattern',fn('patterns.PatternLibrary.load','','templates'),fn('patterns.PatternLibrary.save','name,rules','None'),fn('gui.TicketEditor.refresh_patterns','','None'),note='apply_pattern은 load 결과를 폼에만 적용; 저장 전 파일 반영 없음')
web('UC-22','실패 패턴 템플릿','action(save-preset)','/api/patterns',fn('patterns.PatternLibrary.save','name,rules','None'),fn('patterns.PatternLibrary.load','','templates'),post=True,note='apply-preset은 bootstrap으로 받은 S.patterns를 draft에만 적용')
tg('UC-23','폴더·로그·Residual 선택','browse/bd/bf/bapply/bcancel',fn('ticket_chat.TicketChat.browse_start','kind','None',fn('editor.case_browser_start','directory,folder','Path'),fn('ticket_chat.TicketChat.browser','page','None',panel())),fn('ticket_chat.TicketChat.apply_field','bapply: key,text','None'),editor=True)
gui('UC-23','폴더·로그·Residual 선택','choose_logs',fn('tkinter.filedialog.askopenfilenames','initialdir=case root','selected paths'),note='choose_case→askdirectory; choose_residual→askopenfilename; logs는 case-relative 목록으로 변환')
web('UC-23','폴더·파일 선택','showBrowser','/api/browse',fn('web.WebApp.browse','query(path,root,kind)','BrowserListing',fn('editor.case_browser_start','path,tickets folder','Path')),note='hidden/symlink 제외, 최대 1000건; pickPath는 draft만 변경')
tg('UC-24','입력 취소·초안 폐기·검색 취소','/cancel / backinput / discard / stopscan',fn('ticket_chat.TicketChat.card','pending/browser/scan_id 제거; draft 유지','None',panel()),editor=True)
gui('UC-24','저장/폐기/취소·닫기','confirm_switch',fn('tkinter.messagebox.askyesnocancel','미저장 변경','True/False/None'),fn('gui.TicketEditor.save','Yes일 때','bool'),note='No: 폐기하고 이동; None: 현재 화면 유지; close는 성공 시 after 취소/destroy',outcome='bool → new/open/duplicate/close 진행 여부')
chart('UC-24-web','초안 폐기·modal 취소','web','confirmDiscard / close-modal / beforeunload',fn('web_static.app.js:confirmDiscard','next action','UI action',fn('web_static.app.js:modal','dirty면 폐기 확인','None')),'확인 시 다음 action; 취소 시 draft 유지; 서버 rollback 동작 아님','진행 중 fetch의 서버 작업을 취소하는 기능은 없음')
tg('UC-25','대화 일괄 정리','/clean',db('chat_messages','chat, since=48h-60s','message_ids'),fn('telegram.Telegram.delete_messages','chat,ids','deleted ids',fn('telegram.Telegram._delete_batch','최대 100 ids','deleted ids',fn('telegram.Telegram.call','deleteMessages','API result'),note='400이면 절반씩 재귀; 단일 불가 ID는 []')),db('clear_messages','chat'),fn('ticket_chat.TicketChat.forget_panels','chat','None',note='draft 유지; token/panel/pending 제거'))
# Non-UI operational entrypoints are one chart with explicit command branches.
chart('UC-26','외부 PC 접속과 브라우저','Windows / macOS / Linux','launcher / cfd-web-tunnel',fn('OpenSSH ssh','-N -T -L loopback:local:loopback:server','연결 유지',note='Windows Get-SshAliases / macOS ssh_config_hosts → Host 번호 선택; ssh가 인증/ProxyJump 해석'), '포트 열림 확인 → 브라우저 http://127.0.0.1 → UC-01-web; 종료 시 자신이 만든 SSH 종료','잘못된 선택·포트 충돌·SSH 종료 시 실패; HTTP health 확인과 단순 포트 열림은 다름')
chart('UC-27','운영 CLI 명령 분기','CLI','python -m cfd_bot [command]',fn('cli.main','argv','exit code',fn('config.load_bot','config path','BotConfig'),fn('config.cases_for','check: force=True; enqueue: cached index','Case[]'),fn('processes.snapshot','status만: command','Snapshot',detail='D-04'),db('enqueue','enqueue만: selected Case','Job'),fn('monitor.Monitor.run_once','monitor만: loop/once','bool'),fn('bot.serve','serve만','None; loop'),fn('web.serve','web만','None; loop')), 'check는 검증/stdout만; identify는 Telegram 조회; gui는 config 분기 전에 launch','OSError/ValueError/RuntimeError → stderr, exit 2; _worker는 별도 worker 반환')
tg('UC-28','실행 중 managed 작업 중단','queue → stop:id → stopyes:id',db('job','확인 화면용 id','Job'),fn('queue_control.interrupt_running_job','확인 후 id','Job | None',detail='D-16'),tg_send())
gui('UC-28','실행 중 managed 작업 중단','interrupt_active_job',fn('tkinter.messagebox.askyesno','선택 active job','bool'),fn('queue_control.interrupt_running_job','확인 Yes: id','Job | None',detail='D-16'),fn('gui.TicketEditor.refresh_queue_manager','','None'))
web('UC-28','실행 중 managed 작업 중단','action(stop-job)','/api/queue',fn('queue_control.interrupt_running_job','action=interrupt, id','Job | None',detail='D-16'),post=True,note='브라우저 확인 modal 뒤 호출; stopping 상태에는 버튼 없음')
# Background flows, expanded function-level request/return charts.
chart('BG-01','solver·monitor CPU를 합치는 scanner','Bash + Python','bin/ofps options',fn('bin/ofps:scan_and_sync','records path','scan exit status',fn('bin/ofps:scan_once','records_file','stdout',fn('bin/ofps:scan_supervisors','proc/process candidates','supervisor records'),fn('bin/ofps:openfoam_case_from_process','PID','case root|empty'),fn('bin/ofps:basilisk_case_from_process','PID','case root|empty'),fn('bin/ofps:monitor_case_from_process','PID environ + cwd','표식 있는 case root|empty',note='bot CASE/JOB/MONITOR_CPU 또는 TCB_MONITORED_SOLVER_PID; controlDict와 case 내부 cwd 확인'),fn('bin/ofps:thread_affinity_union','solver 또는 monitor PID','CPU set')),fn('bin/ofps:sync_ticket_state','standalone만 snapshot file','status',fn('processes.parse_snapshot','raw','case별 solver+monitor affinity 합집합'),db('put','snapshot, parsed'),fn('tickets.sync_ticket_states','settings,store,snapshot','None'))),'managed는 stdout만; --check는 monitor CPU overlap도 거절; --watch는 반복','유효 case root 또는 지원하는 monitor 표식이 없는 후보 제외; standalone sync 실패가 CPU 검사 exit를 덮어쓰지 않음')
chart('BG-02','현재 CASE · 이전 실행/종료 확인 중 CASE 감시','Monitor thread','run_once 완료 후 poll_seconds 대기',
 fn('monitor.Monitor.run_once','','bool',fn('monitor.Monitor.tick','','None',
    fn('catalog.ticket_index','config','index',detail='D-01'),snap(),db('put','snapshot / monitor_error'),
    fn('tickets.accept_submissions','config,store','None'),fn('jobs.Scheduler.recover','','None'),db('jobs','LIVE','managed jobs'),
    db('tracked_observations','','persisted pending roots'),
    loop('현재 snapshot roots ∪ tracked roots − managed roots',
         fn('catalog.TicketIndex.lookup','canonical full root','Ticket | None'),
         fn('monitor.automatic_case','미등록 root','automatic Case'),
         fn('monitor.Monitor.observe','case,record | None','None',
            fn('monitor.observed_identity','record','supervisor/process identities'),
            fn('monitor.new_execution','previous identities,current','bool: same path restart'),
            fn('logs.recent_case_log','대상 case / 종료 확인 시 final=True','(telemetry,path)'),
            fn('outcomes.decide','missing 임계 도달 / 최신 로그','(status,reason)',detail='D-12'),
            db('put','observed:root; trigger가 tracking/변경 기록'),
            fn('jobs.terminal_event','terminal observed','None'),db('finish_observation','outbox 성공한 terminal root,run id'))),
    fn('jobs.Scheduler.tick','snapshot.cases','None'),fn('tickets.sync_ticket_states','config,store,snapshot','None',detail='D-15'))),
 '종료 확정·outbox 완료 대상은 다음 tick 감시에서 제외; 새 실행 identity 구분','scan 실패는 missing 증가 없음; state/outbox 사이 실패는 persisted tracking으로 재시도')
chart('BG-03','변경된 제출 티켓 접수','Monitor','submit=True 티켓',
 fn('tickets.accept_submissions','config,store','None',
    fn('catalog.ticket_index','config','index',detail='D-01'),fn('catalog.TicketIndex.tickets','submit_only=True','pending Ticket[]'),
    fn('catalog.TicketIndex.cases','접수 대상 존재 시','cached Case[]'),db('enqueue_batch','children,request_id','Job[]; SQL trigger 기록'),
    fn('tickets.atomic_json','submit=False 또는 접수 실패 reason','None'),db('event','submission 또는 submission-blocked','None')),
 'DB 큐 순서·request 멱등성 유지; 이후 D-15에서 대상 상태 반영','batch 충돌: reason/outbox; 실제 solver 시작은 Scheduler가 결정')
chart('D-15','증분 상태 반영 · 종료 child와 부모 macro 재시도','공용','Monitor / Web / CLI / standalone ofps',
 fn('tickets.sync_ticket_states','config,store,snapshot','None',
    fn('tickets.ticket_lock','state directory','전체 sync 직렬화 context'),
    fn('tickets._sync_ticket_states','config,store,snapshot,tickets=None','None',
       fn('catalog.ticket_index','config','index',detail='D-01'),fn('catalog.TicketIndex.changes','','changed roots + generations'),
       db('ticket_changes','','root → watermark'),fn('catalog.TicketIndex.cases','현재 roots ∪ ticket 변경 ∪ DB 변경','target Case[]'),
       fn('catalog.TicketIndex.related_macros','target roots','parent Macro[]'),db('jobs_for_roots','target roots','Job[]'),db('get_many','observed:target roots','observed runs'),
       loop('대상 child/single + 관련 parent macro만',fn('tickets.ticket_lock','ticket folder','context'),
            fn('config.read_json','대상 ticket / parent','현재 document'),
            fn('tickets.atomic_json','request_id 동일 + 변경 있을 때','fsync / replace',note='부모 row와 집계 포함; 실패하면 ACK하지 않음')),
       db('acknowledge_ticket_changes','성공한 roots와 읽었던 watermark','새 이벤트 보존'),fn('catalog.TicketIndex.acknowledge','성공한 generations','None'))),
 '종료·취소·전후처리도 DB 이벤트로 반영; 완료된 과거 티켓 반복 검사 없음','동기화 중 새 제출의 request_id가 바뀌면 보존·재시도; DB/JSON 분리 장애는 journal 재생')
chart('D-16','실행 중 managed 작업 안전 중단','공용','interrupt_running_job(store, jid)',
 fn('queue_control.interrupt_running_job','store, jid, ui?','Job | None',
    db('request_interruption','LIVE job id, reason','stopping Job | None'),
    fn('queue_control.interrupt_process_groups','저장된 hook/monitor/solver PID + identity','signalled PID[]',
       fn('processes.identity','현재 PID','boot:pid:starttick | None'),
       fn('os.getpgid','identity 일치 PID','process group id'),
       fn('os.killpg','session leader, SIGTERM','None')),
    db('update_job','worker/children 모두 없으면 stopping → interrupted','Job | None')),
 'stopping 동안 CPU·case unique 예약 유지; worker가 TERM 후 필요 시 KILL하고 interrupted 확정',
 'queued/terminal/external ofps-only job은 변경 없음; PID identity 불일치는 신호 생략')
chart('BG-04','현재 head 기반 quota·동적 borrow admission','Monitor thread','Scheduler.tick(observed)',fn('jobs.Scheduler.tick','observed cases','None',fn('jobs.Scheduler.recover','','None'),db('unpublished_terminal_jobs','outbox/marker 없는 terminal','Job[]'),fn('jobs.terminal_event','미발행 terminal job','outbox + published marker'),fn('catalog.TicketIndex.queue_profiles','legacy ticket + assigned job profile','Profile[]'),fn('jobs.scheduling_candidates','queued jobs, active jobs','active 없는 batch별 첫 child + idle queue별 head'),fn('queueing.queue_heads','임의 개수 queue_id별 FIFO','Job[]'),fn('jobs.Scheduler._assign_queue_profiles','일반 head 실제 NP + monitor','자동 CPU profile + blocked'),fn('queueing.borrowing_plan','현재 동적 child NP + 미예약 CPU + 작은 donor','allowed CPUs + donors'),fn('jobs.Scheduler._borrow_state','drain claim + donor별 1회 우선권','claim,fairness'),fn('execution.execution_case','선택된 현재 head','ExecutionCase'),fn('execution.openfoam_environment','bashrc, env(token 제거)','env'),snap(),fn('cpu_allocation.allocate_cpus','자동 profile 또는 borrow allowed set','allocation'),fn('processes.check_cpus','command,case; monitor면 추가 1회','(safe,report)'),db('update_job','expected=queued → starting','Job|None'),fn('subprocess.Popen','python -m cfd_bot _worker; new session','Popen')),'terminal event는 job당 한 번; 즉시 실행 macro도 active child가 없을 때 현재 child 하나만 admission; 서로 다른 queue는 겹치지 않는 CPU에서 병렬 실행','환경/CPU 실패 queued 유지; 전체 관리 용량 초과는 대기; worker spawn OSError → failed + terminal_event')
chart('BG-05','worker·solver·hooks·판정','detached process','cli.main(_worker)',fn('jobs.worker','state_dir,jid','0 success / 1 failure',db('update_job','starting → running, phase=preprocess','Job|None'),fn('execution.apply_execution_settings','case,job_folder','None',note='cfd_bot이 .process-core 생성/동기화; 원본 backup'),fn('jobs.run_case_hooks','preprocess','errors / raises'),fn('subprocess.Popen','case Allrun; .process-core → --cpu-list + cpu-list:ordered + odls_base_max_threads=1','Popen',note='local rank spawn만 직렬화; solver MPI 계산은 병렬'),fn('logs.read_log','0.5초 loop; wrapper/configured','telemetry'),fn('jobs._retry_locked','Store write; locked/busy면 재시도','DB 결과',note='재시도 중 solver·monitor·hook 유지'),db('update_job','telemetry + stopping 확인','Job|None'),fn('jobs._stop_child','중단 요청 시 process group','TERM → 10초 뒤 KILL'),fn('logs.finish_log','최종 잔여 데이터','telemetry'),fn('outcomes.decide','case,telemetry,rc','status,reason',detail='D-12'),fn('jobs.run_case_hooks','성공 시 postprocess','post_errors'),fn('artifacts.freeze_exports','case,event folder,started,status','files,notes'),db('put','event:jid, frozen payload'),db('update_job','terminal state','Job|None')),'다음 Scheduler.tick이 미발행 terminal만 outbox 접수; 중단은 interrupted가 solver 판정보다 우선','preprocess 실패 solver 시작 안 함; DB locked/busy는 child 유지+재시도; stopping이면 child cleanup+interrupted')
chart('BG-06','outbox 알림 전달·checkpoint','Delivery thread','deliver 완료 후 1초 대기',fn('bot.deliver','store,api,text_path,ui','None',db('pending','limit=10','outbox rows'),fn('report.render_run','terminal run, completion template','text'),db('save_delivery','messages/file offset 초기화'),fn('telegram.Telegram.send','남은 message 순차','Message[]'),db('save_delivery','message_index 증가'),fn('telegram.Telegram.file','남은 file 순차','Message'),db('save_delivery','file_index 증가'),db('delivered','완료 row id'),db('retry','실패 row, retry_after','None')),'recipient별 pending/완료; API 성공 후 offset 저장 사이 crash는 중복 전송 가능','TelegramError → retry_after+backoff; 첨부 파일 유실 → skip 안내; 기타 오류도 retry')
chart('BG-07','worker 소실·재시작 복구','Monitor / Scheduler','Scheduler.recover',fn('jobs.Scheduler.recover','','None',db('jobs','LIVE','Job[]'),fn('processes.identity','worker/hook/solver/monitor PID','identity|None'),fn('queue_control.interrupt_process_groups','stopping job','signalled PID[]',detail='D-16'),fn('logs.recent_case_log','worker/solver 모두 종료한 case','telemetry,path'),fn('outcomes.decide','fresh log, case','status,reason',detail='D-12'),db('update_job','expected LIVE','Job|None'),db('event','worker lost but solver alive','None')),'starting 60초 grace; stopping은 모든 process 소멸 뒤 interrupted; 그 밖의 죽은 실행은 로그 판정','preprocess/startup 소실은 failed; PID 재사용은 boot/starttick identity로 구분')

for uc,title in [('UC-05-file','Residual 실제 HTTP 전송'),('UC-06-file','결과 파일 실제 HTTP 전송')]:
    chart(uc+'-web',title,'web','GET /api/file?case&source&file[&download=1]',
          fn('web.Handler.handle_request','GET /api/file','binary HTTP response',
             fn('web.WebApp.file','query','(open stream, filename, size)',
                fn('web.WebApp.artifact_paths','case, source','(Case, allowed Paths)'),
                fn('config.inside','root, relative file','Path'),
                fn('pathlib.Path.open','rb','file object',note='열린 fd의 /proc/self/fd 경로 및 fstat 재검증; 49 MiB 제한')),
             fn('web.Handler.send_headers','mime, disposition, size','None'),note='source.read(65536) → wfile.write 반복; stream close'),
          '브라우저 inline 이미지 또는 attachment 다운로드','현재 선언 목록 제외/경로/크기 오류 400; 연결 종료는 stream 정리')
tg('UC-10-exports','요청 데이터 정의 편집','exports / xe / xnew / xkind / xapply / xdelete',
   fn('ticket_chat.TicketChat.exports','page','None',panel()),
   fn('ticket_chat.TicketChat.export_card','session.export_edit','None',panel()),
   fn('editor.validate_export','xapply: item,others,root,index','Export'),editor=True)
web('UC-10-exports','요청 데이터 정의 검증','validateExports','/api/exports/validate',
    fn('editor.validate_export','item,others,root,index','Export'),post=True,note='각 export를 순차 요청; add/remove는 먼저 로컬 draft 수정')

gui('UC-10-exports','요청 데이터 정의 편집','edit_item',fn('gui.TicketEditor.edit_item.apply','적용 버튼 callback','None',fn('editor.validate_export','item,existing,case root,index','Export'),fn('gui.TicketEditor.refresh_tables','','None')),note='적용 callback은 dialog에서 입력 후 실행; 취소는 dialog.destroy만 호출')
# Entry commands and field replies branch directly in TicketChat.handle.
for c in charts:
    if c['key']=='UC-07-tg':
        handle=c['root']['children'][0];action=handle['children'][0]
        listing=action['children'].pop(0);handle['children'].insert(0,listing)
    if c['key']=='UC-10-tg':
        handle=c['root']['children'][0];action=handle['children'][0]
        input_call=action['children'].pop();handle['children'].append(input_call)
    if c['key']=='UC-24-tg':
        handle=c['root']['children'][0];handle['children']=handle['children'][0]['children']
    if c['key']=='UC-10-gui':
        c['root']['children']=[]
        c['root']['note']='Tk 변수/텍스트를 FormValues로 수집. form_values는 set_form에서 사용; 저장 시 D-11'
    if c['key']=='UC-01-gui':
        c['root']['children']=[fn('gui.TicketEditor.new','','None',fn('gui.TicketEditor.refresh','','None',service('listing','','names'))),fn('gui.TicketEditor.poll_execution','','None',fn('gui.TicketEditor.update_execution_button','','None',runner()))]
    if c['key']=='UC-02-web':
        ov=c['root']['children'][0]['children'][0]['children'][0]['children'][0]
        rows=ov['children'];jobs=rows.pop(5);rows.insert(4,jobs)

# Never hide external process/network boundaries behind a collapsed helper.
from copy import deepcopy
for c in charts:
    if c['key']=='BG-01':
        c['root']['note']='managed=1이면 sync_ticket_state 생략. /proc scan은 실행; standalone은 D-15 증분 sync'
    if c['key']=='UC-19-gui':
        nodes=c['root']['children']
        pos=next(i for i,n in enumerate(nodes) if n['name']=='run_views.running_macro_views')
        nodes.insert(pos,fn('config.tickets_for','cached macro documents','Ticket[]'))
scan_detail=deepcopy(next(c['root'] for c in charts if c['key']=='D-04'))
run_detail=deepcopy(next(c['root'] for c in charts if c['key']=='D-03'))
scan_detail['children'][0]['children']=[fn('bin/ofps:scan_and_sync', '새 프로세스; CFD_BOT_OFPS_MANAGED=1', 'stdout / stderr / exit code', fn('bin/ofps:scan_once', 'records_file; /proc / ps 탐색', 'ENGINE / CASE / process table'), note='managed 모드: 내부 ticket sync 생략; scan 자체는 실행')]
def expand_boundaries(n):
    if n['name']=='run_views.running_macro_views':
        n['detail']='D-14'
        if not n['note']:n['note']='각 미완료 row의 _remaining → estimate → unknown이면 history 조회. 뒤의 queue_text와 중복; D-14 상세'
    if n['name']=='tickets.ticket_lock':
        n['response']='context manager; with 진입 시 획득'
    if n['name']=='config.cases_for':
        n['detail']='D-01'
        if not n['note']:
            n['note']='공용 TicketIndex: 최초 전체 검증, 이후 변경 파일만 load_case; warm은 검증 0회. D-01 상세'
    if n['name']=='bot.Bot.cases' and not n['children']:
        n['children']=[fn('config.cases_for','config','Case[]',detail='D-01')]
    if n['name']=='ticket_run.TicketRunner.request' and not n['children']:
        n['children']=deepcopy(run_detail['children'])
    if n['name']=='processes.snapshot':
        n['children']=deepcopy(scan_detail['children'])
        n['note']='SNAPSHOT_LOCK 획득 대기 → subprocess 실행 중 보유 → 해제 → parse'
    if n['name']=='ticket_run.TicketRunner._snapshot' and not n['children']:
        n['children']=[snap(),db('put','snapshot, at 포함 current','None')]
    if n['name']=='ticket_run.TicketRunner.state' and 'fresh=True' in n['request'] and not n['children']:
        n['children']=[runner('_snapshot','','Snapshot')]
    if n['name']=='web.WebApp.fresh' and not n['children']:
        n['children']=[runner('_snapshot','','Snapshot'),fn('tickets.sync_ticket_states','config,store,snapshot','None')]
    if n['name']=='telegram.Telegram.call' and not n['children']:
        n['children']=[fn('urllib.request.urlopen','HTTPS Telegram Bot API; timeout','HTTP response JSON',note='실제 외부 HTTP 요청; token은 표시하지 않음')]
    for child in n['children']:expand_boundaries(child)
for c in charts:expand_boundaries(c['root'])

def q(s):
    return json.dumps(str(s), ensure_ascii=False)

def wrap(s,width=42):
    return '\n'.join('\n'.join(textwrap.wrap(line,width,break_long_words=False,break_on_hyphens=False)) for line in s.split('\n'))

def render(item):
    """Horizontal UML-style participants, lifelines, request/return arrows."""
    import html
    import math
    def owner(name):
        if '.work' in name:return '검색 worker' if 'scan' in name else '실행 worker'
        if '.finish' in name:return 'Tk 이벤트 loop'
        if name.startswith('web_static.'):return 'Browser JS'
        for prefix,title in [('ticket_chat.TicketChat','TicketChat'),('ticket_run.TicketRunner','TicketRunner'),('editor.TicketService','TicketService'),('gui.TicketEditor','TicketEditor'),('web.Handler','HTTP Handler'),('web.WebApp','WebApp'),('bot.Bot','Bot'),('telegram.Telegram','Telegram client'),('storage.Store','SQLite Store'),('monitor.Monitor','Monitor'),('jobs.Scheduler','Scheduler'),('patterns.PatternLibrary','PatternLibrary')]:
            if name.startswith(prefix):return title
        return {'urllib':'Telegram Bot API','bin/ofps':'ofps 프로세스','OpenSSH ssh':'OpenSSH','pathlib':'파일 시스템','tkinter':'Tk dialog'}.get(name.split(':')[0].split('.')[0],name.split('.')[0].split(':')[0])
    def method(name):
        return name.split(':')[-1] if ':' in name else name
    external='사용자 / '+item['platform'] if len(item['platform'])<20 else '사용자 / 운영자'
    events=[];deferred=[]
    def note(text):
        if text:events.append(('note','', '',text,''))
    def walk(n,caller,async_body=False):
        if 'loop' in n:
            events.append(('loop_start','','',n['loop'],''))
            for child in n['children']:walk(child,caller)
            events.append(('loop_end','','','',''))
            return
        target=owner(n['name'])
        events.append(('call',caller,target,method(n['name'])+'('+n['request']+')',n['detail']))
        note(n['note'])
        for c in n['children']:
            if c['name'].endswith('.work'):
                events.append(('async',target,owner(c['name']),'Thread.start → '+method(c['name'])+' (비동기)',''))
                deferred.append((c,target,'work'))
            elif c['name'].endswith('.finish'):
                deferred.append((c,'Tk 이벤트 loop','finish'))
                note('root.after(100, finish) 예약; UI callback은 결과를 기다리지 않고 반환')
            else:walk(c,target)
        events.append(('return',target,caller,n['response'],''))
    walk(item['root'],external)
    while deferred:
        n,caller,kind=deferred.pop(0)
        note('비동기 후속 처리: 부모 요청 반환과 별개로 실행 / 결과 반영')
        if kind=='finish':walk(n,caller)
        else:
            target=owner(n['name']);note(n['name'])
            for c in n['children']:walk(c,target)
            note('worker 종료; session 저장 또는 Queue 결과 → UI 갱신')
    participants=[external]
    for event in events:
        for p in event[1:3]:
            if p and p not in participants:participants.append(p)
    lane=280;W=max(1600,len(participants)*lane+380,len(events)*56+380)
    xs={p:150+(W-360)*i/max(1,len(participants)-1) for i,p in enumerate(participants)}
    # Keep wide enough for labels and landscape proportions even for long flows.
    y=180;rows=[]
    def wrapped(txt,chars):
        return wrap(txt,max(22,chars)).split('\n')
    for kind,a,b,txt,detail in events:
        if kind in ('note','loop_start'):
            ll=wrapped(txt,int((W-160)/11));height=24+22*len(ll)
        elif kind=='loop_end':
            ll=[];height=20
        else:
            distance=abs(xs[a]-xs[b])
            chars=int((distance-20)/8.5) if a!=b else 30
            ll=wrapped(txt,chars)
            height=20+18*len(ll)+(20 if a==b else 0)
        rows.append((y,height,kind,a,b,ll,detail));y+=height
    # Separate alt failure frame, then the observable response boundary.
    error_lines=wrapped(item['error'],int((W-160)/11));out_lines=wrapped(item['outcome'],int((W-160)/11))
    bottom=y+len(error_lines)*22+len(out_lines)*22+180
    H=bottom+110
    esc=lambda s:html.escape(str(s),quote=True)
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(item["title"])}"><title>{esc(item["key"]+" "+item["title"])}</title><defs><marker id="req" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="#16a34a"/></marker><marker id="ret" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10" fill="none" stroke="#0284c7" stroke-width="1.5"/></marker></defs><rect width="100%" height="100%" fill="white"/><g font-family="Noto Sans CJK KR, sans-serif">']
    def text(x,y,t,size=15,color='#0f172a',anchor='middle',bold=False):
        parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" fill="{color}"'+(' font-weight="700"' if bold else '')+'>'+esc(t)+'</text>')
    text(W/2,40,item['key']+' · '+item['title'],27,bold=True)
    changed = item['key'] in {'D-03', 'BG-04', 'UC-17-tg', 'UC-17-gui', 'UC-17-web', 'UC-18-tg', 'UC-18-gui', 'UC-18-web'}
    revision = '#24 현재 head 기반 admission 반영' if changed else '#20 운영 반영'
    text(W/2,71,item['platform']+' | '+revision+' · 함수 요청은 실선 → / 반환은 점선 ← / 시간은 위에서 아래로',15,'#475569')
    text(W/2,97,'진입: '+item['trigger'],14,'#475569')
    for p,x in xs.items():
        parts.append(f'<path d="M{x},155 V{bottom}" stroke="#cbd5e1" stroke-dasharray="5 5"/>')
        for yy in (112,bottom):
            width=min(220,(W-140)/max(1,len(participants)-1)-14) if len(participants)>1 else 240
            parts.append(f'<rect x="{x-width/2}" y="{yy}" width="{width}" height="42" fill="#16a34a"/>')
            text(x,yy+27,p,15,'white',bold=True)
    loop_frames=[];loop_stack=[]
    for yy,h,kind,a,b,ll,detail in rows:
        if kind=='loop_start':loop_stack.append((yy,len(loop_stack)))
        elif kind=='loop_end':
            start,depth=loop_stack.pop();loop_frames.append((start,yy+h,depth))
    for start,end,depth in loop_frames:
        inset=20+depth*12
        parts.append(f'<rect x="{inset}" y="{start}" width="{W-2*inset}" height="{end-start}" fill="none" stroke="#7c3aed" stroke-width="2"/>')
    activations=[];open_calls={p:[] for p in participants}
    # Activation bars sit behind arrows; self calls use a narrower offset bar.
    for yy,h,kind,a,b,ll,detail in rows:
        line_y=yy+18*len(ll)
        if kind=='call':open_calls[b].append(line_y)
        elif kind=='return' and open_calls[a]:
            start=open_calls[a].pop();activations.append((xs[a],start,line_y-start))
    for x,yy,h in sorted(activations,key=lambda r:-r[2]):
        parts.append(f'<rect x="{x-6}" y="{yy}" width="12" height="{max(8,h)}" fill="#f0fdf4" stroke="#86efac"/>')
    for yy,h,kind,a,b,ll,detail in rows:
        if kind=='loop_end':continue
        if kind=='loop_start':
            for i,line in enumerate(ll):text(W/2,yy+23+i*22,('loop · ' if i==0 else '')+line,14,'#6d28d9',bold=True)
            continue
        if kind=='note':
            parts.append(f'<rect x="32" y="{yy+2}" width="{W-64}" height="{h-8}" rx="3" fill="#fffbeb" stroke="#fcd34d"/>')
            for i,line in enumerate(ll):text(W/2,yy+23+i*22,line,14,'#854d0e')
            continue
        ax,bx=xs[a],xs[b];ly=yy+18*len(ll)
        returning=kind=='return';color='#0284c7' if returning else '#16a34a';marker='ret' if returning or kind=='async' else 'req'
        dash=' stroke-dasharray="6 4"' if returning else ''
        if ax==bx:
            parts.append(f'<path d="M{ax+6},{ly} H{ax+(-110 if ax>W-350 else 110)} V{ly+18} H{ax+6}" fill="none" stroke="{color}" stroke-width="1.8"{dash} marker-end="url(#{marker})"/>')
            tx=ax+12;anchor='start'
            if ax>W-350:
                tx=ax-12;anchor='end'
        else:
            sign=1 if bx>ax else -1
            parts.append(f'<path d="M{ax+sign*6},{ly} H{bx-sign*7}" fill="none" stroke="{color}" stroke-width="1.8"{dash} marker-end="url(#{marker})"/>')
            tx=(ax+bx)/2;anchor='middle'
        if detail:parts.append('<a href="'+esc(detail+'.svg')+'" target="_blank">')
        for i,line in enumerate(ll):text(tx,yy+14+i*18,line,15,color if returning else '#111827',anchor)
        if detail:parts.append('</a>')
    y+=12
    eh=len(error_lines)*22+55
    parts.append(f'<rect x="24" y="{y}" width="{W-48}" height="{eh}" fill="#fff1f2" stroke="#fda4af"/>')
    text(42,y+22,'alt [오류 / 거절 분기]',15,'#9f1239','start',True)
    for i,line in enumerate(error_lines):text(42,y+47+i*22,line,14,'#9f1239','start')
    y+=eh+18
    for i,line in enumerate(out_lines):text(W/2,y+22+i*22,'최종 응답: '+line if i==0 else line,15,'#065f46',bold=True)
    parts.append('</g></svg>\n')
    (HERE/(item['key']+'.svg')).write_text(''.join(parts))

if __name__=='__main__':
    for item in charts:render(item)
    (HERE/'index.json').write_text(json.dumps([{k:v for k,v in c.items() if k!='root'} for c in charts],ensure_ascii=False,indent=2)+'\n')
    print('Rendered',len(charts),'function request/response flowcharts')
