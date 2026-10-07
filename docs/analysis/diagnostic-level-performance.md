# 기본/상세 로그 분리 성능 (#30)

**정상 921개 조회는 OFF 635.86 ms, basic 685.82 ms, detailed 5,021.94 ms였다.** 기본 모드의 중앙값 추가 지연은 약 7.9%, 상세 대비 시간은 약 86.3% 감소했다. 이는 내부 정상 helper 호출/분기를 수집하지 않는 변경이며 같은 전체 로그를 무손실 압축한 결과가 아니다.

## 조건

같은 수정본·호스트·Python 3.9.25, 임시 티켓 921개/활성 CASE 40개, 가짜 snapshot/transport를 사용했다. 각 모드를 새 interpreter에서 OFF → basic → detailed 순서로 실행했다. 시나리오별 3회 warmup과 5회 측정의 p50/p95다. p95는 작은 표본의 경험적 순위값이므로 운영 SLA 보증이 아니다. CPU는 측정 구간 합, VmHWM은 Linux process peak다. 마지막 shell 로그 필터의 가짜 ERR 제거는 이 Python/browser 측정 이후 별도 회귀 테스트로 확인했으며 측정된 Python/browser 경로는 바뀌지 않았다.

실제 Telegram 전송·solver 시작·장기 동시 요청/느린 디스크를 측정하지 않았다. 브라우저는 같은 app/fixture의 Chromium 20회 렌더와 3회 warmup이며 숫자는 함수 실행 시간이다. 실제 화면 paint나 end-to-end 네트워크 지연과 다르다.

## Python p50 / p95 (ms)

| 조건 / 시나리오 | OFF | basic | detailed |
|---|---:|---:|---:|
| CPU 설정 누락 / warm_catalog | 35.24 / 35.39 | 42.81 / 43.18 | 36.73 / 36.75 |
| CPU 설정 누락 / one_ticket | 0.51 / 0.52 | 0.84 / 0.84 | 1.91 / 1.93 |
| CPU 설정 누락 / telegram_stat | 35.15 / 35.36 | 50.17 / 50.25 | 107.48 / 107.76 |
| CPU 설정 누락 / web_overview | 331.11 / 333.09 | 551.80 / 552.51 | 1152.36 / 1166.02 |
| CPU 설정 누락 / log_tail_2000_lines | 13.92 / 13.96 | 14.46 / 14.62 | 125.50 / 134.43 |
| 정상 설정 / web_overview | 635.86 / 726.15 | 685.82 / 799.84 | 5021.94 / 5048.59 |

설정 누락 조회의 basic은 OFF 대비 약 66.6% 느리다. 티켓 881개에서 발생하는 실제 예외와 전파·catch 기록을 보존했기 때문이다. 경고·예외를 없애서 빠르게 만들지 않았다. 정상 경로에서도 비용이 0은 아니며 모든 시나리오가 같은 개선율을 갖지 않는다.

## 기록량 / CPU / 메모리

누적 기록 수와 bytes는 준비·warmup을 포함한다. 설정 누락은 5개 시나리오의 합이며 정상 설정은 web 조회만이므로 다른 fixture끼리 직접 비교하지 않는다. bytes는 batch 출력량이고 map/codebook/header의 보관량은 raw JSON의 log_bytes에 따로 있다.

| 설정 | 모드 | 기록 수 | batch bytes | CPU 합 ms | VmHWM KiB |
|---|---|---:|---:|---:|---:|
| 누락 | off | 0 | 0 | 2022 | 48,744 |
| 누락 | basic | 43,029 | 15,190,032 | 3226 | 55,996 |
| 누락 | detailed | 438,014 | 81,185,614 | 7259 | 63,356 |
| 정상 | off | 0 | 0 | 3347 | 48,624 |
| 정상 | basic | 12,042 | 1,535,684 | 3555 | 57,544 |
| 정상 | detailed | 2,285,362 | 237,641,845 | 24313 | 66,512 |

정상 조회 workload는 detailed 2,285,362건 → basic 12,042건(99.47% 감소), 237,641,845 → 1,535,684 bytes(99.35% 감소)다. 숫자 사전·공유 값/예외 저장 방식은 유지한다. 구분한 업무 사건/경고/예외는 샘플링하지 않는다. 출력 오류 계수는 모든 측정에서 0이었다.

## Chromium

| 행 수 | 모드 | p50 ms | p95 ms | 생성 사건 (warmup 포함 23회) |
|---:|---|---:|---:|---:|
| 40 | off | 0.20 | 0.90 | 0 |
| 1000 | off | 3.50 | 4.50 | 0 |
| 40 | basic | 0.20 | 0.70 | 46 |
| 1000 | basic | 3.70 | 5.20 | 46 |
| 40 | detailed | 2.40 | 4.40 | 18,446 |
| 1000 | detailed | 72.55 | 108.50 | 460,046 |

1,000행 렌더에서 basic은 ticketList의 call/return 2개씩만 남겨 총 46개 사건을 생성했다. detailed는 esc/map/filter 등 전체 호출을 남겨 460,046개다. 클릭·필드 변경·HTTP/서버 trace·예외는 두 ON 모드에 유지된다. p95는 basic 5.2 ms, OFF 4.5 ms로 차이가 남는다. buffer byte 수는 내용 추정값이며 JS heap의 엄격한 상한이 아니다.

## 검증 / 한계

- compileall 및 전체 Python unittest 347개 통과(inotify 환경 1개 skip). shell 필터의 가짜 ERR 제거 이후 최종 전체 회귀도 61.236초에 통과했다.
- 기본 모드 helper 정상 요약 생략, 예외 원본 객체/stack·catch·warning, generator send/throw/close, 티켓 파일 변경·job ID·commit, solver warning/fatal 보존, child/env 수준 전파를 검증했다.
- 실제 Telegram/GUI/web adapter 메서드는 가짜 transport/widget/service로 caller/actor/업무 경계 기록을 검사했다. 전체 Chromium 티켓·큐·설정·편집·파일 선택·매크로·모바일 시나리오가 통과했다. 네이티브 Tk 화면/Windows/macOS는 이번 환경에서 실행하지 않았다.
- 브라우저 기본/상세 전환과 CSRF 제외, HTTP correlation, 51개 사건의 compact export/decoder 왕복, falsey throw/Promise rejection을 확인했다.
- 호스트 managed ofps snapshot: 종료 0, 관측 프로세스 기록 30개, 총 483개 사건 decode. 일반 helper의 정상 call/return은 제외하며 nonzero return과 ERR는 유지한다. 새 필터 함수 자체의 실패를 가짜 shell.error로 기록하던 문제도 제거하고 회귀 테스트했다.
- 운영 설정 read-only check: 985개 CASE 정상. 운영 checkout/config/service 변경이나 재시작은 하지 않았다. PR #29의 변경이며 기본 OFF는 유지한다.
- 99개 HLD/LLD 시퀀스의 노드별 basic/detailed 매핑, 소스 지문, SVG XML/렌더를 확인했다. 기본에서 생략한 정상 내부 호출은 과거 문제 발생 후 복원할 수 없다.

## 재현 / 원자료

```bash
python3 docs/analysis/compare_diagnostic_levels.py --output /tmp/levels.json --samples 5
# 임시 tests.web_fixture 및 Playwright Chromium 필요
node docs/analysis/benchmark_browser_levels.cjs
```

[Python 원자료](diagnostic-results/levels-python.json) · [브라우저 원자료](diagnostic-results/levels-browser.json) · [수집 계약](../DIAGNOSTICS.md) · [사전 설계](../history/2026-10-07-diagnostic-logging-levels.md)
