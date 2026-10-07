# 로그 지연·중복과 숫자 코드/해시 사전 검토 (#30)

**로그 기록 방식에 개선이 필요하다. 반복되는 메타데이터·예외 스택과 과도하게 세밀한 helper 계측이 확인됐다.** 숫자 코드+별도 사전은 현재 정제된 로그를 동일하게 복원하면서 약 25~47%의 크기를 줄였다. 하지만 파일 쓰기를 없앤 실험에서도 대부분의 지연이 남았다. 코드 치환만으로 응답 성능이 해결됐다고 볼 수 없다.

[이슈 #30](https://github.com/jo0n-lab/cfd-bot/issues/30) · [별도 signal 코드 사전 명세](diagnostic-codebook-proposal.md) · [사전 As-Is/To-Be](../history/2026-10-07-diagnostic-overhead-review.md) · [기존 성능 자료](diagnostic-performance.md). 대상 제품 소스는 PR #29의 `0306c0d`이며 이번 변경은 분석 도구·문서뿐이다.

## 원인 1: 호출 수와 범용 요약·직렬화 비용

921개 티켓/활성 CASE 40개의 synthetic fixture를 사용했다. 이전 benchmark와 같은 CPU 설정 누락 경로와, `.process-core` 및 비실행 `Allrun` 파일을 갖춘 반환 경로를 구분했다. 실제 Telegram 전송·계산 시작은 하지 않았다. 아래 응답 시간은 같은 revision의 3회 표본 중앙값이며 이전 보고서의 p95와 직접 비교하면 안 된다.

| 921개 web 조회 | OFF ms | ON ms | 함수 호출 | 함수 raise | 별도/내장 분기 | 로그 파일 bytes |
|---|---:|---:|---:|---:|---:|---:|
| CPU 설정 누락 | 340.18 | 1328.69 | 13,193 | 2,643 | 884 / 10,160 | 14,181,978 |
| CPU/Allrun 설정 있음 | 922.42 | 7206.02 | 133,009 | 0 | 3 / 130,857 | 79,824,300 |

설정이 있는 경로는 `capacity_status → occupied_cpus`가 각 대기 티켓에서 40개 관측 프로세스의 CPU를 다시 읽는다. 이는 기존 업무 호출 구조다. `cpu_set` 37,002회, `UiCatalog.text/value` 각 38,043회로 이 세 helper가 전체 함수 호출의 약 85%다. 여기에 모두 call/return과 입력/결과 요약을 붙여 133,009개 호출·반환 쌍이 됐다. 예외가 없어진 정상 반환 경로에서도 기록 폭증이 더 심해질 수 있다. 업무 판단·루프 자체를 이번 작업에서 변경하지 않았다.

cProfile의 설정 있음 경로에서 범용 `summary`는 재귀 포함 818,101회, 로그 `_write`는 267,382회 호출됐다. self CPU 상위는 JSON encoder, summary, 호출 context 생성, writer, step 처리였다. cProfile은 자체 overhead가 있으므로 그 시간은 서비스 응답 지연과 동일하지 않으며 cumulative 시간을 서로 더하지 않는다.

### 기록 단계 제거 실험

같은 임시 객체에 대해 mode 순서를 고정 seed로 섞고 각 3회 측정했다. `no_write`와 `no_summary`는 비용 구분용 임시 mock이며 제품 변경안이나 성능 합격 결과가 아니다. 각 실험은 겹치는 비용이 있어 차이를 합산할 수 없다.

| 시나리오 | OFF | 전체 ON | JSON 유지, 파일 대신 /dev/null | _write 우회 | 입력·결과 요약 우회 |
|---|---:|---:|---:|---:|---:|
| web_overview | 340.18 | 1328.69 | 1283.78 | 933.41 | 902.64 |
| log_tail_2000_lines | 14.18 | 94.76 | 94.01 | 59.70 | 74.01 |

단위 ms. web에서 파일 대신 `/dev/null`로 보내도 1,328.7 → 1,283.8 ms에 그쳤다. 출력·직렬화 진입을 우회해도 933.4 ms로 OFF 340.2 ms보다 훨씬 크다. 호출 context·시각·순서·branch 수집·입력 요약도 주요 원인이다. 단순 비동기 파일 writer만으로 전부 해소된다는 근거가 없다. fsync가 없는 page-cache 조건이라 느린 디스크의 최악 상황까지 배제한 것은 아니다.

## 원인 2: 중복 정보와 진단 가치가 낮은 기록

| 항목 | 관측/코드 근거 | 판단과 개선 후보 | 보존할 내용 |
|---|---|---|---|
| 같은 예외의 재기록 | 설정 누락 881개 ticket에서 execution_case→_capacity→_state 각 881 raise, states catch 881개. 예외가 든 전체 기록 3,527개, exception 필드 약 2.92 MB | 실제 여러 실패로 오인하면 안 됨. 최초 error 정의+전파별 참조/추가 frame 권장 | 발생별 error ID, 모든 전파/처리 위치, 원인 chain과 최초 오류 |
| 예외마다 즉시 flush | `_Call.finish(exc)`와 except step이 flush. 위 요청 3,640 JSONL 줄; 128개 묶음만으로 기대한 것보다 자주 배출 | 같은 예외 전파의 스택 재생성·동기 배출 반복을 줄이는 후보 | 최초 원인과 종료/미처리 예외의 기록 보존, crash 손실 범위 명시 |
| 함수명/caller/ID 반복 | 설정 있음 요청에서 function 문자열 약 5.19 MB, caller 약 8.67 MB. 같은 PID/trace/함수명이 반복 | 정적 함수·caller ID와 header/context 참조로 제거 가능한 표현 중복 | 어떤 함수가 누구를 언제 호출했는지 |
| parser 줄별 기록 | 2,000줄에서 feed 2,000회, 총 2,004 call/return 쌍, 내장 branch 10,002개. None 반환 2,000개, 동일 input 요약 999회씩 두 종류 | 각 줄의 같은 설명·None·길이/상태 요약은 가치가 낮음. 원본 로그의 byte offset과 branch ID를 묶는 후보 | 실제 순서/offset, 경고·오류·상태 변화 및 caller |
| UI 순수 helper | 1,000행 한 번 렌더에서 esc 7,000회, 전체 10,001 call/return 쌍 중 70% | esc/attr/badge의 매번 긴 JSON과 범용 입력/반환 요약은 과함. 작은 코드·반복 배열 후보 | 버튼/화면/요청과 연결된 호출 경로 및 예외 |
| Telegram 같은 update 도착 | `_Call.begin`이 Bot.handle과 TicketChat.handle 모두 ui.telegram.received 기록 | 수신은 한 번, editor 라우팅은 별도 짧은 routed code로 구분 후보 | 두 처리 단계와 동일 update/actor 연결 |
| 읽기 context의 commit 라벨 | `_Connection.__exit__`가 in_transaction 없이 정상 종료 모두 commit으로 기록. 표본 91 commit 중 acquired 2개 | DB context 종료와 실제 commit 구별 필요. 단순 소음뿐 아니라 의미 정확성 문제 | 실제 쓰기 transaction과 commit/rollback의 시각 |
| source map + function.definition | 같은 정적 정의가 map과 최초 호출 시 함께 존재 | 한 번의 정의라 우선순위는 낮음. 사전 참조로 통일 가능 | 해당 배포 버전의 파일/함수/line 해석 |

반복 실행은 정보 중복과 다르다. 각 티켓의 동일한 실패·별도 클릭·서로 다른 작업 등록·실제 SQL 실행을 사건 자체가 같다고 합치거나 삭제하면 안 된다. 전체 함수/HLD·LLD 추적 요구가 있으므로 저가치 helper도 이번 검토에서 일괄 삭제하지 않았다. 반복 metadata/동일 payload를 참조로 바꾸고 각 occurrence의 순서·식별값·결과를 유지하는 것이 우선이다.

## 브라우저 실측

합성 티켓 1,000행 한 번 렌더: **20,002개 event, 10,472,259 bytes**. `esc` 7,000회, filter/map callback 각각 1,000회, badge 1,000회, ticketList 1회다. 모든 event는 현재 즉시 JSON.stringify되어 main thread에서 처리된다. 5회 렌더의 Chromium CPU 표본 541개에서 append·summary와 GC가 상위였다. 여기의 128 MiB 버퍼는 단일 렌더 원자료 유실을 막기 위한 임시 분석 설정이며 제품 8 MiB 설정은 변경하지 않았다. buffer가 확장됐으므로 GC 표본을 실제 8 MiB 환경과 동일한 비율로 해석하지 않는다.

## 숫자 코드·해시·압축 비교

현재 정제된 기록을 대상으로 오프라인 변환 후 원본과 동일한지 assert로 확인했다. **사전 bytes를 포함**한 크기다. 숫자 사전 실험은 key ID와 반복 문자열 ID를 사용한다. SHA-256 실험은 input/result/caller 중 120 bytes 이상의 object/array를 정규화한 값 사전으로 참조한다. 따라서 모든 가능한 hash 설계의 최적값을 뜻하지 않는다. 원자료 전체를 본 뒤 사전을 만들었으므로 streaming 환경에서 항상 같은 절감률이 나오지는 않는다.

| 원자료 | 현재 bytes | 숫자 사전 포함 bytes | 감소 | SHA-256 값 사전 포함 bytes |
|---|---:|---:|---:|---:|
| web 설정 누락 | 14,181,981 | 7,462,279 | 47.4% | 14,539,299 |
| web 설정 있음 | 79,824,303 | 49,799,774 | 37.6% | 80,171,673 |
| parser 2,000줄 | 1,379,250 | 1,036,625 | 24.8% | 1,302,311 |
| browser 1,000행 | 10,472,261 | 5,696,232 | 45.6% | 8,537,590 |

**숫자 코드 사전은 권장 검토안이고, 모든 payload를 매번 SHA-256으로 만드는 방식은 권장하지 않는다.** 이 hash 실험은 web 설정 누락에서 오히려 약 2.5% 증가했다. 값이 매번 달라 사전에 한 번만 나오는 항목, 64자리 digest 비용, canonical JSON 생성 비용이 있다. 짧은 정수 값 참조도 해당 web 표본에서 절감은 약 1.6%에 그쳤다. 정적 event/함수/필드 문자열부터 제거하는 편이 유리했다.

gzip level 1은 현재 web 설정 누락 원자료+사전을 1,135,081 bytes, browser를 397,800 bytes로 줄였다. 설정 있음 약 79.82 MB도 9.22 MB로 줄었지만 압축 CPU가 약 708 ms 추가됐다. 이미 만든 큰 로그를 나중에 압축하는 것은 보관량 개선이며 생성 지연 해결과 다르다. 우선 회전 완료 파일/export 압축으로 검토한다.

숫자 코드로 줄인 설정 있음 JSON의 직렬화 CPU는 약 2.34초로 원형 2.09초보다 오히려 컸다. tagged array를 더 만든 프로토타입 탓도 있으며 변환·왕복검증 비용도 별도다. 결과 JSON의 `transform_and_roundtrip_cpu_ms`는 변환+복원검증이고 사전 빈도 사전탐색은 제외한다. 어느 숫자도 구현 후 요청 지연 개선을 입증하지 않는다. 런타임에서는 event 생성 단계부터 빌드 시 결정한 code와 작은 필드 배열을 직접 써야 한다.

## 권고와 다음 검증

1. 빌드 시 event/function/caller/field code를 생성하고 별도 JSON/Markdown 사전을 함께 배포한다. 사전 버전은 SHA-256, 매 사건 참조는 작은 정수로 둔다.
2. 동일 예외 스택은 최초 정의와 error ID·전파/처리 경로로 표현한다. 기존 함수의 반환·raise·업무 검증을 변경하지 않는다.
3. 브라우저 순수 helper와 parser 반복의 긴 메타데이터·무의미한 동일 요약을 줄인다. 모든 occurrence가 복원되는 작은 buffer/반복 배열을 우선 검토한다. 요약만 남겨 호출을 누락하는 설계는 별도로 구분한다.
4. 원본 의미를 복원하는 fixture 비교와 동일 최종 revision의 OFF/ON CPU·p50/p95·기록량을 다시 확인한다. 이번 분석에서 새로운 런타임 최적화나 운영 배포는 수행하지 않았다.

## 재현 / 자료

```bash
python3 docs/analysis/audit_diagnostics.py --output /tmp/cfd-log-audit-new
python3 docs/analysis/audit_diagnostics.py --output /tmp/cfd-log-audit-ready-new --valid-cpu --modes off full --scenario web_overview
# 별도 터미널에서 임시 fixture를 켠 뒤 browser 분석, 종료 후 Ctrl-C로 정리
CFD_BOT_DIAGNOSTICS=1 python3 -m tests.web_fixture
node docs/analysis/audit_browser_diagnostics.cjs /tmp/cfd-log-audit-new
```

Browser에는 설치된 Playwright와 Chromium이 필요하다. `PLAYWRIGHT_MODULE`/`PLAYWRIGHT_BROWSERS_PATH`로 경로를 지정할 수 있다. 분석 directory는 새 경로를 사용한다. 실험 ablation raw logs는 크기가 크므로 /tmp에만 두고 아래 요약 결과만 저장소에 보관했다. 선택 mode별 fresh configure/close 및 source map 초기화 시간은 요청 측정 밖이다. cold startup·동시 요청·실제 사용자의 입력/네트워크·장기 disk 영향은 남은 측정 범위다.

- [audit-missing-settings.json](diagnostic-results/audit-missing-settings.json)
- [audit-ready-settings.json](diagnostic-results/audit-ready-settings.json)
- [audit-browser.json](diagnostic-results/audit-browser.json)
- [audit-browser-codecs.json](diagnostic-results/audit-browser-codecs.json)
- [Python 재현 도구](audit_diagnostics.py)
- [Browser 재현 도구](audit_browser_diagnostics.cjs)
