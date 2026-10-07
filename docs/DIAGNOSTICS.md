# 사후 원인 분석용 진단 로그 (#18)

이미 일어난 문제에서 사용자 조작 → 함수 호출/반환 → 기존 판단 → 티켓/작업 변경 → 응답/예외를 연결한다. 로그 설정과 기록만 추가하며 업무 검증·실행 차단·재시도·복구 정책은 추가하지 않는다. [사전 설계](history/2026-10-07-diagnostic-sequence-logging.md), [99개 시퀀스 대응표](analysis/diagnostic-flow-coverage.md), [실측 결과](analysis/diagnostic-performance.md)를 함께 참고한다.

## OFF / basic / detailed

기존 설정에는 진단 로그가 **기본 OFF**다. ON이면 `basic`이 기본이며 `detailed`에서 모든 내부 함수/분기를 추가 기록한다. 기존 `enabled: true`만 있는 설정도 이제 basic이다. OFF 동안 발생한 문제의 상세 진단 기록은 나중에 복원할 수 없다. 사후 분석이 필요한 운영 구간에는 사전에 ON을 설정해야 한다.

`bot.json`의 공통 설정:

```json
"diagnostic_logging": {
  "enabled": true,
  "level": "basic",
  "max_bytes": 20971520,
  "backups": 3
}
```

`CFD_BOT_DIAGNOSTICS=1` 또는 `0`은 설정 파일보다 우선한다. `CFD_BOT_DIAGNOSTICS_LEVEL=basic|detailed`는 `level`보다 우선하며 알 수 없는 값은 basic으로 처리한다. 이것은 로깅 설정 선택이며 업무 검증 규칙이 아니다. `CFD_BOT_DIAGNOSTICS_DIR`로 기록 디렉터리를 지정할 수 있다. 기본은 `<state_dir>/diagnostics`; 설정의 `directory`가 있으면 해당 경로가 우선한다. 상대 경로는 bot 설정 파일 기준이다. 설정은 시작/설정 로드 시 적용하며 서비스 재기동 후 브라우저도 새로 고침한다. 워커·ofps 등 자식 프로세스에는 ON/OFF·수집 수준·디렉터리·trace/parent ID를 전달한다. 이미 실행 중인 독립 워커에는 재기동한 부모의 새 설정이 소급 적용되지 않는다.

```bash
CFD_BOT_DIAGNOSTICS=1 CFD_BOT_DIAGNOSTICS_LEVEL=basic python3 -m cfd_bot --config bot.json web
CFD_BOT_DIAGNOSTICS=0 python3 -m cfd_bot --config bot.json check
```

Telegram, GUI, web backend는 같은 Python 모듈과 설정을 사용한다. 브라우저는 응답의 `X-CFD-Diagnostics`와 `X-CFD-Diagnostics-Level`을 따른다. basic/detailed 변경도 다음 응답에 반영된다. 임시 브라우저 검사에는 `CFDLog.configure(true, "detailed")`를 사용할 수 있지만 다음 서버 응답이 공통 설정을 다시 적용한다. Windows/macOS/Linux 접속 도구는 접속 전에 서버 설정을 읽을 수 없으므로 같은 이름의 **클라이언트 환경변수**로 켠다. 브라우저 로그 전송용 서버/API는 없다. OFF는 추가 진단 기록을 중단하며, 계산 판정에 쓰는 기존 solver 로그나 기존 운영 로그를 삭제하지 않는다.

## 수집 수준과 생략 범위

| 모드 | 수집 범위 |
|---|---|
| OFF | 추가 진단 기록 없음 |
| basic (ON 기본) | 사용자 조작, 요청/응답, 업무 경계의 caller/trace/입력·결과, 티켓·작업 변경, DB/subprocess/관측 프로세스, 경고·예외·기존 catch 위치 |
| detailed | basic + 계측된 전체 내부 함수의 정상 call/return, 정상 분기/반복, parser 각 줄, 브라우저 표시 helper |

basic에서는 문구 조회·CPU 변환·HTML escape·map/filter 등의 정상 호출에 대해 context/시각/인자·결과 요약을 만들지 않는다. Python 정상 분기 1,342곳은 인자 평가 전 상세 모드 guard로 건너뛰고, 165개 except 지점은 두 ON 모드 모두 기록한다. 동일 예외의 전파는 필요한 별도 관측이며 code/value/error 참조를 계속 사용한다. SQL statement와 실제 transaction, 다른 티켓·작업·클릭은 제거하거나 하나로 합치지 않는다.

basic helper 오류는 Python `function.error`에 실제 함수·stack을 남기고 상위 업무 call/trace에 연결한다. 브라우저 helper `function.raise`는 call_id/duration이 null이며 parent_call_id가 기록된 업무 경계다. 정상 call이 생략됐으므로 임의의 시작 시각이나 지속 시간을 만들지 않는다. ofps는 scan/동기화/명령 함수 경계와 관측 프로세스·ERR/EXIT를 유지하며 정상 helper call/return은 detailed 전용이다. helper의 nonzero return은 두 ON 모드에 보존하며 shell 조건식의 false를 곧바로 업무 실패라고 해석하지 않는다.

