# 운영에 접근하지 않는 티켓 색인 검증 예

저장소 루트에서 `python3 docs/analysis/benchmark_ticket_index.py`를 실행한다. 스크립트는 임시 디렉터리에 매크로 1개와 child 921개, 별도 SQLite를 만들고 종료 시 정리한다. ofps·OpenFOAM·Telegram은 실행하지 않는다. 기존/새 membership, cold/warm catalog, 단일 변경 및 활성 1개 상태 반영 시간을 `docs/analysis/ticket-index-results.json`에 기록한다.

실행 명령과 데이터 생성·정리 코드는 [benchmark_ticket_index.py](../docs/analysis/benchmark_ticket_index.py)에 있다. 전체 검증은 최초와 복구·check에서 유지하고, 변경 없는 반복 조회의 JSON 재검증은 0회여야 한다. 실제 운영 응답 시간은 별도 ofps·ETA·Telegram 전송 시간을 포함한다.
