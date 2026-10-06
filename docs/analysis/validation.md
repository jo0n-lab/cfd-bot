# 문서·그림·기능 검증 결과

기준: 2026-10-06, [소스 기준](source-manifest.json), [#21 변경 이력](../history/2026-10-06-capacity-aware-parallel-execution.md).

| 확인 | 결과 |
|---|---|
| `python3 -m compileall -q cfd_bot tests` | 통과 |
| `python3 -m unittest discover -s tests -q` | 305개 실행, OK, skipped=1 |
| `python3 -m cfd_bot --config bot.json check` | 통과, 982개 케이스 설정 정상 |
| `python3 docs/analysis/benchmark_architecture.py` | 완료, 합성 snapshot·임시 DB/파일; 원시 결과 저장 |
| `python3 docs/diagrams/build_flows.py` | 함수 요청·응답 시퀀스 SVG 생성 |
| `python3 docs/analysis/build_reference.py` | 388개 Python 함수 소스 색인·그림 갤러리 생성 |
| `python3 docs/diagrams/build_overviews.py` | 기존 5개 SVG 경로 갱신 |
| `python3 docs/analysis/validate_docs.py` | 결과는 아래 최종 검사 기록 참조 |

첫 테스트 실행은 샌드박스의 loopback 소켓 생성 제한으로 웹 테스트 12개가 오류, scanner 테스트 1개가 실패했다. 권한을 확보한 동일 전체 테스트 재실행은 통과했다. `Notification retry: rate limit` 출력은 fake API 재시도 테스트에서 의도한 로그다.

skipped=1은 Tk WidgetTests의 class setup이다. 따라서 공용 GUI 도메인/adapter 테스트와 실제 Tk 위젯 동작을 구별한다. 현재 환경에서 실제 데스크톱 전체 클릭, Windows/macOS 네이티브 실행, Playwright 브라우저 end-to-end는 이번에 실행하지 않았다. 기존 fake HTTP/API·launcher 단위 검증 결과를 그 범위 이상으로 해석하지 않는다.

실제 Telegram 전송·실제 OpenFOAM 계산·서비스 재시작은 수행하지 않았다. 제품 Python/JS/스크립트 등 42개 파일은 작업 시작 전후 SHA-256이 동일하다. 기존 미커밋 기능 변경은 보존했다. check의 운영 케이스 목록은 문서에 복사하지 않고 집계만 기록했다.

## 그림과 링크 검사

모든 새 시퀀스 그림은 가로 participant header, 세로 lifeline, activation, 실제 함수 요청과 반환, 외부 ofps/Telegram API, 비동기 후속 처리, 오류 설명을 가진 독립 SVG다. `rsvg-convert`로 실제 PNG를 렌더링하고 XML·이미지 파일·텍스트 경계·내부 링크를 검사한다. 대표 그림은 육안 검토한다. 렌더링 결과는 `/tmp/cfd-architecture-review/rendered/`에 두며 생성 PNG를 저장소에 중복 보관하지 않는다.

## 최초 재작성 검사 기록

2026-10-04 검사 결과: 설계·상세·분석 Markdown 12개에서 로컬 링크 1,812개, 소스 기준 해시 34개를 확인했다. 시퀀스 96개와 개요·호환 경로 5개를 합한 SVG 101개의 XML·내부 링크·PNG 렌더링 검사가 통과했다. 시퀀스 텍스트 경계 검사에서도 잘림이 발견되지 않았다.

Telegram `/stat`, GUI 비동기 매크로 검색, 시스템 HLD의 실제 렌더링을 육안으로 확인했다. `/stat` 그림에는 `processes.snapshot → subprocess.run → bin/ofps → stdout/returncode → parse_snapshot`과 Telegram API 호출·응답을 직접 표시했다. 전체 기능 테스트는 272개 실행, OK(skipped=1)이며 실제 Tk 위젯·브라우저·네이티브 launcher의 수동 검증 범위는 위 제한을 따른다.

## 내부 반복 누락 보완 후 검증

D-01과 Telegram stat에 child×macro row 중첩 loop·Path.resolve의 파일 접근·catalog flock 해제 시점을 추가했다. D-13은 전체 큐 ETA/본문, D-14는 매크로 ETA와 본문의 중복 조회를 그린다. 가로 participant·요청 실선·반환 점선 형식은 유지했다. D-01/D-13의 실제 렌더링과 loop 프레임을 육안 검토했다.

후속 최종 검사는 Markdown 13개·로컬 링크 1,890개·소스 기준 해시 34개, 시퀀스 98개와 개요 5개를 합한 SVG 103개가 모두 통과했다. XML·SVG 내부 링크·텍스트 경계·PNG 실제 렌더링 오류가 없었다. 분석/그림 생성 Python의 compileall과 git diff --check는 통과했다. 읽기 전용 계측 도구의 import가 측정을 실행하지 않는 것도 확인했다. 제품 파일 42개 해시는 최초 기준과 동일하므로 기존 기능 테스트를 다시 실행하지 않았다.

[운영 데이터 분석](live-bottlenecks.md)은 host managed ofps와 read-only SQL을 이용한 독립 함수 측정, snapshot.at의 수동 관찰, fake API를 사용한 queue dispatcher 측정을 구분한다. 실제 Telegram 전송·큐 변경·계산 시작은 없었다. API latency와 daemon 내부 요청/lock 대기는 이 결과에 포함하지 않는다.


## #20 구현·운영 반영 검증

위 기록은 문서 재작성 당시의 결과다. 이후 #20 구현은 compileall, 전체 unittest 292개(통과, Tk display 관련 1개 skip), 배포 전후 운영 bot.json check(982개 CASE)를 통과했다. 2026-10-04 23:57 KST 운영 반영 후 서비스와 웹 health, 실행 계산 및 대기 큐 보존을 확인했다. [배포 근거와 검증 한계](../history/2026-10-04-ticket-index-incremental-monitor.md)를 참조한다.

## #21·#22 용량 기반 병렬 실행과 CPU binding 검증

2026-10-06 기준 compileall, 전체 unittest 292개(통과, Tk display 관련 1개 skip), `bot.json check` 982개 CASE, `git diff --check`, `bash -n bin/ofps`가 통과했다. 문서 검사는 Markdown 13개·로컬 링크 1,965개·소스 fingerprint 35개를 확인했고 SVG 104개를 XML parse한 뒤 `rsvg-convert`로 104개 PNG를 실제 렌더링했다. 링크·텍스트 경계·렌더링 오류는 없었다.

호스트에서 실제 `bin/ofps`를 실행하여 `DS_CART_NQ_0133`의 40개 `foamRun` affinity가 `.process-core`와 동일한 `0-19,26-45`임을 확인했다. 이 실행은 OpenMPI의 기본 ODLS spawn thread 4개 때문에 PID가 일부 CPU 순서와 다르게 생성됐다. 수정된 Akita `run_parallel.sh`에 `odls_base_max_threads=1`을 적용하고 thread-pool 조건을 강제한 12-rank probe에서 rank 0..11, PID 오름차순, CPU `20-25,46-51`이 일치했다. Akita 1,006개 스크립트와 TCB `of-main/Allrun.solve`는 같은 옵션 적용 후 모두 `bash -n`을 통과했다.

`KillMode=process`를 확인한 뒤 bot·web user service를 재시작했다. 기존 worker `4016488`, Allrun `4016500`, mpirun `4055530`과 40개 solver PID가 그대로 유지됐고 두 service는 active 상태다. web `/api/health`는 `cfd-control-room` version 1을 반환했다.

## #21 이름 있는 대기열·동적 매크로 추가 검증

2026-10-06 재설계는 고정 3개 lane을 임의 개수 `queue_id`와 겹치지 않는 CPU quota로 바꿨다. 전용 macro queue, 같은 queue의 FIFO 직렬 실행, queue 간 독립 병렬 실행, 자식별 cores, 미예약 CPU 우선, 작은 donor quota 우선 선택, donor 자연 종료, 동적 작업 뒤 donor별 1회 우선권을 `test_named_queues.py`에 추가했다. 기존 profile 없는 티켓과 `queue_lane` job 호환도 유지했다.

전체 unittest 305개가 통과했고 Tk display 1개만 skip됐다. `bot.json check`는 운영 티켓 982개를 통과했다. UI JSON parse, JavaScript syntax, compileall, `git diff --check`가 통과했다. 문서 검사는 Markdown 13개, 로컬 링크 1,991개, 소스 fingerprint 36개, SVG 104개의 XML parse와 PNG 실제 렌더링을 확인했으며 오류와 텍스트 경계 초과가 없었다.

bot·web user service를 2026-10-06 23:38:05 KST에 재시작했고 둘 다 active 상태다. 기존 detached worker와 40-rank OpenFOAM 계산은 같은 PID로 유지됐다. web health는 `cfd-control-room` version 1을 반환했고, 재시작 뒤 `monitor_error`, `queue_drain_claim`, `queue_fair_turns`는 각각 `None`, `None`, 빈 map으로 확인됐다.

## #24 NP 기반 자동 quota 검증

2026-10-07 재설계는 티켓의 예약 CPU 입력을 제거하고 일반 head의 실제 NP, 동적 macro의 현재 child NP에서 quota를 산정한다. 일반 queue의 자동 CPU profile, 서로 다른 queue의 병렬 시작, 같은 queue의 작업별 profile 크기 재산정, 케이스 `.process-core`/`Allrun` NP 상속, 동적 donor 선택, legacy profile과 lane 호환을 `test_named_queues.py`와 세 UI adapter 테스트로 확인했다.

전체 unittest 309개가 통과했고 Tk display 1개만 skip됐다. `bot.json check`는 운영 티켓 982개를 통과했다. compileall, Telegram UI JSON parse, JavaScript syntax, `git diff --check`도 통과했다. 문서 검사는 Markdown 13개, 로컬 링크 1,994개, source fingerprint 36개, SVG 104개의 XML parse와 PNG 실제 렌더링을 확인했으며 오류와 텍스트 경계 초과가 없었다. Playwright 모듈이 없어 브라우저 E2E는 실행하지 못했지만 HTTP Web adapter 테스트는 전체 unittest에 포함했다.

bot·web user service를 재시작했고 둘 다 active 상태이며 web `/api/health`가 `cfd-control-room` version 1을 반환했다. 재시작 직전 fresh `ofps` snapshot에는 활성 계산이 없었다.
