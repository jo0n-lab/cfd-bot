# 작업 큐 실행 작업 전체 선택·일괄 중단

- GitHub Issue: 사용자 지시에 따라 등록하지 않음.
- 검증: 사용자 지시에 따라 변경 경로의 표적 테스트와 구문 검사만 수행.

## As-Is HLD / LLD

```mermaid
flowchart LR
    UI[Telegram / GUI / web] --> One[실행 작업 하나 확인]
    One --> Stop[interrupt_running_job]
```

대기 작업만 전체 선택할 수 있고, 실행 중인 작업은 개별 중단만 가능하다.

## To-Be HLD / LLD

```mermaid
flowchart LR
    UI[Telegram / GUI / web] --> Select[실행 작업 전체 선택 / 해제]
    Select --> Confirm[선택 ID 목록 확인]
    Confirm --> Batch[interrupt_running_jobs]
    Batch --> Existing[기존 interrupt_running_job 반복]
    Existing --> Result[중단 요청 수 / 이미 종료·변경 수]
```

세 UI가 선택한 ID를 공용 batch helper로 전달한다. helper는 ID 중복을 제거하고 기존
중단 함수의 PID 검증·stopping·CPU 예약 정책을 그대로 사용한다. 대기 작업 취소와
자동 시작 pause는 기존 동작을 유지한다. 선택 후 새로 시작된 작업은 자동 포함하지 않는다.
Telegram 문구/manifest, 현행 HLD/LLD/architecture와 UC-28 그림을 함께 갱신한다.

## 구현·확인

- 세 UI에 실행 작업 전체 선택·해제·일괄 중단을 연결했다. Telegram은 대기 취소의 기존
  페이지/선택 UI를 공유하고, 확인한 ID 목록을 고정해 이후 새 작업을 포함하지 않는다.
- 표적 테스트 12개 통과: Telegram 전체 선택·확인 이후 변경, GUI 다중 선택,
  web 단일/일괄 중단 API, 기존 대기 취소와 UI resource 확인.
- compileall·JavaScript 구문·diff 공백 검사 완료. 사용자 요청에 따라 전체 테스트와
  실제 브라우저/GUI 화면 테스트는 추가 실행하지 않았다.
