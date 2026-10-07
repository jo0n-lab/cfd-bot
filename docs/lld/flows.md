# 전체 유즈케이스 × 플랫폼 — 함수 요청·응답 그림

각 링크는 **가로 participant·세로 lifeline·함수 요청/반환 화살표**로 그린 SVG다. 각 플랫폼 LLD에는 같은 그림과 함수 소스 링크를 함께 삽입했다. `ofps` 실행과 외부 Telegram API 경계도 그림 안에서 표시한다.

[Telegram 전체](telegram.md) · [GUI 전체](gui.md) · [Web 전체](web.md) · [Runtime 전체](runtime.md) · [공용 함수 상세](domain.md)

| UC | 사용자 동작 | Telegram | GUI | Web | 운영/접속 |
|---|---|---|---|---|---|
| UC-01 | 진입·도움말 | [UC-01-tg](../diagrams/UC-01-tg.svg) | [UC-01-gui](../diagrams/UC-01-gui.svg) | [UC-01-web](../diagrams/UC-01-web.svg) | N/A¹ |
| UC-02 | 실시간 현재 CASE | [UC-02-tg](../diagrams/UC-02-tg.svg) | N/A¹ | [UC-02-web](../diagrams/UC-02-web.svg) | N/A¹ |
| UC-03 | 등록 케이스 목록·선택 | [UC-03-tg](../diagrams/UC-03-tg.svg) | N/A¹ | [UC-03-web](../diagrams/UC-03-web.svg) | N/A¹ |
| UC-04 | 상태 상세 | [UC-04-tg](../diagrams/UC-04-tg.svg) | N/A¹ | [UC-04-web](../diagrams/UC-04-web.svg) | N/A¹ |
| UC-05 | Residual 조회 | [UC-05-tg](../diagrams/UC-05-tg.svg) | [UC-05-gui](../diagrams/UC-05-gui.svg) | [UC-05-web](../diagrams/UC-05-web.svg) / [UC-05-file-web](../diagrams/UC-05-file-web.svg) | N/A¹ |
| UC-06 | 결과 파일 조회 | [UC-06-tg](../diagrams/UC-06-tg.svg) | [UC-06-gui](../diagrams/UC-06-gui.svg) | [UC-06-web](../diagrams/UC-06-web.svg) / [UC-06-file-web](../diagrams/UC-06-file-web.svg) | N/A¹ |
| UC-07 | 티켓 목록·다중 선택 | [UC-07-tg](../diagrams/UC-07-tg.svg) | [UC-07-gui](../diagrams/UC-07-gui.svg) | [UC-07-web](../diagrams/UC-07-web.svg) | N/A¹ |
| UC-08 | 새 티켓 | [UC-08-tg](../diagrams/UC-08-tg.svg) | [UC-08-gui](../diagrams/UC-08-gui.svg) | [UC-08-web](../diagrams/UC-08-web.svg) | N/A¹ |
| UC-09 | 티켓 열기 | [UC-09-tg](../diagrams/UC-09-tg.svg) | [UC-09-gui](../diagrams/UC-09-gui.svg) | [UC-09-web](../diagrams/UC-09-web.svg) | N/A¹ |
| UC-10 | 기본·감시·알림·스크립트 편집 | [UC-10-tg](../diagrams/UC-10-tg.svg) / [UC-10-exports-tg](../diagrams/UC-10-exports-tg.svg) | [UC-10-gui](../diagrams/UC-10-gui.svg) / [UC-10-exports-gui](../diagrams/UC-10-exports-gui.svg) | [UC-10-web](../diagrams/UC-10-web.svg) / [UC-10-exports-web](../diagrams/UC-10-exports-web.svg) | N/A¹ |
| UC-11 | 티켓 검증 | [UC-11-tg](../diagrams/UC-11-tg.svg) | [UC-11-gui](../diagrams/UC-11-gui.svg) | [UC-11-web](../diagrams/UC-11-web.svg) | N/A¹ |
| UC-12 | 저장·이름 변경 | [UC-12-tg](../diagrams/UC-12-tg.svg) | [UC-12-gui](../diagrams/UC-12-gui.svg) | [UC-12-web](../diagrams/UC-12-web.svg) | N/A¹ |
| UC-13 | 티켓 복제 | [UC-13-tg](../diagrams/UC-13-tg.svg) | [UC-13-gui](../diagrams/UC-13-gui.svg) | [UC-13-web](../diagrams/UC-13-web.svg) | N/A¹ |
| UC-14 | 개별·다중 티켓 삭제 | [UC-14-tg](../diagrams/UC-14-tg.svg) | [UC-14-gui](../diagrams/UC-14-gui.svg) | [UC-14-web](../diagrams/UC-14-web.svg) | N/A¹ |
| UC-15 | 매크로 검색·필터·취소 | [UC-15-tg](../diagrams/UC-15-tg.svg) | [UC-15-gui](../diagrams/UC-15-gui.svg) | [UC-15-web](../diagrams/UC-15-web.svg) | N/A¹ |
| UC-16 | 매크로 구성원 선택 | [UC-16-tg](../diagrams/UC-16-tg.svg) | [UC-16-gui](../diagrams/UC-16-gui.svg) | [UC-16-web](../diagrams/UC-16-web.svg) | N/A¹ |
| UC-17 | 실행·queue 이름·동적 macro 설정 | [UC-17-tg](../diagrams/UC-17-tg.svg) | [UC-17-gui](../diagrams/UC-17-gui.svg) | [UC-17-web](../diagrams/UC-17-web.svg) | N/A¹ |
| UC-18 | 즉시 실행·이름 있는 대기열 등록 | [UC-18-tg](../diagrams/UC-18-tg.svg) / [UC-18-legacy-tg](../diagrams/UC-18-legacy-tg.svg) | [UC-18-gui](../diagrams/UC-18-gui.svg) | [UC-18-web](../diagrams/UC-18-web.svg) | N/A¹ |
| UC-19 | 큐·이력·매크로 진행 | [UC-19-tg](../diagrams/UC-19-tg.svg) | [UC-19-gui](../diagrams/UC-19-gui.svg) | [UC-19-web](../diagrams/UC-19-web.svg) | N/A¹ |
| UC-20 | 자동 시작 pause/resume | [UC-20-tg](../diagrams/UC-20-tg.svg) | N/A¹ | [UC-20-web](../diagrams/UC-20-web.svg) | N/A¹ |
| UC-21 | 대기 작업 선택·취소 | [UC-21-tg](../diagrams/UC-21-tg.svg) | [UC-21-gui](../diagrams/UC-21-gui.svg) | [UC-21-web](../diagrams/UC-21-web.svg) | N/A¹ |
| UC-22 | 실패 패턴 템플릿 | [UC-22-tg](../diagrams/UC-22-tg.svg) | [UC-22-gui](../diagrams/UC-22-gui.svg) | [UC-22-web](../diagrams/UC-22-web.svg) | N/A¹ |
| UC-23 | 폴더·로그·Residual 선택 | [UC-23-tg](../diagrams/UC-23-tg.svg) | [UC-23-gui](../diagrams/UC-23-gui.svg) | [UC-23-web](../diagrams/UC-23-web.svg) | N/A¹ |
| UC-24 | 입력 취소·초안 폐기·검색 취소 | [UC-24-tg](../diagrams/UC-24-tg.svg) | [UC-24-gui](../diagrams/UC-24-gui.svg) | [UC-24-web](../diagrams/UC-24-web.svg) | N/A¹ |
| UC-25 | 대화 일괄 정리 | [UC-25-tg](../diagrams/UC-25-tg.svg) | N/A¹ | N/A¹ | N/A¹ |
| UC-26 | 외부 PC 접속과 브라우저 | N/A¹ | N/A¹ | N/A¹ | [UC-26](../diagrams/UC-26.svg) |
| UC-27 | 운영 CLI 명령 분기 | N/A¹ | N/A¹ | N/A¹ | [UC-27](../diagrams/UC-27.svg) |
| UC-28 | 실행 중 managed 작업 중단 | [UC-28-tg](../diagrams/UC-28-tg.svg) | [UC-28-gui](../diagrams/UC-28-gui.svg) | [UC-28-web](../diagrams/UC-28-web.svg) | N/A¹ |

