# #31 보관 정리 검증 (2026-10-16)

정리는 요청 경로 밖에서 실행한다. 다음 수치는 해당 호스트의 한 차례 측정이며 일반적인 응답 성능이나 #32 로거 자체 비용 계측 결과가 아니다.

| 작업 | 파일 수 | wall | CPU | 결과 |
|---|---:|---:|---:|---|
| 운영 최초 dry-run, 캐시가 차갑던 구간 | 87,654 | 76.40초 | 3.32초 | 75,295개 후보; sandbox PID namespace여서 열린 legacy 판정 수치는 배포 판단에 사용하지 않음 |
| 호스트 권한 재확인 dry-run, warm cache | 87,678 | 1.078초 | 1.069초 | 75,312개 후보, 열린 legacy 1개 유예 |
| 운영 실제 초기 삭제, warm cache | 87,695 | 2.853초 | 2.145초 | 75,322개 / 5,255,217,247 bytes 삭제, 실패 0 |
| fixture dry-run, warm cache | 14,500 | 0.170초 | 0.169초 | 12,000개 후보 |
| fixture 삭제, warm cache | 14,500 | 0.348초 | 0.285초 | 12,000개 삭제, 2,500개 유지, 실패 0 |

운영 초기 정리 직후 순회 집계는 12,373개·476,224,538 bytes였다. 로그는 계속 생성되므로 이후 수치는 달라진다. 최초 dry-run은 256-byte header 확인에도 파일시스템 read-ahead로 약 1.17 GB의 물리 읽기 counter 증가가 있었다. warm cache에서 운영 삭제와 fixture는 read_bytes 증가가 0이었다. 이 때문에 디렉터리 순회를 요청마다 수행하지 않고 5분마다 별도 낮은 CPU/I/O 우선순위 프로세스로 실행한다. timer 로그에 실행별 wall/CPU와 `/proc/self/io`, rusage block counter 차이를 남긴다. 이는 디스크 지연의 정확한 기여율을 의미하지 않는다.

`_Sink.write`에 20,000개 합성 record를 입력하고 close까지 재는 8회 교차 실행의 median은 변경 전 71.688ms, 변경 후 72.392ms(+0.98%)였다. 기록 오류 0·20,000건·출력 바이트 양수를 확인했다. 시간 회전 경계에 도달하지 않는 buffered sink microbenchmark이며, 사용자 요청 p95나 shell의 전체 처리 비용을 측정한 것은 아니다. 새 경과 시간 검사는 Python에서 batch drain당, shell에서 실제 기록당 수행한다.

만료 경계, dry-run, 크기 회전 백업, 최근 기록 혼합, symlink/비대상, 열린 legacy, FD 접근 제한, 동시 정리, 삭제 실패, 파일 교체, Python/shell 시간 회전, idle 만료 후 재기록, 절전 후 wall clock 전환을 테스트한다. 기존 로그 테스트는 크기 회전·trace·해독·OFF·예외/반환 보존을 함께 검증한다.

테스트는 실제 Telegram 전송이나 OpenFOAM 계산을 시작하지 않았다. 전체 unittest에서 티켓 삭제 관련 기존 실패 2건(`test_bulk_cancel_and_busy_ticket_keep_all_files`, `test_bulk_selection_survives_pagination_and_deletes_only_confirmed_files`)이 있었고, HEAD의 원래 diagnostics.py로 바꿔 메모리에 로드해 두 테스트를 재실행해도 동일하게 실패했다. 이 변경에서는 해당 티켓 로직을 수정하지 않았다.

보관 정책·설치·지연 상한·예외는 [DIAGNOSTICS](../DIAGNOSTICS.md#보관과-읽기), 사전 구조는 [설계 이력](../history/2026-10-16-diagnostic-retention.md)을 참고한다. 운영 원본 집계는 로컬 `state/investigations/2026-10-16-retention/initial-cleanup.json`에 저장했다.

배포 후 timer 실행은 12,402개를 조회하고 만료 7개를 0.051초(wall) / 0.051초(CPU)에 정리했다. 오류·열린 legacy 유예는 0이며 마지막 결과를 `state/diagnostics/retention-status.json`에서 읽을 수 있다. Telegram/web은 재시작 뒤 active/running, web HTTP 200 및 basic 활성화, ofps/Python 신규 시간 segment 생성을 확인했다.

최종 운영 작업 공간 통합 검증: **412개 테스트 통과, 1개 skip** (52.67초). 이 작업 공간에는 기존 진행 중이던 #34 티켓 변경도 포함되어 있다. 별도 #31 브랜치의 기준 main에서 재현된 위 2건과 통합 결과를 구분한다. 새 보관/기존 로그 테스트 34개도 별도로 통과했다. compileall, 실제 bot.json check(4개 케이스), Bash 및 systemd unit 문법, SVG XML parse/실제 렌더링을 확인했다.
