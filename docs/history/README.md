# 설계 변경 이력

로직 수정이나 기능 추가 전에는 `YYYY-MM-DD-topic.md`에 As-Is/To-Be HLD와 LLD를 기록한다. 같은 내용을 GitHub Issue에 등록하고 이 문서에 Issue 번호나 등록 상태를 남긴 뒤 구현한다.

| 날짜 | 변경 | GitHub Issue |
|---|---|---|
| 2026-10-07 | [숫자 코드·사전 기반 진단 로그 구현](2026-10-07-diagnostic-codec-implementation.md) | [#30](https://github.com/jo0n-lab/cfd-bot/issues/30) |
| 2026-10-07 | [진단 로그 지연·중복·숫자 코드/해시 사전 검토](2026-10-07-diagnostic-overhead-review.md) | [#30](https://github.com/jo0n-lab/cfd-bot/issues/30) |
| 2026-10-07 | [전체 시퀀스 진단 로그·ON/OFF·성능 비교](2026-10-07-diagnostic-sequence-logging.md) | [#18](https://github.com/jo0n-lab/cfd-bot/issues/18) |
| 2026-10-07 | [종료 이벤트 재처리로 인한 SQLite lock 제거](2026-10-07-terminal-event-db-lock.md) | [#26](https://github.com/jo0n-lab/cfd-bot/issues/26) |
| 2026-10-07 | [`ofps`의 전용 모니터링 CPU 관측](2026-10-07-ofps-monitor-cpu-observation.md) | [#25](https://github.com/jo0n-lab/cfd-bot/issues/25) |
| 2026-10-07 | [동적 매크로의 현재 head 기준 실행 판정](2026-10-07-dynamic-macro-head-admission.md) | [#24](https://github.com/jo0n-lab/cfd-bot/issues/24) 재오픈 |
| 2026-10-07 | [케이스 코어 수에서 대기열 quota 파생](2026-10-07-derived-queue-quota.md) | [#24](https://github.com/jo0n-lab/cfd-bot/issues/24) |
| 2026-10-07 | [매크로 파일 선택기의 부모 경로 fallback 차단](2026-10-07-macro-export-relative-path.md) | [#23](https://github.com/jo0n-lab/cfd-bot/issues/23) |
| 2026-10-06 | [이름 있는 독립 대기열과 동적 코어 매크로](2026-10-06-capacity-aware-parallel-execution.md) | [#21](https://github.com/jo0n-lab/cfd-bot/issues/21), [#22](https://github.com/jo0n-lab/cfd-bot/issues/22) |
| 2026-10-04 | [티켓 색인·증분 감시로 전역 재검증 제거](2026-10-04-ticket-index-incremental-monitor.md) | [#20](https://github.com/jo0n-lab/cfd-bot/issues/20) |
| 2026-10-04 | [함수 요청·응답과 유즈케이스·플랫폼별 아키텍처 전면 재작성](2026-10-04-architecture-function-flows.md) | [#19](https://github.com/jo0n-lab/cfd-bot/issues/19) |
| 2026-10-04 | [매크로 하위 케이스 이름 포함·제외 필터](2026-10-04-macro-case-name-filters.md) | [#17](https://github.com/jo0n-lab/cfd-bot/issues/17) |
| 2026-10-04 | [모니터링 전용 CPU의 명시적 활성화와 입력란 표시](2026-10-04-monitoring-opt-in-visibility.md) | [#16](https://github.com/jo0n-lab/cfd-bot/issues/16) |
| 2026-10-04 | [케이스별 `.process-core` 자동 생성과 동기화](2026-10-04-process-core-generation.md) | [#15](https://github.com/jo0n-lab/cfd-bot/issues/15) |
| 2026-10-02 | [case 실행 설정의 NP 보존](2026-10-02-case-np-preservation.md) | [#14](https://github.com/jo0n-lab/cfd-bot/issues/14) |
| 2026-10-02 | [모니터링 전용 CPU와 스크립트 실행](2026-10-02-monitoring-cpu-allocation.md) | [#13](https://github.com/jo0n-lab/cfd-bot/issues/13) |
| 2026-10-01 | [유효 CFD case가 없는 scanner 오탐 제거](2026-10-01-ofps-invalid-case-filter.md) | [#12](https://github.com/jo0n-lab/cfd-bot/issues/12) |
| 2026-09-29 | [Telegram 응답 지연 제거](2026-09-29-telegram-response-latency.md) | [#10](https://github.com/jo0n-lab/cfd-bot/issues/10) |
| 2026-09-29 | [결과 데이터 추적 링크와 실행 중인 매크로 진행 현황](2026-09-29-trackable-results-and-live-macros.md) | [#9](https://github.com/jo0n-lab/cfd-bot/issues/9) |
| 2026-09-28 | [작업 큐 다중 선택 취소와 전체 선택·해제](2026-09-28-bulk-queue-cancellation.md) | [#8](https://github.com/jo0n-lab/cfd-bot/issues/8) |
| 2026-09-28 | [매크로 직계 하위 케이스 검색](2026-09-28-macro-direct-child-discovery.md) | [#7](https://github.com/jo0n-lab/cfd-bot/issues/7) |
| 2026-09-28 | [웹 진행 현황 ETA·진행바 깜박임 제거](2026-09-28-web-progress-flicker.md) | [#6](https://github.com/jo0n-lab/cfd-bot/issues/6) |
| 2026-09-28 | [웹 UI 블루 테마](2026-09-28-web-blue-theme.md) | [#5](https://github.com/jo0n-lab/cfd-bot/issues/5) |
| 2026-09-28 | [웹 티켓 선택 지연 제거](2026-09-28-web-ticket-selection-latency.md) | [#4](https://github.com/jo0n-lab/cfd-bot/issues/4) |
| 2026-09-28 | [개별 티켓 실행 설정 · 세 UI 동등 적용](2026-09-28-single-ticket-execution.md) | [#3](https://github.com/jo0n-lab/cfd-bot/issues/3) |
| 2026-09-28 | [SSH config Host 선택](2026-09-28-ssh-config-host-selection.md) | [#2](https://github.com/jo0n-lab/cfd-bot/issues/2) |
| 2026-09-28 | [SSH 실행기 단순화](2026-09-28-simple-ssh-launcher.md) | [#2](https://github.com/jo0n-lab/cfd-bot/issues/2) |
| 2026-09-28 | [localhost 웹 UI](2026-09-28-localhost-web.md) | [#2](https://github.com/jo0n-lab/cfd-bot/issues/2) |
| 2026-09-28 | [`ofps` 단일 실행 파일 통합](2026-09-28-ofps-single-entry.md) | [#1](https://github.com/jo0n-lab/cfd-bot/issues/1) |
