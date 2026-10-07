# 진단 signal 코드·사전 형식 제안 (#30)

**검토안이며 현재 런타임 형식이 아니다.** [분석 보고서](diagnostic-overhead-review.md), [이슈 #30](https://github.com/jo0n-lab/cfd-bot/issues/30), [현재 형식](../DIAGNOSTICS.md)을 구분해서 읽는다. 여기서 signal은 404 같은 숫자 사건 코드이며 POSIX signal 전송이나 오류 처리 정책을 뜻하지 않는다.

## 판단

반복되는 설명·함수명·필드명·caller·경로를 숫자 ID로 바꾸고 의미를 별도 사전에 보관하는 방식은 유효하다. 해시 자체가 압축을 하는 것은 아니다. 긴 SHA-256 문자열을 매 event에 기록하면 짧은 원문보다 커지고, 원문 전체를 매번 정규화·해시하면 CPU도 증가한다. **정적인 정보는 빌드 시 숫자 ID, 사전 전체의 버전 식별은 SHA-256, 동적인 값은 제한된 값 사전의 짧은 ID**를 우선한다.

현재 Python도 분기 ID와 source-map hash, batch 공통 context를 사용한다. 제안은 이를 함수·caller·event·필드와 browser까지 확장한다. 현재 로그를 먼저 크게 만든 뒤 해시로 재변환하는 단계는 요청 경로에 추가하지 않는다.

## 별도 문서/기계 사전에 포함할 내용

`diagnostic-codebook.<sha256>.json`과 사람이 읽는 `diagnostic-codebook.<sha256>.md`를 같은 정의에서 생성한다. source revision, schema version, namespace, event/함수/호출 위치 ID, 필드 순서·타입·단위, null/누락의 의미, 민감정보 제외 규칙, HLD/LLD flow와 소스 위치, 정상/예외 예시를 담는다.

ID는 해당 사전 버전에서만 해석한다. 버전이 달라진 ID를 같은 사건이라고 추정하지 않는다. 파일 header는 사전 hash와 process instance를 갖고, 사전과 로그를 함께 export한다. 회전 파일은 단독으로 필요한 정의를 찾을 수 있어야 한다. 사전이 없으면 decoder는 미해석 ID로 표시하고 설명을 지어내지 않는다. 이는 오프라인 해석기의 동작이며 업무 검증 로직을 추가하는 것이 아니다.

hash는 원문 복원 수단이 아니므로 대응 내용을 반드시 보관한다. 동적 사전은 제한된 수명과 용량을 두고 reset/checkpoint를 기록한다. 같은 hash라고 다른 값을 합치지 않도록 사전 생성 시 내용을 비교한다. 비밀값 제거는 사전에 넣기 전에 수행하며 사용자 원문을 hash 사전에 숨겨 보관하지 않는다.

## 예시 signal 코드

아래 번호는 설명용 제안이며 예약·배포된 값이 아니다. 실제 함수/분기는 생성된 사전의 ID로 관리한다.

| namespace / code | 의미 | 값 필드 | 관련 경계 |
|---|---|---|---|
| core / 100 | 함수 호출 | function_id, call_id, parent_id, caller_site_id, input_ref | 모든 실제 호출 |
| core / 101 | 정상 반환 | call_id, duration, result_ref | 기존 반환값 |
| core / 102 | 예외 전파 | call_id, error_id, 추가 stack 구간 | 기존 예외 재전파 |
| core / 103 | 실제 분기·반복 | call_id, step_id, ordinal, values_ref | HLD/LLD 내부 경로 |
| ui / 1101 | 버튼·callback 선택 | interaction_id, actor_ref, action_id, target_ref | Telegram/GUI/web |
| ticket / 2101 | 티켓 파일 교체 완료 | ticket_ref, revision, path_ref | atomic_json 완료 |
| job / 3101 | 작업 등록 관측 | job_ref, ticket_ref, transaction_ref | DB insert 시점 |
| db / 3102 | transaction commit 완료 | transaction_ref, duration | 실제 commit |
| io / 404 | 대상 파일 없음 | path_ref, operation_id, original_error_ref | 기존 파일 오류 |
| python / 500 | 예외 최초 관측 | error_id, type_id, message/args, frames | 원래 예외 |
| shell / 501 | 기존 명령의 nonzero | call_id, source_id, line, exit_code, PIPESTATUS | 기존 ERR/RETURN/EXIT |

`io/404`와 HTTP status 404는 별개 필드다. 숫자만 보고 원인을 동일시하지 않는다. 메시지가 같아도 ticket/job/요청이 다르면 독립된 사건이다. 새로운 오류나 차단 조건을 만들어 코드를 발생시키지 않는다.

## 기록 예시와 복원

header: `schema=2, codebook=<sha256>, instance=<process>, time_origin_ns=<UTC>, context_table=...`.

고정 배열 예시: `[delta_seq, delta_time_ns, context_id, namespace_id, signal_code, call_no, parent_no, values]`.

```text
[1, 0,    0, 0, 100, 42, 7, [731, 91, 12]]
[1, 2400, 0, 2, 2101,42, 7, [81, "revision-A", 29]]
[1, 4200, 0, 0, 101, 42, 7, [6600, 13]]
```

사전에서 함수 731, caller 위치 91, 티켓 81, 경로 29와 값 참조 12/13을 풀어 기존 사건을 재구성한다. 값 사전은 정적 코드 사전과 별도다. context에는 actor·trace·thread를 두며 process ID는 header에 둔다. null과 0, bool과 int, list와 object를 구별하고 timestamp/duration 단위를 명시한다. 반복을 묶더라도 각 호출의 ordinal·순서·부모·결과와 식별 값은 복원할 수 있어야 한다.

JSON으로 내보낼 때 UTC nanosecond 원점은 문자열 등 정밀도가 유지되는 형식을 사용하고, browser의 Number 정수 범위를 넘는 값은 숫자로 강제 변환하지 않는다. delta의 기준은 batch header에 명시하며 process 간 순서는 trace/parent와 함께 해석한다.

실험용 `audit_diagnostics.py`의 tagged JSON codec은 크기 가능성을 확인하는 프로토타입이다. 위 고정 배열 런타임 구현이나 완성된 decoder가 아니다. 실험은 정제된 현재 로그를 byte 값/필드 단위로 동등하게 복원했으며, 이미 제거된 비밀값·생략된 원문까지 복원했다는 의미는 아니다.

## 중복 예외와 반복 처리

같은 예외 객체가 여러 함수로 올라오면 최초 오류의 type/message/원인 chain과 stack을 한 번 정의한다. 각 전파에는 error_id·call_id·추가된 frame 구간만 남긴다. 최종 catch는 처리 지점과 기존 결과를 기록한다. 서로 다른 발생의 같은 문구는 occurrence ID를 따로 유지한다. 객체 ID 재사용과 메모리 누적을 피하도록 참조 수명은 trace/batch에 제한한다. 이 수명 관리는 로그 내부에만 적용한다.

순수 `esc`, `badge`, 텍스트 lookup, 파서의 줄별 `None` 반환은 설명과 동일 요약을 매번 만들 필요가 낮다. 다만 전체 함수/시퀀스 추적 요구 때문에 단순 삭제하거나 count만 남기지 않는다. 미리 정한 schema로 필요한 작은 값만 기록하고, 반복되는 경로는 template+횟수+각 occurrence의 값/시간 배열로 표현할 수 있는지 검증한다. 서로 다른 동적 값을 하나의 hash로 대체하고 사전에 원본을 저장하지 않는 방식은 원인 분석 정보를 잃는다.

## 적용 우선순위와 검증

1. 정적 event/function/caller/field code를 빌드 시 결정하고 문서·JSON 사전을 함께 생성한다. browser도 session과 함수명을 반복하지 않고 batch context에 둔다.
2. 동일 예외의 전체 stack 재생성을 error_id+추가 frame으로 전환한다. call/return·catch·오류 경계는 유지한다.
3. 타입 범용 요약과 문자열 정규식 대신 사건별로 필요한 필드를 정해 수집한다. 티켓/작업마다 식별자·결과·순서를 유지한다.
4. 숫자 배열 buffer를 생성 시점부터 사용하고 serialize는 batch/export 시점에 한다. 비동기 writer만 붙이면 Python 요약·GIL 비용이 사라지지 않으므로 따로 측정한다.
5. gzip 같은 추가 압축은 우선 회전 완료 파일/export에서 검토한다. 원자료·사전을 함께 압축한 실제 크기와 CPU를 측정한다.

완료 판정에는 모든 기존 정상/예외·취소·다중 thread·process 경로의 복원, HLD/LLD 99개 매핑, 사전 없는 파일/회전/강제 종료의 해석, 비밀값 제외, OFF zero-write를 포함한다. 성능은 CPU 설정 유무, 정상/예외 비율, 40/921 티켓, 2,000줄 parser, 1,000행 browser, 동시 요청을 같은 최종 revision에서 비교한다. **이번 압축 실험의 용량 절감률을 그대로 실행 시간 개선률로 주장하지 않는다.**
