# 사후 원인 분석용 진단 로그 (#18)

이미 일어난 문제에서 사용자 조작 → 함수 호출/반환 → 기존 판단 → 티켓/작업 변경 → 응답/예외를 연결한다. 로그 설정과 기록만 추가하며 업무 검증·실행 차단·재시도·복구 정책은 추가하지 않는다. [사전 설계](history/2026-10-07-diagnostic-sequence-logging.md), [99개 시퀀스 대응표](analysis/diagnostic-flow-coverage.md), [실측 결과](analysis/diagnostic-performance.md)를 함께 참고한다.

## ON/OFF

기존 설정에는 진단 로그가 **기본 OFF**다. 대량 데이터에서 모든 함수/분기를 기록하는 ON의 비용이 확인되어 기존 운영에 자동 활성화하지 않는다. OFF 동안 발생한 문제의 상세 진단 기록은 나중에 복원할 수 없다. 사후 분석이 필요한 운영 구간에는 사전에 ON을 설정해야 한다.

`bot.json`의 공통 설정:

```json
"diagnostic_logging": {
  "enabled": true,
  "max_bytes": 20971520,
  "backups": 3
}
```

`CFD_BOT_DIAGNOSTICS=1` 또는 `0`은 설정 파일보다 우선한다. `CFD_BOT_DIAGNOSTICS_DIR`로 기록 디렉터리를 지정할 수 있다. 기본은 `<state_dir>/diagnostics`; 설정의 `directory`가 있으면 해당 경로가 우선한다. 상대 경로는 bot 설정 파일 기준이다. 설정은 시작/설정 로드 시 적용하며 서비스 재기동 후 브라우저도 새로 고침한다. 워커·ofps 등 자식 프로세스에는 ON/OFF·디렉터리·trace/parent ID를 전달한다. 이미 실행 중인 독립 워커에는 재기동한 부모의 새 설정이 소급 적용되지 않는다.

```bash
CFD_BOT_DIAGNOSTICS=1 python3 -m cfd_bot --config bot.json web
CFD_BOT_DIAGNOSTICS=0 python3 -m cfd_bot --config bot.json check
```

Telegram, GUI, web backend는 같은 Python 모듈과 설정을 사용한다. 브라우저는 응답의 `X-CFD-Diagnostics`를 따른다. Windows/macOS/Linux 접속 도구는 접속 전에 서버 설정을 읽을 수 없으므로 같은 이름의 **클라이언트 환경변수**로 켠다. 브라우저 로그 전송용 서버/API는 없다. OFF는 추가 진단 기록을 중단하며, 계산 판정에 쓰는 기존 solver 로그나 기존 운영 로그를 삭제하지 않는다.

## 기록 범위와 해석

| 경계 | 주요 기록 |
|---|---|
| Python 함수 392개 | function.call / return / raise, caller, input/result 요약, call/parent/trace, 소요 시간 |
| 기존 분기 1,507곳 | 원본 함수·위치 기반 step ID, 실제 선택한 분기/반복, 순서·시각, 반복 대상 요약, 처리된 예외 |
| Telegram | update ID, command/callback action, 비식별 actor, ACK thread 및 API request/response·오류 |
| Tk GUI | 함수·버튼 callback, 기존 입력/상태와 작업 thread의 부모 호출 연결 |
| web | HTTP method/path/status, 서버 trace 응답 헤더, 브라우저 클릭·필드·화면 이동·JS 함수/Promise 결과·예외 |
| 티켓·작업 | 파일 교체, 각 job insert/update/cancel, transaction 안의 기록과 commit 후 기록 구분 |
| SQLite | 연결 ID, SQL 지문·종류, 실행 대기/소요 시간, 변경 행 수, commit/rollback/오류; bind 값과 SQL 전문 제외 |
| ofps·worker·hooks | subprocess 요청/시작/PID/응답/종료 코드, 관측된 모든 프로세스의 CPU, solver 종료를 DB 갱신 전에 기록, 기존 종료 signal 요청 |
| Python·shell 경고/예외 | warning/logging.WARNING 이상, 처리된/미처리 예외와 thread 예외, 예외 chain/stack 위치, Bash ERR/RETURN/EXIT와 PIPESTATUS |
| PC 접속 | 호스트 선택, SSH 시작/대기/종료 정리, 브라우저 열기, Windows launcher 예외 |

일반 문자열·대형 payload는 길이/개수와 제한된 요약을 남긴다. 식별에 필요한 티켓/작업/경로/상태/CPU 등의 필드는 보존하며, 한 batch의 각 티켓·작업은 별도 이벤트/반복 지점으로 추적한다. token·CSRF·인증 헤더·환경변수 내용·자유 입력 전문은 기록하지 않는다. Telegram actor는 SHA-256 앞 16자리로 연결한다. web에는 기존 로그인 계정이 없으므로 이 기록만으로 실제 사람의 신원을 단정할 수 없다.

