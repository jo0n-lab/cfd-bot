# 병목 분석 — 코드 근거·격리 실험·개선안

> 아래 실측·후보 목록은 #20 구현 전 기준이다. #20의 격리 구현·921-member 결과는 [새 측정](ticket-index-results.json)과 [변경 이력](../history/2026-10-04-ticket-index-incremental-monitor.md)을 참조한다. 아래 과거 운영 수치는 갱신하지 않았다.

> [#19](https://github.com/jo0n-lab/cfd-bot/issues/19) · 2026-10-04 · [함수 요청/반환 그림](../lld/flows.md). 제품 로직은 변경하지 않았다. 아래 우선순위는 **조사·개선 후보의 순서**이며 운영 환경의 병목 기여율 순위가 아니다.

## 1. 확인한 사실과 아직 모르는 것

실제 설정 검사에서 등록된 catalog는 982개 케이스였다. 후속 운영 데이터 분석에서 전체 JSON 1,007개, macro 1개/921행, child 945개를 확인했다. catalog 수와 실제 실행 중인 CASE 수는 다르다. 요청마다 전체 티켓 집합을 검사하며 하나의 큰 macro에서는 membership이 O(N²)로 증가한다. 케이스 이름·운영 로그·인증값은 이 문서에 복사하지 않았다.

최초 격리 실험 이후 [운영 데이터 읽기 전용 분석](live-bottlenecks.md)을 추가했다. managed ofps 3회는 약 1.06초, cases_for의 membership만 16.562/19.684초, queue_text만 5.535초였다. **현재 표본에서 우선 수정할 병목은 P-08의 macro membership 중첩 검색**이다. P-09의 큐 전체 ETA/본문 생성·직렬 전송도 확인했다. Telegram 요청 대기, daemon 내부 lock wait, API RTT는 여전히 미측정이며 독립 함수 결과를 사용자 요청 전체 trace로 해석하지 않는다.

## 2. 재현 가능한 격리 실험

```bash
python3 docs/analysis/benchmark_architecture.py
```

[실험 코드](benchmark_architecture.py) · [원시 결과](benchmark-results.json) · [검토 소스 SHA-256](source-manifest.json)

Python 3.9.25/Linux, single ticket 1/10/40개, 같은 수의 합성 live CASE, 짧은 2개 Time/ClockTime 표본, jobs=0, macro=0, 동시 요청=1, Monitor/Delivery 없음. 각 경로 warmup 3회 뒤 20회 실행. snapshot은 mock이며 subprocess.run/Popen과 urlopen은 호출하면 즉시 실패하도록 차단했다. SQLite와 JSON/로그 읽기는 실제 임시 파일에서 수행했다. 첫 warmup에서 queue 표시를 동기화하므로 표는 반복 읽기 위주의 상태다.

Telegram 경로는 `Bot.fresh_runs → Bot.status`, web 경로는 `WebApp.overview`다. Telegram transport/dispatcher, 실제 ofps, HTTP serialization/브라우저 렌더링은 포함하지 않는다. 두 경로는 제공하는 정보량이 달라 단순 속도 경쟁으로 해석하지 않는다. p95는 정렬된 20개 중 19번째 값으로 표본 수가 작다. 타이밍과 호출 수 수집은 별도 실행해 profiler overhead를 시간에서 제외했다.

| backend | 티켓/live CASE | p50 ms | 표본 p95 ms | load_case 호출 | SQLite connect | Store.jobs |
|---|---:|---:|---:|---:|---:|---:|
| Telegram stat | 1 | 1.691 | 1.978 | 1 | 3 | 0 |
| Telegram stat | 10 | 13.324 | 15.042 | 10 | 21 | 0 |
| Telegram stat | 40 | 50.951 | 55.991 | 40 | 81 | 0 |
| Web overview | 1 | 11.730 | 13.926 | 5 | 10 | 3 |
| Web overview | 10 | 48.493 | 55.296 | 50 | 46 | 12 |
| Web overview | 40 | 169.397 | 176.248 | 200 | 166 | 42 |

이 fixture에서는 Telegram이 `load_case=T`, SQLite `2T+1`, web이 `load_case=5T`, SQLite `4T+6`, Store.jobs=`T+2`였다. 이는 정적 코드에서 본 전체 catalog/티켓별 상태 조회 반복과 일치한다. `TicketRunner._state`는 jobs를 root별로 매번 필터링하므로 jobs가 많아지면 티켓 수×active job 수 항도 생긴다. macro 멤버·과거 job·filesystem·경합은 이 표에 없으므로 982개 운영 환경 시간으로 선형 외삽하지 않는다.

## 3. 병목 후보별 호출 그림·근거·To-Be

<a id="p-08"></a>
### P-08 — macro membership 중첩 경로 확인 (후속 실측 1순위)

[내부 반복 D-01](../diagrams/D-01.svg) · [LLD 알고리즘](../LLD.md#catalog) · [원시 실측](live-readonly-results.json).

As-Is: cases_for가 child마다 parent.cases를 처음부터 any로 검색하고 매 row에서 Path.resolve를 수행한다. 921개 유효 child와 24개 목록 밖 child의 현재 표본은 membership row 조건 446,685회, 부모 확인까지 resolve 447,630회다. 이 횟수는 입력/조건으로 계산한 값이며 syscall trace가 아니다. 실제 함수만 16.562/19.684초로 managed ofps 약 1.06초보다 컸다. 기존 T=1/10/40 single-only fixture에는 이 macro 항이 없어 이 문제를 검출하지 못했다.

To-Be(미구현): 요청 내에서 부모별 `(resolved ticket path, case root)` set을 한 번 구축한다. 같은 입력에 대한 메모리 내 비교용 구현은 0.0952초였고 982개 반환 및 순서가 같았다. 제품 적용 전 경로 안전성, 누락 부모/미발행 child, 중복 root, symlink와 동시 파일 변경을 검증해야 한다. 경로 검사를 삭제하거나 watcher cache를 재사용하는 최적화가 아니다.

그 뒤 Monitor 정상 tick의 membership 3회/catalog 읽기 4회와 web overview의 membership 3회 재사용을 설계한다. membership 알고리즘만 개선하는 것과 tick 전체의 상태 일관성 변경을 구분한다. 실제 125초 DB 관찰에서 snapshot 갱신 간격은 76.624/72.059초였으나 작성자 식별자가 없어 이를 Monitor 단독 실행 시간으로 단정하지 않는다.

<a id="p-09"></a>
### P-09 — 전체 큐 ETA 조회와 수십 개 메시지 직렬 전송 (후속 실측 2순위)

[큐 내부 반복 D-13](../diagrams/D-13.svg) · [UC-19 Telegram](../diagrams/UC-19-tg.svg).

As-Is: queue_text는 전체 queued/LIVE를 읽고 queued마다 runtime_history(연결 2회)+estimate를 호출한다. 취소 버튼 20개 제한은 본문에 적용되지 않는다. 표본은 본문 생성 5.535초, history 919회, SQLite 연결 1,839회, 119,378자/35조각이었다. macro 요약과 실제 API 전송은 제외했다. send는 조각마다 동기 API 호출하며 마지막에만 keyboard를 붙인다. pause/resume도 같은 경로다.

To-Be(미구현): 전체 건수/매크로 요약과 페이지로 응답량을 제한하고 표시 항목만 ETA를 bulk 계산한다. Telegram·GUI·Web에 일관된 조회/페이지 계약을 적용한다. 한 요청의 API 수, DB 연결 수, 조회 bytes, ETA 정확성, 페이지 전환 중 큐 변경을 검증한다. 네트워크 RTT/rate limit의 실제 기여율은 전송 계측 전 미정이다.

전체 dispatcher 후속 검증은 fake transport에서도 35.888초였다. cases_for 21.837초(내부 JSON 읽기 포함), running_macro_views 7.874초, queue_text 4.586초, 전체 history 1,837회/DB 연결 3,681회였다. [D-14](../diagrams/D-14.svg)의 매크로 집계와 queue_text가 ETA/history를 재사용하지 않는 중복을 추가로 확인했다. UI 페이지화만으로 집계 비용이 사라지지는 않는다. [계측 포함 관계](live-bottlenecks.md)를 보존해 해석한다.

<a id="p-01"></a>
### P-01 — Web overview의 반복 catalog/상태 읽기 (조사 1순위)

[UC-02-web](../diagrams/UC-02-web.svg), [D-01](../diagrams/D-01.svg)

As-Is: `overview → fresh → sync_ticket_states`에서 catalog를 읽고, `Bot.cases`, `Bot.active_runs`가 각각 목록을 읽는다. `ticket_rows`는 각 티켓의 load_case/revision과 `runner.state → _members → load_case`를 수행한다. `runner._state`는 티켓마다 Store.jobs와 root별 observed를 읽는다. 마지막의 최근 100개 이력 표시도 전체 jobs 조회 후 절단한다.

확인: 위 fixture에서 40개 티켓당 load_case 200회, connections 166회, Store.jobs 42회. raw scan 비용을 제거해도 반복이 남는다.

To-Be(미구현): 요청 단위 context에 한 번 읽은 catalog/jobs/observed를 보관하고 root별 인덱스를 공용 helper에 전달한다. 서버 전역의 오래된 cache로 대체하지 않고 한 요청 내 재사용부터 적용한다. history 목록은 DB 단계의 limit/filter를 검토한다.

검증: count가 티켓별 jobs 조회에서 요청당 일정 횟수로 감소하는지, live/invalid/삭제 ticket·macro 진행률·revision 정확성이 같은지 비교한다. 변경 비용 중간, freshness/관계 검증 회귀 위험 중간. 시간 절감율은 전후 계측 전 미정.

<a id="p-02"></a>
### P-02 — Telegram stat에 표시하지 않는 ETA 데이터 준비 (조사 1순위)

[UC-02-tg](../diagrams/UC-02-tg.svg)

As-Is: `Bot.status`가 등록 run마다 `Store.runtime_history`를 읽는다. `compact_status → _context → estimate`는 controlDict 및 ETA까지 준비하지만 compact 텍스트는 이름·owner·CPU·시작·경과만 사용한다. `_context`의 다른 상세 필드 계산도 compact 표시에는 불필요하다.

확인: 40개 등록 live CASE에서 runtime_history 40회와 SQLite 연결 81회. 전체 catalog 982개와 실제 live CASE 수는 다른 변수이며 history 비용은 등록 live 수에 따라 증가한다.

To-Be(미구현): compact 전용 context를 만들고 이력/ETA를 필요한 상세 UC에서만 준비한다. 요청별 fresh ofps 전체 CASE 범위는 유지한다.

검증: 기존 compact 출력/등록 버튼/미등록 포함이 동일하고 status 경로의 runtime_history·control 조회 수가 0으로 줄어드는지 확인한다. 변경 비용 낮음, 출력 회귀 위험 낮음. 현재 운영 지연에서의 비중은 미측정.

<a id="p-03"></a>
### P-03 — Telegram main 직렬 처리와 session lock 내부 전송 (조사 2순위)

[UC-12-tg](../diagrams/UC-12-tg.svg), [UC-15-tg](../diagrams/UC-15-tg.svg), [BG-06](../diagrams/BG-06.svg)

As-Is: `serve`가 updates를 순서대로 handle한다. `Bot.send/file`은 동기 네트워크 호출이다. TicketChat.handle은 RLock을 잡고 도메인 처리와 render/editMessageText/send까지 수행한다. 검색 worker도 결과 반영 때 같은 lock을 잡는다. ACK worker는 별도지만 실제 dispatch는 여전히 main thread다.

확인: 코드상 대기 의존성. 이번 실험에는 네트워크 지연을 주입하지 않았으므로 실제 lock 점유 시간을 측정했다고 주장하지 않는다.

To-Be(미구현): 먼저 session generation을 확보한 뒤 lock 밖에서 전송하는 방안을 검토한다. 요청별 제한된 worker를 도입하면 사용자/session 내 순서와 update offset 처리 의미를 함께 설계해야 한다. 무제한 thread를 추가하지 않는다.

검증: 느린 fake API, 두 사용자, stale callback, render 재시도·/clean·재시작 상황에서 순서/초안/전송 결과가 일관되는지 본다. 변경 비용 높음, session/offset 회귀 위험 높음. 송신 RTT와 lock wait를 따로 측정한다.

<a id="p-04"></a>
### P-04 — fresh scan 대기와 추가 CPU scan (조사 2순위)

[D-04](../diagrams/D-04.svg), [BG-04](../diagrams/BG-04.svg)

As-Is: `processes.snapshot`은 process-local lock을 기다린 뒤 ofps subprocess를 최대 45초 실행한다. 45초는 lock 대기 상한이 아니다. parse_snapshot과 owner /proc 조상 탐색은 lock 해제 뒤 실행한다. 같은 봇의 stat·Monitor·검색이 경합하고 별도 web/GUI/CLI는 독립 lock이라 동시 scan도 가능하다. Scheduler는 첫 Monitor scan 외에 automatic/monitor용 snapshot과 solver/monitor CPU check를 추가 수행한다. check_cpus는 SNAPSHOT_LOCK을 쓰지 않는다.

후속 확인: 프로세스 758개/live CASE 1개인 실제 호스트에서 managed scan은 3회 약 1.06초였다. scan_and_sync는 supervisor 탐색과 application 탐색에서 /proc를 각각 순회하지만 봇이 지정한 managed 모드에서는 ticket sync를 생략한다. standalone ofps는 sync를 수행하므로 실행 모드를 구분해야 한다.

To-Be(미구현): 아직 미측정인 daemon lock 대기·CPU 검사 시간을 분리한다. scanner 자체의 /proc 접근 비용과 불필요한 연속 호출도 검토하되 현재 표본에서는 P-08이 우선이다. fresh가 요구되는 UC의 정확성과 admission 직전 재검사를 보존한다. `/stat`을 watcher cache로 바꾸지 않는다.

검증: process 수·MPI rank·동시 요청·Monitor 겹침별 시간, 관측 누락·CPU 예약 회귀. 변경 비용 중간~높음, 외부 프로세스 freshness 위험 높음.

<a id="p-05"></a>
### P-05 — ticket flock, fsync, DB writer 및 이력 누적 (조사 2순위)

[D-02](../diagrams/D-02.svg), [D-09](../diagrams/D-09.svg), [BG-03](../diagrams/BG-03.svg)

As-Is: catalog 읽기도 배타 flock이다. save는 lock 안에서 전체 티켓 중복 검사와 파일 쓰기를 수행한다. macro 발행은 child 수만큼 atomic_json/fsync/load_case를 반복한다. Monitor는 별도로 accept_submissions와 sync_ticket_states에서 catalog를 다시 읽고, recover가 한 tick에 두 번 호출되며 Scheduler는 모든 terminal job의 terminal_event를 재방문한다. Store는 호출마다 새 SQLite 연결이며 write 경쟁 시 최대 30초 기다릴 수 있다.

To-Be(미구현): bulk 읽기/동일 tick context 재사용, 변경된 티켓만 검증하는 안전한 인덱스, terminal event 재방문 범위 축소를 검토한다. fsync 제거처럼 내구성을 바꾸는 최적화는 별도 결정한다.

검증: 대규모 macro 저장 중 stat/open/scan과 동시 실행, revision·rollback·멱등·outbox 복구. 비용 중간~높음, 파일/DB 일관성 위험 높음.

<a id="p-06"></a>
### P-06 — 실행 경로와 mutation freshness 차이 (구조 개선 후보)

[D-03](../diagrams/D-03.svg), [UC-18-legacy](../diagrams/UC-18-legacy-tg.svg), [UC-27](../diagrams/UC-27.svg)

As-Is: 세 편집기는 TicketRunner.request를 사용하지만 Telegram legacy enqueue와 CLI는 Store.enqueue를 직접 사용한다. web 일반 저장·삭제는 fresh+sync, TG/GUI는 JSON queue 상태 검사다. 이 차이는 구현 이해·계측 지점·정책 유지 비용을 높인다. 속도 병목이라고 단정하지 않는다.

To-Be(미구현): 정책을 하나로 정한 뒤 공용 명령 service를 통해 연결한다. JSON 제출과 직접 DB 접수의 호환성·응답 의미·request ID를 먼저 명세한다. validation/read-only 경로에 불필요한 scan을 넣지 않는다. 비용 중간, 실행 재진입·동작 호환성 위험 높음.

<a id="p-07"></a>
### P-07 — 브라우저 연쇄 왕복·artifact glob·로그 재읽기 (조사 3순위)

[UC-12-web](../diagrams/UC-12-web.svg), [D-10](../diagrams/D-10.svg), [D-07](../diagrams/D-07.svg)

As-Is: saveNow는 export별 validation HTTP를 순차 호출한 뒤 save → cases → overview를 기다린다. overview는 live log를 state 없이 읽으므로 각 polling에서 bounded tail을 재분석한다. export_files는 max_files로 절단하기 전에 모든 glob 결과를 탐색·정렬한다. web artifacts는 그룹마다 case catalog를 재조회할 수 있다.

To-Be(미구현): 공용 validate_export 규칙을 유지한 batch validation, 요청 내 case/log 재사용, 안전한 파일 결과 제한 전략을 검토한다. 비용 중간, 파일 freshness·path guard 위험 중간. 폼 자동 저장/요청 데이터 자동 전송으로 의미를 바꾸지 않는다.

## 4. 운영 계측 계약과 전후 검증

[#18](https://github.com/jo0n-lab/cfd-bot/issues/18)의 진단 trace 구현과 연결한다. 이번 문서 작업은 운영 로깅을 추가하지 않는다.

| 구간 | timestamp/측정 지점 | 분리할 값 |
|---|---|---|
| 요청 | update 수신/HTTP 진입/Tk event | 대기열 대기, handler 실행, 사용자가 결과를 보는 시점 |
| snapshot | lock 직전/직후, subprocess 전/후, parse 후 | lock wait, scanner 실행, parse /proc I/O |
| 티켓 | catalog 시작/끝, flock 전/후, load_case 횟수 | lock wait, 파일 수/읽은 bytes, JSON 변환/검증 |
| DB | connect/BEGIN 전후/query/commit | 연결 수, writer wait, 읽은 job 수, decode 비용 |
| 알림·응답 | API 시작/끝, retry, checkpoint | ACK와 실제 메시지 구분, queue wait와 RTT 구분 |
| UI | response 수신/DOM·widget 반영 | backend 시간과 화면 갱신 시간 |

각 표본에 UC/platform, request/trace ID, component, PID/thread, ticket/live/process/job 수를 기록하고 내용/인증값은 수집하지 않는다. 동일 입력과 동시성 조건에서 warmup·반복수·p50/p95/max·오류율·호출 수를 전후 비교한다. 테스트는 fake Telegram, fake scanner, 임시 case와 DB를 사용한다. 운영 계측이 추가되면 실제 지연 분포와 잠금 대기를 별도로 채운다.

## 5. 기존 개선과 현재 범위

[#10](https://github.com/jo0n-lab/cfd-bot/issues/10)의 managed ofps 내부 sync 생략, callback ACK 비동기, stat 요청의 표시 외 JSON sync 제거는 현재 코드에 반영되어 있다. 이 비용을 아직 존재하는 중복으로 적지 않았다. 그 뒤에도 catalog/이력 조회, main 직렬 전송, process-local scan 경합과 별도 프로세스의 반복 scan은 남아 있다.

기존 불변식의 end_time/legacy auto-export 차이, 플랫폼 기능 차이는 [ARCHITECTURE 발견 사항](../ARCHITECTURE.md#4-현행과-설계-원칙의-차이)에 별도로 기록했다. 이번 작업은 재설계 근거와 검증 가능한 문서를 제공하며, 제안한 최적화의 성능 개선 완료를 의미하지 않는다.
