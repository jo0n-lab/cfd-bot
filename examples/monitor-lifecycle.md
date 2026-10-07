# 계산 종료 후 모니터링이 남는 경우

`alone-case.json`이나 macro child 티켓의 형식 변경 없이 적용된다.

| ofps CASE 상태 | CPU 현황·다음 작업의 배정 검사 | 계산 알림 |
|---|---|---|
| solver + monitor 실행 중 | 두 프로세스의 CPU 합집합 | 계산 실행 하나 |
| managed 계산 완료, monitor만 남음 | 남은 monitor CPU도 사용 중 | worker의 종료 알림만 발행 |
| 외부 solver 종료, monitor만 남음 | 남은 monitor CPU도 사용 중 | 기존 계산을 missing_polls 후 종료 판정 |
| 같은 경로에서 새 solver 실행 | 새 solver + 남은 monitor CPU | 새 계산 시작을 정상 감지 |

monitor만 관측된 CASE에서는 새 외부 계산과 종료 알림을 만들지 않는다.
서버의 실제 계산이나 Telegram 전송 없이 재현:

```bash
python3 -m unittest -q tests.test_core.MonitorTests
```
