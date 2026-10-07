# 진단 로그 성능 실측 — 2026-10-07

**판정: 성능 완료 조건 미달. 기본 OFF, Draft PR, 운영 미배포.** OFF의 제안 목표는 기준본 대비 p95 증가 max(1%, 1 ms), ON은 OFF 대비 max(5%, 5 ms)다. 일부 OFF와 여러 ON 시나리오가 목표를 넘겼다. 기능 구현과 성능 수용을 구분하며 #18을 닫지 않는다.

## 측정 방법과 한계

기준본은 `ebf494f`다. Python 3.9.25, 동일 호스트에서 각 mode를 새 인터프리터로 실행했다. 임시 40/921개 티켓, 활성 CASE 40개, fake ofps/transport를 사용했고 실제 Telegram 전송·OpenFOAM 실행은 하지 않았다. 각 시나리오 20회 표본이다. CPU는 측정 구간 process CPU 합, RSS는 프로세스 peak다. 실제 HTTP 네트워크 시간·동시 요청·실제 Tk 화면·Windows/macOS 네이티브 성능은 이 표에 포함하지 않는다.

통합 20회 표본은 batch/cache 최적화 시점이며 이후 dict 식별 필드 요약·trace root 등의 소규모 수정 전이다. 후속 요약 변경은 별도의 5회 ON 표본으로 아래에 표시한다. 최종 커밋의 모든 조합을 재측정한 결과로 해석하지 않는다. 호스트 부하 영향을 받는 소수 표본이며 p95는 경험적 순위 통계다.

## Python p95 (ms)

| 티켓 수 | 시나리오 | 기준본 | OFF | ON |
|---:|---|---:|---:|---:|
| 40 | warm_catalog | 2.41 | 2.55 | 4.52 |
| 40 | one_ticket | 0.69 | 0.70 | 2.32 |
| 40 | telegram_stat | 53.06 | 57.28 | 180.06 |
| 40 | web_overview | 94.98 | 97.53 | 282.01 |
| 40 | log_tail_2000_lines | 19.53 | 22.34 | 138.45 |
| 921 | warm_catalog | 61.52 | 76.66 | 73.89 |
| 921 | one_ticket | 0.97 | 0.73 | 2.98 |
| 921 | telegram_stat | 56.96 | 68.05 | 182.31 |
| 921 | web_overview | 588.14 | 520.77 | 2159.66 |
| 921 | log_tail_2000_lines | 26.44 | 20.77 | 192.63 |

## CPU·메모리·출력

아래 수치는 각 mode의 5개 시나리오 × 20회 전체 측정이다. 기록 수/bytes에는 준비와 warmup도 포함한다. 출력 bytes는 회전으로 삭제된 파일까지 포함한 누적 기록량이며 보관량과 다르다.

| 티켓 수 | mode | CPU 합 ms | peak RSS KiB | 기록 수 | 누적 출력 MiB | 보관 MiB |
|---:|---|---:|---:|---:|---:|---:|
| 40 | baseline | 2963 | 30368 | 0 | 0.00 | 0.00 |
| 40 | off | 3063 | 31200 | 0 | 0.00 | 0.00 |
| 40 | on | 10976 | 32732 | 294961 | 92.65 | 73.10 |
| 921 | baseline | 11606 | 47408 | 0 | 0.00 | 0.00 |
| 921 | off | 12198 | 47780 | 0 | 0.00 | 0.00 |
| 921 | on | 48213 | 57284 | 983984 | 416.57 | 76.85 |

통합 Python 측정의 log_errors는 모두 0이었다. OFF는 진단 기록과 출력 bytes가 0이다. 기록량·CPU 증가가 커서 파일 회전만으로 성능 요구가 해결되지는 않는다.

## 후속 dict 요약 변경 (ON, 921개, 5회)

| 시나리오 | p50 ms | p95 ms |
|---|---:|---:|
| warm_catalog | 56.26 | 58.49 |
| one_ticket | 2.27 | 2.35 |
| telegram_stat | 170.07 | 173.41 |
| web_overview | 1937.15 | 1957.03 |
| log_tail_2000_lines | 137.52 | 140.95 |

web 조회 p95가 약 1.96초로 줄었으나 목표는 여전히 미달이다. 표본 수가 달라 통합 결과와 엄밀한 효과 크기 비교를 할 수 없다.

## 실제 Chromium UI 렌더 (20회, p95 ms)

| 행 수 | 기준본 | OFF | ON |
|---:|---:|---:|---:|
| 40 | 0.3 | 0.3 | 7.5 |
| 1,000 | 2.0 | 3.5 | 138.1 |

임시 web fixture의 합성 티켓 목록 렌더만 측정했다. 약 8 MiB 로컬 ring buffer의 오래된 기록은 덮어쓰며 export에 건수를 명시한다. OFF 결과의 retained 252개는 OFF 전 bootstrap 기록이다. 측정 OFF 동안 새 기록은 생성하지 않았다. 입력 지연·frame drop을 포함한 장시간 실사용 검증은 하지 않았다.

## ofps 실호스트 read-only snapshot (1회 warmup + 5회)

| mode | p50 ms | p95 ms | 누적 log bytes |
|---|---:|---:|---:|
| baseline | 4039.62 | 4441.03 | 0 |
| off | 3851.79 | 4592.48 | 0 |
| on | 4472.27 | 4885.59 | 2508455 |

`CFD_BOT_OFPS_MANAGED=1`에서 동일 OFPS_BASILISK_ROOT로 실행했다. DB sync·계산 시작은 수행하지 않았고 세 mode 각각 6회 모두 exit 0이었다. 실제 프로세스 목록과 시스템 부하는 변할 수 있다. ON p95는 OFF보다 약 6.4% 증가했다.

## 재현과 원자료

[Python benchmark](benchmark_diagnostics.py)는 임시 fixture만 사용한다. `--source`에 기준 checkout 또는 변경 checkout을 전달하고 각 mode/티켓 수를 별도 프로세스로 실행한다.

```bash
python3 docs/analysis/benchmark_diagnostics.py --source /path/to/baseline --mode baseline --members 921 --samples 20
python3 docs/analysis/benchmark_diagnostics.py --source /path/to/change --mode off --members 921 --samples 20
python3 docs/analysis/benchmark_diagnostics.py --source /path/to/change --mode on --members 921 --samples 20
```

[브라우저 benchmark](benchmark_browser_diagnostics.cjs)는 실행 중인 `tests.web_fixture`의 `/tmp/cfd-web-fixture.json`과 기준본 app.js를 `/tmp/cfd-app-baseline.js`에 준비하고 Playwright Chromium으로 실행한다. launcher·실제 Telegram 네트워크 성능은 별도로 남은 검증이다.

- [python-integration.json](diagnostic-results/python-integration.json)
- [python-projection-followup.json](diagnostic-results/python-projection-followup.json)
- [browser.json](diagnostic-results/browser.json)
- [ofps.json](diagnostic-results/ofps.json)

남은 완료 조건은 모든 HLD/LLD 시퀀스의 원인 추적 정보를 보존하면서 대량/빈번 경로의 ON/OFF 비용을 낮추고, 동일 최종 revision에서 전체 비교를 다시 통과하는 것이다. 이벤트를 누락하여 성능 합격으로 표시하지 않는다.