¹ N/A의 구체적 이유와 부분 지원 범위는 [ARCHITECTURE 플랫폼 표](../ARCHITECTURE.md#2-전체-사용자-유즈케이스--플랫폼)에 있다. GUI의 데이터 조회는 경로 표시이며 다운로드가 아니다. Telegram legacy 실행은 별도 그림으로 분리했다.

## 백그라운드와 공용 함수

| ID | 호출/응답 그림 |
|---|---|
| D-01 | [공용 티켓 색인 · 변경된 JSON 검증](../diagrams/D-01.svg) |
| D-02 | [저장·revision·원자적 파일 반영](../diagrams/D-02.svg) |
| D-03 | [공용 실행 요청](../diagrams/D-03.svg) |
| D-04 | [fresh snapshot과 lock 대기](../diagrams/D-04.svg) |
| D-05 | [삭제 preview와 최종 삭제](../diagrams/D-05.svg) |
| D-06 | [직계 하위 케이스 검색](../diagrams/D-06.svg) |
| D-07 | [로그·진행률·ETA](../diagrams/D-07.svg) |
| D-08 | [다중 대기 취소](../diagrams/D-08.svg) |
| D-09 | [매크로 발행·멤버 재구성](../diagrams/D-09.svg) |
| D-10 | [선언된 artifact 찾기](../diagrams/D-10.svg) |
| D-11 | [폼 변환과 티켓 검증](../diagrams/D-11.svg) |
| D-12 | [수치 기반 종료 판정](../diagrams/D-12.svg) |
| D-13 | [큐 전체 ETA 조회 · 대량 응답 생성](../diagrams/D-13.svg) |
| D-14 | [매크로 진행 ETA · 큐 본문과 중복 조회](../diagrams/D-14.svg) |
| BG-01 | [solver·monitor CPU를 합치는 scanner](../diagrams/BG-01.svg) |
| BG-02 | [현재 CASE · 이전 실행/종료 확인 중 CASE 감시](../diagrams/BG-02.svg) |
| BG-03 | [변경된 제출 티켓 접수](../diagrams/BG-03.svg) |
| D-15 | [증분 상태 반영 · 종료 child와 부모 macro 재시도](../diagrams/D-15.svg) |
| D-16 | [실행 중 managed 작업 안전 중단](../diagrams/D-16.svg) |
| BG-04 | [현재 head 기반 quota·동적 borrow admission](../diagrams/BG-04.svg) |
| BG-05 | [worker·solver·hooks·판정](../diagrams/BG-05.svg) |
| BG-06 | [outbox 알림 전달·checkpoint](../diagrams/BG-06.svg) |
| BG-07 | [worker 소실·재시작 복구](../diagrams/BG-07.svg) |

## 소스와 재생성

그림 원본은 [build_flows.py](../diagrams/build_flows.py)의 함수 호출/반환 명세다. 생성된 SVG를 직접 수정하지 않고 명세를 고친다. 외부 폰트/CDN/이미지/JavaScript에 의존하지 않는다.

```bash
python3 docs/diagrams/build_flows.py
python3 docs/analysis/build_reference.py
```

현재 103개 SVG. [코드 함수 색인](../analysis/function-index.md), [검증 결과](../analysis/validation.md).
