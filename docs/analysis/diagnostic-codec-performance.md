# 숫자 코드 기록 적용 전후 성능 (#30)

**숫자 코드·공유 값/예외·browser 숫자 buffer를 구현했다. 저장량은 줄었지만 상시 ON 지연 목표는 아직 미달이다.** 정상 설정 921개 조회의 ON 중앙값은 5.56초 → 4.69초, 측정 workload의 누적 출력량은 약 684 MB → 238 MB였다. parser는 오히려 느려졌고 browser p95도 개선되지 않았다. 기본 OFF와 Draft 상태를 유지한다.

## 조건

같은 호스트, Python 3.9.25, 임시 921개 티켓/활성 CASE 40개, 가짜 process snapshot/transport, 새 인터프리터를 사용했다. 이전 형식은 `7efff10`, 새 형식은 본 구현의 호출/요약 최적화판이다. 마지막 예외 참조 상한과 falsey JS 예외 보완은 이 측정 후 별도 회귀 테스트로 확인했다. 원자료의 v1/OFF는 순차 비교, v2는 마지막 인자 처리 최적화 후 같은 fixture로 다시 실행했다. mode별 동시 실행은 하지 않았다. 3회 warmup 후 5회 측정했다. 작은 표본의 p95는 경험적 순위값이며 통계적 개선 보증으로 보지 않는다. 실제 Telegram 전송/계산은 시작하지 않았다.

## Python p50 / p95 (ms)

| 시나리오 | 이전 ON p50 / p95 | 현재 OFF p50 / p95 | 현재 ON p50 / p95 |
|---|---:|---:|---:|
| CPU 설정 누락: warm_catalog | 36.58 / 38.40 | 35.04 / 35.14 | 35.68 / 35.84 |
| CPU 설정 누락: one_ticket | 1.62 / 1.66 | 0.63 / 0.63 | 1.84 / 1.88 |
| CPU 설정 누락: telegram_stat | 103.47 / 103.62 | 43.06 / 43.07 | 104.25 / 104.73 |
| CPU 설정 누락: web_overview | 1296.63 / 1340.27 | 329.36 / 329.41 | 1266.16 / 1308.64 |
| CPU 설정 누락: log_tail_2000_lines | 95.16 / 95.80 | 14.01 / 14.04 | 105.72 / 105.86 |
| 정상 설정: web_overview | 5561.06 / 5699.44 | 724.33 / 752.45 | 4690.44 / 5033.01 |

숫자 치환의 자체 dictionary 처리도 CPU를 사용한다. 2,000줄 parser의 ON 중앙값은 95.16 → 105.72 ms로 증가했다. 단일 티켓도 1.62 → 1.84 ms로 증가했다. 개선된 조회 경로만 골라 성능 완료로 표시하지 않는다.

## 기록량·CPU·메모리

설정 누락은 5개 시나리오, 정상 설정은 web 조회만 측정했다. 건수/bytes는 fixture 준비와 warmup을 포함한 누적 값이다. CPU 합은 5회 측정 구간의 process CPU다. 누적 bytes는 writer의 batch 출력 계수이며 header와 별도 사전 파일은 보관량에 포함된다. 서로 다른 workload 행을 직접 비교하지 않는다.

| fixture | mode | 논리 기록 수 | 누적 batch bytes | 보관 bytes | CPU 합 ms | VmHWM KiB |
|---|---|---:|---:|---:|---:|---:|
| 설정 누락 | v1 | 438,014 | 174,414,648 | 70,028,857 | 7914 | 57,960 |
| 설정 누락 | off | 0 | 0 | 0 | 2066 | 47,784 |
| 설정 누락 | v2 | 438,014 | 81,178,492 | 81,755,726 | 7530 | 72,424 |
| 설정 있음 | v1 | 2,285,362 | 684,244,695 | 75,966,304 | 27662 | 58,064 |
| 설정 있음 | off | 0 | 0 | 0 | 3475 | 48,816 |
| 설정 있음 | v2 | 2,285,362 | 237,629,333 | 70,370,392 | 23986 | 66,944 |

