# HLD/LLD 로그 대응표 (#18 / #30)

[기록 계약](../DIAGNOSTICS.md) · [기계 지도와 노드별 수준](../../cfd_bot/diagnostic_map.json)

기본(basic)은 업무 경계와 명시적 사건·경고·예외를 기록한다. 내부 helper의 정상 호출/분기는 상세(detailed) 전용이며 기본 기록에서 복원할 수 없다. UI callback은 실제 사용자 Event일 때 기본 기록한다. 아래는 정적 대응이며 전체 시나리오의 실제 실행 검증을 뜻하지 않는다.

| 시퀀스 | 목적 | 노드 | basic Python 업무 함수 | 상세 전용 Python 정상 호출 | UI/외부 경계 |
|---|---|---:|---:|---:|---:|
| [D-01](../diagrams/D-01.svg) | 공용 티켓 색인 · 변경된 JSON 검증 | 8 | 2 | 6 | 0 |
| [D-02](../diagrams/D-02.svg) | 저장·revision·원자적 파일 반영 | 6 | 5 | 1 | 0 |
| [D-03](../diagrams/D-03.svg) | 공용 실행 요청 | 20 | 5 | 12 | 3 |
| [D-04](../diagrams/D-04.svg) | fresh snapshot과 lock 대기 | 8 | 1 | 4 | 3 |
| [D-05](../diagrams/D-05.svg) | 삭제 preview와 최종 삭제 | 4 | 2 | 1 | 1 |
| [D-06](../diagrams/D-06.svg) | 직계 하위 케이스 검색 | 5 | 1 | 4 | 0 |
| [D-07](../diagrams/D-07.svg) | 로그·진행률·ETA | 5 | 2 | 3 | 0 |
| [D-08](../diagrams/D-08.svg) | 다중 대기 취소 | 2 | 2 | 0 | 0 |
| [D-09](../diagrams/D-09.svg) | 매크로 발행·멤버 재구성 | 5 | 3 | 2 | 0 |
| [D-10](../diagrams/D-10.svg) | 선언된 artifact 찾기 | 3 | 2 | 1 | 0 |
| [D-11](../diagrams/D-11.svg) | 폼 변환과 티켓 검증 | 5 | 1 | 4 | 0 |
| [D-12](../diagrams/D-12.svg) | 수치 기반 종료 판정 | 3 | 1 | 2 | 0 |
| [D-13](../diagrams/D-13.svg) | 큐 전체 ETA 조회 · 대량 응답 생성 | 9 | 0 | 9 | 0 |
| [D-14](../diagrams/D-14.svg) | 매크로 진행 ETA · 큐 본문과 중복 조회 | 7 | 0 | 6 | 1 |
| [UC-01-tg](../diagrams/UC-01-tg.svg) | 진입·도움말 | 9 | 6 | 2 | 1 |
| [UC-01-gui](../diagrams/UC-01-gui.svg) | 편집기 기동 | 7 | 6 | 1 | 0 |
| [UC-01-web](../diagrams/UC-01-web.svg) | 초기 화면 | 6 | 3 | 1 | 2 |
| [UC-02-tg](../diagrams/UC-02-tg.svg) | 실시간 현재 CASE | 23 | 10 | 9 | 4 |
| [UC-02-web](../diagrams/UC-02-web.svg) | 대시보드·현황 갱신 | 26 | 10 | 11 | 5 |
| [UC-03-tg](../diagrams/UC-03-tg.svg) | 등록 케이스 목록·선택 | 19 | 13 | 4 | 2 |
| [UC-03-web](../diagrams/UC-03-web.svg) | 등록 케이스 목록 | 7 | 3 | 2 | 2 |
| [UC-04-tg](../diagrams/UC-04-tg.svg) | 상태 상세 | 13 | 6 | 6 | 1 |
| [UC-04-web](../diagrams/UC-04-web.svg) | 상태 상세·ETA | 10 | 3 | 5 | 2 |
| [UC-05-tg](../diagrams/UC-05-tg.svg) | Residual 조회 | 7 | 6 | 0 | 1 |
| [UC-05-gui](../diagrams/UC-05-gui.svg) | Residual 조회 | 2 | 2 | 0 | 0 |
| [UC-05-web](../diagrams/UC-05-web.svg) | Residual 조회 | 6 | 3 | 1 | 2 |
| [UC-06-tg](../diagrams/UC-06-tg.svg) | 결과 파일 조회 | 7 | 6 | 0 | 1 |
| [UC-06-gui](../diagrams/UC-06-gui.svg) | 결과 파일 조회 | 2 | 2 | 0 | 0 |
| [UC-06-web](../diagrams/UC-06-web.svg) | 결과 파일 조회 | 6 | 3 | 1 | 2 |
| [UC-07-tg](../diagrams/UC-07-tg.svg) | 티켓 목록·다중 선택 | 19 | 17 | 0 | 2 |
| [UC-07-gui](../diagrams/UC-07-gui.svg) | 티켓 목록·다중 선택 | 4 | 3 | 1 | 0 |
| [UC-07-web](../diagrams/UC-07-web.svg) | 티켓 목록·선택 | 8 | 6 | 0 | 2 |
| [UC-08-tg](../diagrams/UC-08-tg.svg) | 새 티켓 | 14 | 13 | 0 | 1 |
| [UC-08-gui](../diagrams/UC-08-gui.svg) | 새 티켓 | 5 | 4 | 1 | 0 |
| [UC-08-web](../diagrams/UC-08-web.svg) | 새 티켓 | 5 | 3 | 0 | 2 |
| [UC-09-tg](../diagrams/UC-09-tg.svg) | 티켓 열기 | 14 | 13 | 0 | 1 |
| [UC-09-gui](../diagrams/UC-09-gui.svg) | 티켓 열기 | 5 | 5 | 0 | 0 |
| [UC-09-web](../diagrams/UC-09-web.svg) | 티켓 열기 | 6 | 4 | 0 | 2 |
| [UC-13-tg](../diagrams/UC-13-tg.svg) | 티켓 복제 | 14 | 13 | 0 | 1 |
| [UC-13-gui](../diagrams/UC-13-gui.svg) | 티켓 복제 | 5 | 5 | 0 | 0 |
| [UC-13-web](../diagrams/UC-13-web.svg) | 티켓 복제 | 5 | 3 | 0 | 2 |
| [UC-10-tg](../diagrams/UC-10-tg.svg) | 기본·감시·알림·스크립트 편집 | 18 | 16 | 0 | 2 |
| [UC-10-gui](../diagrams/UC-10-gui.svg) | 기본·감시·알림·스크립트 편집 | 1 | 0 | 1 | 0 |
| [UC-10-web](../diagrams/UC-10-web.svg) | 기본·감시·알림·스크립트 편집 | 5 | 2 | 1 | 2 |
| [UC-11-tg](../diagrams/UC-11-tg.svg) | 티켓 검증 | 11 | 10 | 0 | 1 |
| [UC-11-gui](../diagrams/UC-11-gui.svg) | 티켓 검증 | 3 | 2 | 1 | 0 |
| [UC-11-web](../diagrams/UC-11-web.svg) | 티켓 검증 | 5 | 3 | 0 | 2 |
| [UC-12-tg](../diagrams/UC-12-tg.svg) | 저장·이름 변경 | 20 | 18 | 0 | 2 |
| [UC-12-gui](../diagrams/UC-12-gui.svg) | 저장·이름 변경 | 6 | 4 | 2 | 0 |
| [UC-12-web](../diagrams/UC-12-web.svg) | 저장·이름 변경 | 18 | 8 | 5 | 5 |
| [UC-14-tg](../diagrams/UC-14-tg.svg) | 개별·다중 티켓 삭제 | 20 | 18 | 0 | 2 |
| [UC-14-gui](../diagrams/UC-14-gui.svg) | 개별·다중 티켓 삭제 | 4 | 4 | 0 | 0 |
| [UC-14-web](../diagrams/UC-14-web.svg) | 개별·다중 티켓 삭제 | 18 | 8 | 5 | 5 |
| [UC-15-tg](../diagrams/UC-15-tg.svg) | 매크로 검색·필터·취소 | 28 | 19 | 4 | 5 |
| [UC-15-gui](../diagrams/UC-15-gui.svg) | 매크로 검색·필터 | 13 | 5 | 5 | 3 |
| [UC-15-web](../diagrams/UC-15-web.svg) | 매크로 검색·필터 | 17 | 7 | 5 | 5 |
| [UC-16-tg](../diagrams/UC-16-tg.svg) | 매크로 구성원 선택 | 11 | 9 | 1 | 1 |
| [UC-16-gui](../diagrams/UC-16-gui.svg) | 매크로 구성원 선택 | 3 | 1 | 2 | 0 |
| [UC-16-web](../diagrams/UC-16-web.svg) | 매크로 구성원 순서·제거 | 3 | 0 | 0 | 3 |
| [UC-17-tg](../diagrams/UC-17-tg.svg) | 실행·queue 이름·동적 macro 설정 | 11 | 10 | 0 | 1 |
| [UC-17-gui](../diagrams/UC-17-gui.svg) | 실행·queue 이름·동적 macro 설정 | 1 | 0 | 1 | 0 |
| [UC-17-web](../diagrams/UC-17-web.svg) | 실행·queue 이름·동적 macro 설정 | 3 | 0 | 0 | 3 |
| [UC-18-tg](../diagrams/UC-18-tg.svg) | 즉시 실행·이름 있는 대기열 등록 | 43 | 19 | 17 | 7 |
| [UC-18-gui](../diagrams/UC-18-gui.svg) | 즉시 실행·이름 있는 대기열 등록 | 26 | 9 | 13 | 4 |
| [UC-18-web](../diagrams/UC-18-web.svg) | 즉시 실행·이름 있는 대기열 등록 | 24 | 7 | 12 | 5 |
| [UC-18-legacy-tg](../diagrams/UC-18-legacy-tg.svg) | 케이스 메뉴 실행(우회 경로) | 21 | 10 | 7 | 4 |
| [UC-19-tg](../diagrams/UC-19-tg.svg) | 큐·이력·매크로 진행 | 14 | 6 | 7 | 1 |
| [UC-19-web](../diagrams/UC-19-web.svg) | 큐·이력·매크로 진행 | 5 | 3 | 0 | 2 |
| [UC-20-tg](../diagrams/UC-20-tg.svg) | 자동 시작 pause/resume | 15 | 7 | 7 | 1 |
| [UC-20-web](../diagrams/UC-20-web.svg) | 자동 시작 pause/resume | 5 | 3 | 0 | 2 |
| [UC-19-gui](../diagrams/UC-19-gui.svg) | 큐·이력·매크로 진행 | 7 | 1 | 6 | 0 |
| [UC-21-tg](../diagrams/UC-21-tg.svg) | 대기 작업 선택·취소 | 13 | 9 | 3 | 1 |
| [UC-21-gui](../diagrams/UC-21-gui.svg) | 대기열별 선택·취소 | 3 | 3 | 0 | 0 |
| [UC-21-web](../diagrams/UC-21-web.svg) | 대기열 카드별 선택·취소 | 5 | 3 | 0 | 2 |
| [UC-22-tg](../diagrams/UC-22-tg.svg) | 실패 패턴 템플릿 | 13 | 12 | 0 | 1 |
| [UC-22-gui](../diagrams/UC-22-gui.svg) | 실패 패턴 템플릿 | 4 | 3 | 1 | 0 |
| [UC-22-web](../diagrams/UC-22-web.svg) | 실패 패턴 템플릿 | 6 | 4 | 0 | 2 |
| [UC-23-tg](../diagrams/UC-23-tg.svg) | 폴더·로그·Residual 선택 | 13 | 10 | 2 | 1 |
| [UC-23-gui](../diagrams/UC-23-gui.svg) | 폴더·로그·Residual 선택 | 2 | 1 | 0 | 1 |
| [UC-23-web](../diagrams/UC-23-web.svg) | 폴더·파일 선택 | 6 | 3 | 1 | 2 |
| [UC-24-tg](../diagrams/UC-24-tg.svg) | 입력 취소·초안 폐기·검색 취소 | 9 | 8 | 0 | 1 |
| [UC-24-gui](../diagrams/UC-24-gui.svg) | 저장/폐기/취소·닫기 | 3 | 2 | 0 | 1 |
| [UC-24-web](../diagrams/UC-24-web.svg) | 초안 폐기·modal 취소 | 2 | 0 | 0 | 2 |
| [UC-25-tg](../diagrams/UC-25-tg.svg) | 대화 일괄 정리 | 9 | 6 | 2 | 1 |
| [UC-26](../diagrams/UC-26.svg) | 외부 PC 접속과 브라우저 | 1 | 0 | 0 | 1 |
| [UC-27](../diagrams/UC-27.svg) | 운영 CLI 명령 분기 | 15 | 7 | 5 | 3 |
| [UC-28-tg](../diagrams/UC-28-tg.svg) | 실행 중 managed 작업 중단 | 10 | 7 | 2 | 1 |
| [UC-28-gui](../diagrams/UC-28-gui.svg) | 실행 중 managed 작업 중단 | 4 | 3 | 0 | 1 |
| [UC-28-web](../diagrams/UC-28-web.svg) | 실행 중 managed 작업 중단 | 5 | 3 | 0 | 2 |
| [BG-01](../diagrams/BG-01.svg) | solver·monitor CPU를 합치는 scanner | 11 | 2 | 1 | 8 |
| [BG-02](../diagrams/BG-02.svg) | 현재 CASE · 이전 실행/종료 확인 중 CASE 감시 | 30 | 13 | 14 | 3 |
| [BG-03](../diagrams/BG-03.svg) | 변경된 제출 티켓 접수 | 7 | 4 | 3 | 0 |
| [D-15](../diagrams/D-15.svg) | 증분 상태 반영 · 종료 child와 부모 macro 재시도 | 15 | 7 | 8 | 0 |
| [D-16](../diagrams/D-16.svg) | 실행 중 managed 작업 안전 중단 | 8 | 5 | 1 | 2 |
| [BG-04](../diagrams/BG-04.svg) | 현재 head 기반 quota·동적 borrow admission | 24 | 6 | 14 | 4 |
| [BG-05](../diagrams/BG-05.svg) | worker·solver·hooks·판정 | 15 | 13 | 1 | 1 |
| [BG-06](../diagrams/BG-06.svg) | outbox 알림 전달·checkpoint | 10 | 8 | 2 | 0 |
| [BG-07](../diagrams/BG-07.svg) | worker 소실·재시작 복구 | 8 | 5 | 3 | 0 |
| [UC-05-file-web](../diagrams/UC-05-file-web.svg) | Residual 실제 HTTP 전송 | 6 | 2 | 3 | 1 |
| [UC-06-file-web](../diagrams/UC-06-file-web.svg) | 결과 파일 실제 HTTP 전송 | 6 | 2 | 3 | 1 |
| [UC-10-exports-tg](../diagrams/UC-10-exports-tg.svg) | 요청 데이터 정의 편집 | 18 | 16 | 0 | 2 |
| [UC-10-exports-web](../diagrams/UC-10-exports-web.svg) | 요청 데이터 정의 검증 | 5 | 3 | 0 | 2 |
| [UC-10-exports-gui](../diagrams/UC-10-exports-gui.svg) | 요청 데이터 정의 편집 | 4 | 3 | 1 | 0 |

Python 노드별 basic/detailed 구분은 source map entries/flows에 있다. 기본 업무 함수가 없는 순수 내부 계산 시퀀스는 호출한 상위 업무 경계와 오류로 관측한다. 상세 함수 호출/분기 재구성이 필요하면 사전에 detailed를 켠다.
