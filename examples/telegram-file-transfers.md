# Telegram 파일 요청 예시

기존 티켓의 `residual_pattern`과 `exports` 설정을 그대로 사용한다.

1. Residual 버튼을 누른 뒤 `/stat`을 입력하면 이미지 전송 중에도 현재 계산을 조회한다.
2. 같은 이미지 버튼을 전송 중 반복해서 누르면 한 번만 전송한다. 완료 후 다시 누르면 최신 파일을 새로 요청한다.
3. 다른 파일은 최대 2개를 동시에 전송한다. 전체 active+pending이 16개면 추가 요청에 대기 포화 안내를 보낸다.
4. `/clean`은 메시지 삭제와 함께 해당 대화의 대기 전송을 취소한다. 이미 전송 중이던 파일이 뒤늦게 도착하면 그 메시지도 삭제한다.
5. 연결/응답 실패는 파일명과 재요청 안내를 보낸다. 자동 재전송은 하지 않는다.

GUI는 기존 결과 경로 목록, web은 기존 preview/download를 사용한다. 파일 경로·symlink 규칙은 공용 helper를 공유한다. 자동 종료 알림은 SQLite outbox에서 계속 재시도하며 위 대화형 queue와 별개다.

진단 시 `telegram.transfer.started.queue_ms`와 `telegram.transport.phase`의 단계별 시간을 비교한다. 대화형 파일 대기는 프로세스 메모리에 있으므로 봇 재시작 후에는 다시 요청한다.