수집 범위는 [Python 정책](../cfd_bot/diagnostic_policy.py), [노드별 지도](../cfd_bot/diagnostic_map.json), [HLD/LLD 대응표](analysis/diagnostic-flow-coverage.md)에 명시한다. **basic에서 생략한 정상 내부 호출/분기는 사후 복원할 수 없다.** 모든 내부 호출까지 조사하려면 해당 사건이 일어나기 전에 detailed를 켜야 한다. 사용자 승인에 따라 정상 helper 기록 범위를 줄인 것이며 동일 사건 전체를 무손실 압축했다는 뜻이 아니다.

## 기록 범위와 해석

| 경계 | 주요 기록 |
|---|---|
| Python 함수 392개 (전체는 detailed) | function.call / return / raise, caller, input/result 요약, call/parent/trace, 소요 시간 |
| 기존 분기 1,507곳 (정상 분기는 detailed) | 원본 함수·위치 기반 step ID, 실제 선택한 분기/반복, 순서·시각, 반복 대상 요약, 처리된 예외 |
| Telegram | update ID, command/callback action, 비식별 actor, ACK thread 및 API request/response·오류 |
| Tk GUI | 함수·버튼 callback, 기존 입력/상태와 작업 thread의 부모 호출 연결 |
| web | HTTP method/path/status, 서버 trace 응답 헤더, 브라우저 클릭·필드·화면 이동·JS 함수/Promise 결과·예외 |
| 티켓·작업 | 파일 교체, 각 job insert/update/cancel, transaction 안의 기록과 commit 후 기록 구분 |
| SQLite | 연결 ID, SQL 지문·종류, 실행 대기/소요 시간, 변경 행 수, commit/rollback/오류; bind 값과 SQL 전문 제외 |
| ofps·worker·hooks | subprocess 요청/시작/PID/응답/종료 코드, 관측된 모든 프로세스의 CPU, solver 종료를 DB 갱신 전에 기록, 기존 종료 signal 요청 |
| Python·shell 경고/예외 | warning/logging.WARNING 이상, 처리된/미처리 예외와 thread 예외, 예외 chain/stack 위치, Bash ERR/EXIT·nonzero RETURN·PIPESTATUS (정상 helper RETURN은 detailed) |
| PC 접속 | 호스트 선택, SSH 시작/대기/종료 정리, 브라우저 열기, Windows launcher 예외 |

일반 문자열·대형 payload는 길이/개수와 제한된 요약을 남긴다. 함수 및 반복 입력의 dict는 티켓/작업/경로/상태/CPU/용량 판단 등의 식별·결과 필드와 개수로 요약한다. 티켓·작업 변경은 ID가 있는 별도 사건으로 추적한다. 정상 helper 반복 지점은 detailed에서만 남는다. 동일 scalar 인자의 정제 요약은 재사용하며 token 추가 시 관련 cache를 무효화한다. token·CSRF·인증 헤더·환경변수 내용·자유 입력 전문은 기록하지 않는다. Telegram actor는 SHA-256 앞 16자리로 연결한다. web에는 기존 로그인 계정이 없으므로 이 기록만으로 실제 사람의 신원을 단정할 수 없다.

외부 solver/Allrun/monitor의 내부 코드를 바꾸지는 않는다. 해당 경계는 실행 명령·PID·기존 stdout/stderr 파일·종료 코드·파싱된 오류/경고로 연결한다. Bash ERR는 shell 자체의 조건식/`||` 처리 규칙을 따르므로 모든 nonzero를 미처리 오류라고 표시하지 않는다. nonzero 함수 return과 ERR를 보존하며, 정상 내부 helper return은 detailed에서 기록한다. OpenSSH로 `exec`한 후의 내부 동작은 OpenSSH의 stderr/exit 경계에 남는다.

## 보관과 읽기

프로세스마다 0600 JSONL 파일을 사용한다. 기본 파일 회전은 Python/ofps 각각 20 MiB × 현재 파일+백업 3개다. ofps watch는 scan 경계에서 회전하므로 한 scan만큼 초과할 수 있다. 여러 프로세스·과거 세션 전체 디렉터리의 총 용량 제한은 아니다. 조사에 필요한 파일과 source map은 회전 전에 복사해 보관한다.

Python schema 2는 최대 128개 기록을 `log.batch.v2`에 묶는다. event/function 숫자 코드, batch context/call/value 사전, UTC 원점+시간 차이, 순서 차이를 사용한다. detailed 함수 분기는 최대 64개 `steps[]`에 묶는다. 선택한 수준에 포함된 사건은 샘플링하지 않는다. 동일 예외는 error ID와 공유 frame/추가 stack으로 표현하며 원래 traceback을 복원한다. 최초 오류는 flush하고 같은 예외의 반복 전파에서는 중복 flush를 생략한다. 최상위 호출 완료 및 계속 이벤트가 들어오는 동안 약 1초마다 flush한다. OFF는 요약·직렬화·파일 쓰기를 수행하지 않는다. fsync를 강제하지 않아 강제 종료·전원 장애 직전 버퍼는 유실될 수 있다. 기록 실패는 업무 예외를 대체하지 않으며 stderr와 출력 오류 계수로 드러낸다.