외부 solver/Allrun/monitor의 내부 코드를 바꾸지는 않는다. 해당 경계는 실행 명령·PID·기존 stdout/stderr 파일·종료 코드·파싱된 오류/경고로 연결한다. Bash ERR는 shell 자체의 조건식/`||` 처리 규칙을 따르므로 모든 nonzero를 미처리 오류라고 표시하지 않는다. 명시적 함수 return과 기존 오류 분기는 별도로 기록한다. OpenSSH로 `exec`한 후의 내부 동작은 OpenSSH의 stderr/exit 경계에 남는다.

## 보관과 읽기

프로세스마다 0600 JSONL 파일을 사용한다. 기본 파일 회전은 Python/ofps 각각 20 MiB × 현재 파일+백업 3개다. ofps watch는 scan 경계에서 회전하므로 한 scan만큼 초과할 수 있다. 여러 프로세스·과거 세션 전체 디렉터리의 총 용량 제한은 아니다. 조사에 필요한 파일과 source map은 회전 전에 복사해 보관한다.

Python은 최대 128개 기록과 공통 문맥을 `log.batch` 한 줄에 묶고, 함수의 분기는 최대 64개 `steps[]`에 묶는다. 각 기록의 호출 ID·시각·순서·값은 보존하며 이벤트 샘플링은 하지 않는다. OFF에서는 이 요약·직렬화·파일 쓰기를 수행하지 않는다. 오류와 최상위 호출 완료는 flush하며, 계속 이벤트가 들어오는 동안 약 1초마다 flush한다. fsync를 강제하지 않아 강제 종료·전원 장애 직전의 버퍼는 유실될 수 있다. 기록 실패는 업무 처리의 예외를 대체하지 않으며 stderr와 로그 출력 오류 계수로 드러낸다.

`sources-<SHA256>.json`은 해당 버전의 함수·분기·시퀀스 지도와 소스 파일 hash다. 파일 회전 후에도 각 파일 header에 map hash를 기록한다. 이전 버전의 로그는 그때 보관한 map을 함께 사용한다.

```bash
python3 -m cfd_bot.diagnostics state/diagnostics/serve-*.jsonl \
  --map state/diagnostics/sources-<SHA256>.json > expanded.jsonl
python3 -m cfd_bot.diagnostics state/diagnostics/serve-*.jsonl \
  --trace '<trace_id>' --map state/diagnostics/sources-<SHA256>.json
```

확장 결과에서 `trace_id`, `parent_call_id`, 티켓/작업 ID로 연결한다. 묶음 저장 때문에 물리적인 줄 순서와 발생 순서는 다를 수 있다. 같은 process instance의 `seq` 및 `ts_ns`로 정렬한다. 프로세스 간 시각은 UTC이며, duration은 각 프로세스의 monotonic clock이다. clock 차이·PID 재사용을 고려해 trace와 instance도 함께 사용한다.

브라우저는 약 8 MiB의 로컬 순환 버퍼를 사용한다. 개발자 도구에서 `CFDLog.download()`를 실행해 JSONL을 저장한다. 내보낸 첫 줄에 오래되어 덮어쓴 기록 수가 포함된다. 새로 고침·탭 종료 전에 내보내야 한다. 서버 trace는 `http.response`에 포함되어 backend 로그와 연결된다. Windows는 `%LOCALAPPDATA%/CFD-Control-Room/logs`, macOS/Linux launcher는 `${TMPDIR:-/tmp}/cfd-client-logs` 또는 지정 디렉터리에 저장한다.

## 성능 판정

후속 [#30 원인·중복 분석](analysis/diagnostic-overhead-review.md)과 [숫자 signal 코드 사전 제안](analysis/diagnostic-codebook-proposal.md)을 별도로 제공한다. 제안 형식은 아직 runtime에 적용하지 않았다.

전체 호출 기록 ON은 무료가 아니다. 921개 티켓과 40개 활성 CASE, 긴 계산 로그, 1,000행 브라우저 렌더링을 포함해 baseline/OFF/ON을 비교한다. 총 기록량·보관량·CPU·RSS도 결과에 포함한다. 시나리오 누락이나 샘플링으로 지연을 낮추지 않는다. **대량 ON의 지연이 확인되어 상시 ON 운영의 성능 검증 완료로 판단하지 않는다.** 수치는 [측정 보고서](analysis/diagnostic-performance.md)에 공개한다.
