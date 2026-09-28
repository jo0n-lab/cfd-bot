# AGENTS.md

이 규칙은 저장소 전체에 적용한다.

## 개발 절차

1. 로직 수정이나 기능 추가가 필요하면 구현 전에 현재 HLD/LLD의 **As-Is**와 변경할 **To-Be**를 그린다.
   - `docs/history/YYYY-MM-DD-topic.md`에 배경, As-Is/To-Be HLD·LLD, 호환성, 검증 계획을 기록한다.
   - 같은 내용을 GitHub Issue로 등록하고 Issue 번호를 history 문서와 index에 연결한다.
   - 그 뒤 구현하고 현행 `docs/HLD.md`, `docs/LLD.md`, `docs/ARCHITECTURE.md`와 관련 그림을 최종 구조에 맞춘다.

2. 인터페이스나 사용자 기능을 변경하면 **Telegram · `cfd-ticket-gui` · web 세 인터페이스 모두에 같은 작업에서 적용한다.** 한 인터페이스에만 반영하고 완료하지 않는다.
   - 티켓 생성·편집·검증·저장·복제·삭제·실행 설정과 실행 제어는 같은 `cfd_bot.editor.TicketService` 및 domain helper를 사용한다.
   - 각 인터페이스에는 화면·버튼·callback·대화 session·HTTP 같은 UI adapter만 둔다. 티켓 규칙과 실행 정책을 UI별로 중복 구현하지 않는다.
   - 세 인터페이스의 표시·입력·저장·실행 연결을 함께 검증하고, 환경상 검증하지 못한 부분은 명시한다.

3. Telegram의 고정 문구와 버튼 라벨은 `telegram-ui/`의 메뉴·시나리오 JSON에서 관리한다. key를 바꾸면 `manifest.json`과 UI resource 테스트를 함께 갱신한다.

4. 동작 변경은 관련 README, example, 테스트를 같은 작업에서 갱신한다. 테스트 후 실제 봇 동작이 바뀌면 user service를 재시작하고 상태를 확인한다.

## 핵심 불변식

- `/stat`은 watcher DB가 아니라 매 요청마다 같은 `ofps` snapshot 경로에서 활성 CASE 전체를 읽는다.
- 외부에서 시작한 계산도 티켓 유무나 SSH·tmux·systemd 실행 방식과 관계없이 감시한다.
- 정상 종료는 `controlDict.stopAt=endTime`이고 최신 로그의 최종 `Time >= endTime`일 때만 인정한다.
- 알림은 SQLite outbox에 먼저 기록하며 종료 알림에는 요약과 Residual 하나만 자동 첨부한다.
- 티켓은 `tickets/*.json`에서 관리하고 case 밖으로 벗어나는 경로와 symlink를 허용하지 않는다.
- bot token과 인증 정보는 코드, 티켓, 문서, 테스트 fixture, 로그에 넣지 않는다.

## 검증

```bash
python3 -m compileall -q cfd_bot tests
python3 -m unittest discover -s tests -q
python3 -m cfd_bot --config bot.json check
```

SVG 변경 시 XML parse와 실제 렌더링도 확인한다. 테스트는 실제 Telegram 전송이나 OpenFOAM 계산을 시작하지 않아야 한다.
