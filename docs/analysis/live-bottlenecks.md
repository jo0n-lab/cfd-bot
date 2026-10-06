# 921행 매크로 운영 데이터 읽기 전용 분석

2026-10-04 KST. [원시 집계](live-readonly-results.json) · [함수 내부 LLD](../LLD.md#catalog) · [기존 격리 실험과 개선안](performance.md). 운영 티켓 내용·경로·인증값은 결과에 저장하지 않았다. 제품 코드는 수정하지 않았고 실제 Telegram 전송, 티켓 동기화, Monitor 실행, 계산 시작을 하지 않았다.

## 측정 범위와 현재 규모

호스트의 실제 `/proc`를 대상으로 **managed ofps**를 세 번 실행했다. 샌드박스의 `/proc`는 2개만 보이므로 scanner 계측은 호스트 접근 권한을 얻어 실행했다. 측정 호스트에는 프로세스 758개, snapshot에는 solver 1개·supervisor 1개·live CASE 1개가 있었다. `CFD_BOT_OFPS_MANAGED=1`이므로 scanner의 JSON/DB 동기화는 생략했다. 실제 JSON을 읽고, DB는 `mode=ro`로 열었다. 기존 `tickets_for`의 읽기용 flock을 사용하며 티켓 내용은 변경하지 않았다.

JSON은 총 1,007개: macro 1개/921행, child 945개, standalone 61개였다. 부모 목록에 포함된 child 921개만 catalog에 남으므로 반환 Case는 982개다. 나머지 child 24개는 부모 파일은 있지만 현재 구성원 목록에 없었다. 이는 이 함수가 제외하는 상태이며 이번 분석에서 삭제하거나 손상으로 단정하지 않았다.

실행이 진행 중이라 DB queued 수는 처음 집계 919개, 다음 집계 918개였다. **921은 macro 구성원 수이고 같은 시각의 queued 수와 동일하지 않다.** 독립 단계 측정은 동일 transaction의 스냅샷도, Telegram 요청 전체 trace도 아니다.

## `/stat` 단계별 실측

| 단계 | 시간 | 포함·제외 |
|---|---:|---|
| managed ofps subprocess | 1.054 / 1.061 / 1.057초 | `/proc` scan + stdout; daemon의 SNAPSHOT_LOCK 대기는 제외 |
| parse_snapshot | 0.00194초 | stdout 해석·PID/owner/CPU 보강 |
| tickets_for | 1.008초 | 1,007개 JSON 로드·검증. flock 획득 대기는 표본에서 0.000008초 |
| cases_for(config, tickets) | **16.562 / 19.684초** | 이미 읽은 tickets의 child-parent membership; JSON 읽기 제외 |
| active_runs | 0.000515초 | 982개 catalog와 live CASE 1개 연결 |
| status + history | 0.00421초 | 이 표본의 live CASE 1개 본문 생성; 전송 제외 |
| queued jobs 읽기·JSON decode | 0.0323초 | 당시 918개 DB row, 비교용 별도 단계 |

45초는 `processes.snapshot → subprocess.run(timeout=45)`에 지정한 제한이며 실측값이 아니다. lock 대기와 이후 catalog·membership·전송은 그 timeout의 범위 밖이다. 현재 표본의 큰 비용은 ofps 뒤의 **cases_for 중첩 검색**이다. 소수 live CASE/대규모 macro 표본이므로 이력 조회가 `/stat`의 주원인이라는 기존 가설보다 membership 문제가 우선한다.

## 중첩 검색이 44만 번의 경로 확인을 만드는 이유

[config.cases_for](../../cfd_bot/config.py#L394)는 부모 dict lookup 후에도 `any(...)`로 부모 rows를 매번 다시 검색한다. 921개 유효 child의 검사 횟수는 `921×922/2=424,581`, 목록 밖 child 24개는 `24×921=22,104`다. 합계 **446,685개 row 조건**, 부모 경로 945회를 더하면 **447,630회 Path.resolve**다. 현재 입력을 원래 조건/순서에 대입해 산출했으며 OS syscall trace의 호출 수는 아니다.

계측 호스트의 `/usr/lib64/python3.9/pathlib.py`와 `posixpath.py`를 확인했다. resolve는 realpath의 경로 성분별 lstat/필요 시 readlink와 마지막 stat을 포함한다. 각 resolve 호출 사이에 macro row 경로 결과를 재사용하지 않는다. 이 때문에 O(N²) Python 반복이 반복적인 파일 메타데이터 접근으로 확대된다.

읽어 둔 같은 tickets로, 부모마다 `(resolved ticket path, case root)` 집합을 한 번 만든 뒤 child를 조회하는 **메모리 내 비교용 구현**은 0.0952초였고 반환 982개 및 순서가 같았다. 현재 표본에서만 동등성을 확인했으며 제품에는 적용하지 않았다. symlink 변경·부모 부재·중복·부분 발행·동시 파일 교체까지 검증한 최적화 완료 결과가 아니다.

## Monitor와 다른 화면에서의 증폭

[Monitor.tick](../../cfd_bot/monitor.py#L102)의 정상 경로는 시작 시 catalog, accept_submissions의 catalog, 마지막 sync_ticket_states의 catalog를 별도로 준비한다. **cases_for 3회, tickets_for 4회**이며 같은 결과를 공유하지 않는다. 첫 catalog는 snapshot 전, 나머지 둘은 snapshot 저장 후다. Scheduler의 조건부 재조회나 Web 요청은 별도다.

serve의 monitor_loop는 run_once 완료 후 `stop.wait(poll_seconds)`를 호출한다. 따라서 poll_seconds=5는 처리 시간을 포함한 주기가 아니다. 125초 동안 DB의 snapshot.at을 수동 관찰했을 때 갱신 간격은 **76.624초, 72.059초**였다. kv에는 작성자 식별자가 없어 이 간격을 Monitor 단독 tick 실행 시간으로 단정하지 않는다. 다만 저장 snapshot이 5초마다 바뀌지 않았다는 사실은 확인했다.

Telegram `/queue`는 catalog 한 번과 macro 목록용 JSON 전체 재조회를 수행한다. Web overview는 fresh→sync, Bot.cases, Bot.active_runs에서 membership을 총 세 번 검사하고 티켓별 상태 조회도 추가한다. GUI 큐 새로고침도 catalog 비용을 부담한다. **같은 공용 함수의 내부 반복과 호출자의 반복을 함께** 개선해야 한다.

## 큐 화면의 두 번째 병목

[report.queue_text](../../cfd_bot/report.py#L126)를 현재 DB에 대해 읽기 전용으로 실행했다. 본문 생성 **5.535초**, runtime_history **919회**, SQLite 연결 **1,839회**였다. Store.jobs 한 연결 + 각 active/queued의 history와 observed 조회 두 연결 구조다. queued 원문을 읽는 0.032초와 모든 항목의 ETA를 계산하는 비용을 구분해야 한다.

본문은 **119,378자**, 실제 chunks 함수 기준 **35조각**이었다. macro 요약·dispatcher·네트워크는 제외한 수치다. 취소 버튼의 20개 제한은 본문에는 적용되지 않는다. `Telegram.send`는 조각마다 동기 sendMessage를 수행하고 마지막 조각에만 버튼을 붙인다. 모든 조각의 처리 또는 예외 반환 전까지 main thread는 다음 update를 처리하지 않는다. pause/resume도 동일 화면 생성 경로다.

네트워크 전송을 하지 않았으므로 35회의 실제 RTT·rate limit·재시도 시간을 추정치로 채우지 않았다. 이 구간은 구조상 확인된 지연 증폭 요인이며 운영 기여율은 아직 미측정이다.

### 전체 dispatcher 추가 검증: 네트워크 없이도 35.9초

19:05에는 [읽기 전용 dispatcher 측정](measure_queue_dispatch.py)으로 실제 Bot.dispatch(queue)와 Telegram.send를 실행했다. Telegram.call만 fake로 대체했고 반환 Message ID를 만들지 않아 로컬 전송 이력도 기록하지 않았다. DB는 read-only/query_only였다. 이 경로는 ofps를 호출하지 않아 호스트 /proc 접근이 필요 없으며 샌드박스에서 실행했다.

| 실제 호출 | 횟수 / 시간 |
|---|---:|
| Bot.dispatch(queue) 전체, fake transport | **35.888초** |
| cases_for (내부 tickets_for 포함) | 1회 / 21.837초 |
| tickets_for (위 내부 1회 + macro용 별도 1회) | 2회 / 합계 2.506초 |
| running_macro_views | 1회 / **7.874초** |
| queue_text | 1회 / **4.586초** |
| runtime_history / SQLite connect 전체 | 1,837회 / 3,681회 |
| sendMessage fake 호출 / keyboard payload | 35회 / 마지막 1개 |

cases_for 시간에 tickets_for 일부가 포함되므로 각 행을 합산하면 이중 계산한다. 119,470자 최종 본문을 생성했으며 당시 큐 진행에 따라 앞선 queue_text 표본과 다르다. 실제 요청 도착 전 대기와 API RTT는 제외한 **dispatcher backend 실행 시간**이다.

[running_macro_views 내부](../diagrams/D-14.svg)는 미완료 row마다 `_remaining → estimate → basis unknown이면 runtime_history → estimate`를 호출한다. 그 뒤에야 batch 중앙값으로 보완한다. 곧이어 queue_text가 같은 job들의 history/ETA를 다시 조회한다. **페이지 분할만 바꿔도 매크로 집계의 전체 조회는 남으므로** 요청 단위 history/estimate 재사용과 bulk 조회도 함께 필요하다.

## 개선 순서와 검증 조건

1. **공용 cases_for의 membership 색인**: 요청 내에서 부모 rows를 한 번 정규화하고 조회한다. missing parent/미발행 child 제외, 경로·root 동시 일치, 반환 순서·중복 root 오류를 보존한다. 영구 cache나 watcher cache로 바꾸지 않는다.
2. **Monitor/overview에서 catalog 재사용**: 단순히 함수 인자를 전달하는 것으로 끝내면 membership을 세 번 반복할 수 있다. 최종 validated cases까지 재사용하되 accept_submissions·sync 사이 티켓 변경과 revision 의미를 설계한다.
3. **큐 요약·페이지 단위 조회와 ETA 일괄 읽기**: 전체 900여 행을 전송하지 않도록 응답을 설계하고, 필요한 job들만 history/observed를 묶어 읽는다. UI 기능을 바꾸면 Telegram·GUI·Web에 함께 적용한다.
4. **요청 trace**: daemon 안의 lock wait, main update 대기, API RTT, 동시 Monitor/HTTP의 영향을 따로 측정한다. 독립 함수 시간을 실제 사용자 지연 전체로 합산하지 않는다.

로직 수정은 별도 As-Is/To-Be와 검증을 거쳐야 한다. 여기서는 문서와 분석만 반영했다.

## 재현 도구

[단계별 읽기 전용 측정](measure_live_readonly.py)은 저장소의 managed scanner만 허용하고 Telegram API·Monitor·sync를 호출하지 않는다. [큐 본문 측정](measure_queue_readonly.py)은 DB를 read-only/query_only로 열고 전송 없이 chunks 수만 센다. 둘 다 `/tmp/cfd-architecture-review/`에 집계만 기록하며 import 시 실행하지 않는다. 정상 호스트 /proc 접근 여부와 현재 작업량에 따라 결과가 달라진다.

```bash
python3 docs/analysis/measure_live_readonly.py
python3 docs/analysis/measure_queue_readonly.py
python3 docs/analysis/measure_queue_dispatch.py
```