[실제 숫자 코드 사전](analysis/diagnostic-codebook.md)은 JSON/Markdown으로 함께 생성한다. `codes-<SHA256>.json`을 로그 디렉터리에 보관하고 각 파일 header에도 필요한 event/function 정의를 포함한다. hash는 사전 버전 식별에만 사용하며 매 payload를 해시하지 않는다. value/context/error 사전은 batch마다 독립적이므로 회전 후 각 파일을 읽을 수 있다. schema 1 파일도 같은 decoder로 계속 읽는다. SQL 읽기 context 종료는 `db.context.exit`, 실제 transaction 종료는 commit/rollback으로 구분한다. Telegram 수신은 `ui.telegram.received`, editor 전달은 `ui.telegram.routed`로 구분한다.

예외 객체의 traceback 참조는 요청 종료 때 해제하고, 긴 serve/CLI 호출에서도 256개를 넘겨 계속 보관하지 않는다. 참조 창에서 빠진 예외를 나중에 다시 관측하면 새 error ID와 완전한 정의를 남긴다. 이 제한은 예외 발생 기록을 생략하는 제한이 아니다.

`sources-<SHA256>.json`은 해당 버전의 함수·분기·시퀀스 지도와 소스 파일 hash다. 파일 회전 후에도 각 파일 header에 map hash를 기록한다. 이전 버전의 로그는 그때 보관한 map을 함께 사용한다.

```bash
python3 -m cfd_bot.diagnostics state/diagnostics/serve-*.jsonl \
  --map state/diagnostics/sources-<SHA256>.json > expanded.jsonl
python3 -m cfd_bot.diagnostics state/diagnostics/serve-*.jsonl \
  --trace '<trace_id>' --map state/diagnostics/sources-<SHA256>.json
```

확장 결과에서 `trace_id`, `parent_call_id`, 티켓/작업 ID로 연결한다. 묶음 저장 때문에 물리적인 줄 순서와 발생 순서는 다를 수 있다. 같은 process instance의 `seq` 및 `ts_ns`로 정렬한다. 프로세스 간 시각은 UTC이며, duration은 각 프로세스의 monotonic clock이다. clock 차이·PID 재사용을 고려해 trace와 instance도 함께 사용한다.

브라우저는 숫자 배열과 정제된 value 사전을 약 8 MiB의 추정 내용 예산에 맞춰 순환 보관한다. 객체 엔진 overhead를 포함한 실제 heap의 엄격한 8 MiB 제한은 아니다. 128개 event 단위 chunk를 회수하고 오래된 기록 수를 export header에 남긴다. 이벤트마다 JSON을 만들지 않으며 `CFDLog.download()`/`exportLines()` 호출 시 compact JSONL을 만든다. `snapshot()`은 기존 도구 호환용으로 펼친 JSON 문자열을 반환하므로 비용이 든다. 새로 고침·탭 종료 전 export해야 한다. UTC 기준 시각과 monotonic 시간 차이를 보관하며 session 안에서의 OS 시각 조정은 개별 Date.now 값으로 기록하지 않는다. server trace는 `http.response`에서 backend와 연결된다.

ofps도 event/function 코드와 파일 header를 사용하며 원래 stdout/exit/PIPESTATUS는 유지한다. browser/ofps export 역시 `python3 -m cfd_bot.diagnostics <file>`로 펼친다. Windows/macOS/Linux launcher의 기존 단순 로그 형식은 계속 읽을 수 있다. Windows는 `%LOCALAPPDATA%/CFD-Control-Room/logs`, macOS/Linux launcher는 `${TMPDIR:-/tmp}/cfd-client-logs` 또는 지정 디렉터리에 저장한다.

## 성능 판정

후속 [#30 원인·중복 분석](analysis/diagnostic-overhead-review.md)에서 제안한 숫자 기록·사전·공유 예외를 runtime에 적용했다. [구현 이력](history/2026-10-07-diagnostic-codec-implementation.md)과 [적용 전후 실측](analysis/diagnostic-codec-performance.md)을 참고한다. 제안 문서는 설계 당시 기록이며 실제 번호/형식은 [코드 사전](analysis/diagnostic-codebook.md)을 따른다.

basic/detailed를 분리한 최신 비교는 [수집 수준별 성능](analysis/diagnostic-level-performance.md)을 따른다. 과거 전체 ON 결과는 상세 모드의 배경 자료다. 실제 네트워크·solver·장기 동시 요청은 합성 fixture 측정과 구분하며 상세 모드는 여전히 큰 비용이 발생할 수 있다.
