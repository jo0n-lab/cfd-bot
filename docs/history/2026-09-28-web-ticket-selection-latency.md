# 웹 티켓 선택 지연 제거

GitHub Issue: [#4](https://github.com/jo0n-lab/cfd-bot/issues/4)

## 배경

웹 티켓 관리에서 티켓을 선택할 때마다 `/api/ticket`이 `TicketRunner.state(..., fresh=True)`를 호출해 전체 `ofps` 스캔을 동기 실행한다. 직접 측정에서 티켓 열기 5.16초, `ofps` 5.14~5.56초였다. Telegram 카드와 `cfd-ticket-gui`는 편집 화면을 열 때 저장된 snapshot을 사용하고 실제 실행·명시적 상태 새로고침에서만 fresh 검사를 한다.

## As-Is HLD / LLD

```mermaid
flowchart LR
  C[웹 티켓 클릭] --> T[/api/ticket]
  T --> O[ofps 전체 스캔 5~10초]
  O --> E[편집 화면]
```

## To-Be HLD / LLD

```mermaid
flowchart LR
  M[Bot Monitor] --> S[(최근 snapshot)]
  C[웹 티켓 클릭] --> T[/api/ticket]
  T --> S --> E[즉시 편집 화면]
  E --> R[실행 요청]
  R --> F[fresh ofps 검사]
  F --> Q[중복 실행 차단 / 큐 등록]
```

## 호환성·안전

편집 화면의 실행 상태는 Bot Monitor 및 웹의 주기적 overview가 저장한 최근 snapshot을 사용한다. 실제 `/api/run`은 기존처럼 fresh `ofps`를 실행하므로 중복 실행 방지는 약화되지 않는다. Telegram과 GUI의 기존 편집 동작과 같은 정책이다.

## 검증 계획

`/api/ticket`이 `ofps`를 호출하지 않는 HTTP 테스트, 실제 localhost 응답시간 측정, Chromium 티켓 선택 흐름, 전체 테스트, 설정 check를 수행한다. 웹 서비스 재시작 후 읽기 전용 smoke test로 확인한다.

## 구현 및 검증 결과

- 웹 `/api/ticket`에서 `fresh=True`를 제거해 Monitor/overview가 저장한 최근 snapshot으로 편집 카드 상태를 계산한다.
- Telegram 카드와 GUI 편집 화면의 기존 cached-state 정책과 일치시켰다. 실제 `/api/run`의 fresh `ofps` 검사와 중복 실행 차단은 변경하지 않았다.
- 동일 티켓의 실제 응답시간이 변경 전 5.16초에서 변경 후 3.7~5.4ms로 감소했다. 느린 overview가 동시에 실행 중일 때도 티켓 응답은 6ms였다.
- 전체 unittest 238개 통과(디스플레이 없는 native Tk 1개 skip), Chromium fixture 및 실제 localhost 읽기 전용 smoke test 통과, compileall·설정 check·diff check·SVG XML parse/render 통과.
- `cfd-bot-web.service` 재시작 후 active/running 확인. 봇 서비스와 계산 worker는 재시작하지 않았다.