이전/현재 ON의 논리 기록 수는 설정 누락 438,014개, 설정 있음 2,285,362개로 각각 같았다. occurrence를 샘플링해서 크기를 낮추지 않았다. 함수 dict 입력은 정의된 식별/상태/용량 필드와 개수로 수집하므로 예전의 범용 요약과 모든 필드가 동일하다는 뜻은 아니다. 값 사전/인자 cache로 메모리는 증가했다. OFF는 기록과 출력이 0이다.

측정 도구의 escalated 실행에서 `ru_maxrss`가 실행 부모의 약 1.9 GiB peak를 상속하는 현상을 확인했다. 이번 결과는 Linux `/proc/self/status`의 **VmHWM**을 사용한다. 이 문제를 발견하기 전의 탐색 측정 RSS는 최종 표에 사용하지 않았다.

## Chromium (20회 렌더)

동일 fixture/app에서 이전 recorder, 현재 OFF, 현재 recorder를 차례로 실행했다. 각 행 크기마다 3회 warmup 후 20회 측정했다. 제품의 8 MiB 순환 보관 설정을 사용했다.

| 행 수 | mode | p50 ms | p95 ms | 보관 event | 덮어쓴 event |
|---:|---|---:|---:|---:|---:|
| 40 | v1 | 3.50 | 4.20 | 8,031 | 10,625 |
| 1000 | v1 | 71.95 | 76.90 | 9,666 | 469,036 |
| 40 | off | 0.10 | 0.90 | 210 | 0 |
| 1000 | off | 1.90 | 2.70 | 210 | 0 |
| 40 | v2 | 1.80 | 5.00 | 10,720 | 7,936 |
| 1000 | v2 | 57.10 | 89.90 | 17,134 | 461,568 |

1,000행 p50는 71.95 → 57.10 ms지만 p95는 76.90 → 89.90 ms로 증가했다. JSON 직렬화를 export 시점으로 옮겨도 JS summary/객체 생성과 GC가 남아 있다. OFF의 보관 event는 OFF 전 bootstrap 기록이며 측정 중 새 기록은 없다. v1의 문자열 크기 계수와 v2의 객체 내용 추정 예산은 동일한 실제 heap 측정이 아니다.

기능 테스트의 456개 사건을 compact export한 파일은 85,100 bytes, 같은 사건을 펼친 JSONL은 164,015 bytes로 약 48.1% 작았다. Python decoder로 모든 사건이 같은 내용으로 복원됨을 확인했다. 이는 작은 해당 표본의 크기 비교이며 모든 브라우저 세션의 절감률을 보장하지 않는다.

## 검증과 적용 범위

- 최종 전체 Python unittest 340개 통과(inotify 환경 1개 skip), compileall 통과.
- 구형 schema 1, 숫자 형식/회전/타입/예외 객체·전파/메모리 참조 해제/SQLite 읽기 context 의미/원래 subprocess·shell 결과 보존을 검증했다.
- 실제 Chromium의 티켓/큐/편집/파일 선택/실행 설정/모바일 흐름, ON/OFF와 즉시 JSON 생성 없음, 비밀값 제외 및 compact 왕복을 확인했다.
- HLD/LLD 99개 시퀀스 mapping, 코드 사전 생성, 새 SVG XML/렌더를 확인했다.
- 운영 설정 read-only check 985개 CASE와 managed ofps snapshot 1,184개 기록 decode를 확인했다. 실제 Windows/macOS/Tk 화면, 장시간 동시 요청, 프로세스 강제 종료 직전 buffer 손실은 추가 운영 검증 범위다.

구현은 PR #29의 코드에 적용했으며 운영 checkout/config/service를 변경하거나 재시작하지 않았다. 엄격한 ON/OFF 성능 수용 조건은 아직 미달이므로 #18/#30을 닫거나 상시 ON 운영 준비 완료라고 표시하지 않는다.

## 재현/자료

```bash
python3 docs/analysis/compare_diagnostic_codecs.py --previous-source /path/to/7efff10 --output /tmp/codec-comparison.json --samples 5
# 임시 tests.web_fixture와 Playwright Chromium 필요
node docs/analysis/benchmark_browser_codec.cjs /path/to/7efff10
```

- [Python 비교 원자료](diagnostic-results/codec-python.json)
- [Browser 비교 원자료](diagnostic-results/codec-browser.json)
- [코드 번호/필드/decoder 사전](diagnostic-codebook.md)
