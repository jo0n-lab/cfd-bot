# ofps + Telegram CFD bot

Linux 호스트의 OpenFOAM/Basilisk 계산을 `ofps`로 조회하고 Telegram과 localhost 웹에서 상태 확인, 결과 요청과 FIFO 실행을 제공합니다. 자동 종료 알림은 Telegram으로 전송합니다. Python 3.9 이상과 표준 라이브러리만 사용합니다.

![ofps Telegram CFD bot 아키텍처](docs/ofps-telegram-architecture.svg)

## Prerequisite: `ofps` 설치

이 저장소는 `ofps`를 포함합니다. 소스는 `/home/joon/telegram/cfd_bot/bin/ofps`이며 Bash 실행 파일이므로 별도 compile·build 단계가 없습니다. bot 확장 명령과 티켓 상태 동기화에 같은 저장소의 `cfd_bot` Python package를 사용하므로 파일을 복사하지 말고 symlink로 설치합니다.

```bash
cd /home/joon/telegram/cfd_bot
chmod +x bin/ofps
mkdir -p ~/.local/bin
ln -sfn "$PWD/bin/ofps" ~/.local/bin/ofps
export PATH="$HOME/.local/bin:$PATH"
hash -r
readlink -f "$(command -v ofps)"
ofps --help
```

마지막 경로는 `/home/joon/telegram/cfd_bot/bin/ofps`여야 합니다. 저장소를 옮기면 symlink와 `bot.json`의 `ofps_command` 절대 경로를 함께 수정합니다.

```json
"ofps_command": ["/home/joon/.local/bin/ofps"]
```

필수 런타임은 Bash, Python 3.9 이상, Linux `/proc`, `awk`, `ps`, `readlink`, `taskset`입니다. OpenFOAM 계산 실행에는 별도로 OpenFOAM과 MPI 환경이 필요합니다. 선택 경로는 다음 환경변수로 바꿀 수 있습니다.

| 환경변수 | 용도 |
|---|---|
| `CFD_BOT_CONFIG` | 기본 `bot.json` 대신 사용할 설정 경로 |
| `CFD_BOT_PYTHON` | bot 확장 명령에 사용할 Python 실행 파일 |
| `OFPS_BASILISK_ROOT` | Basilisk 실행 파일을 식별할 설치 root |

## 빠른 시작

```bash
cd /home/joon/telegram/cfd_bot
ln -sfn "$PWD/bin/cfd-ticket-gui" ~/.local/bin/cfd-ticket-gui
bin/set-telegram-token
python3 -m cfd_bot --config bot.json check
```

`bot.json`의 `allowed_user_ids`와 `chat_ids`를 실제 Telegram ID로 설정합니다. ID를 모르면 봇에 `/start`를 보낸 뒤 `python3 -m cfd_bot --config bot.json identify`로 확인합니다.

서비스를 설치하고 시작합니다.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/cfd-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cfd-bot.service
```

토큰은 `bot.json`에 넣지 않습니다. `bin/set-telegram-token`이 `~/.config/cfd-bot/telegram.env`에 권한 `600`으로 저장합니다.

## localhost 웹

Windows/macOS에서 더블클릭으로 접속하려면 [Windows 실행 프로그램](cfd_bot/web_static/downloads/CFD-Control-Room-Windows.zip) 또는 [macOS 앱](cfd_bot/web_static/downloads/CFD-Control-Room-macOS.zip)을 압축 해제합니다. Windows는 `Start CFD.cmd`, macOS는 `CFD Control Room.app`을 엽니다. 기존 `~/.ssh/config`의 Host 목록에서 번호를 선택하면 SSH 터널과 브라우저를 엽니다. 기존 SSH User·Port·IdentityFile·ProxyJump를 그대로 사용하며 별도 연결 설정은 저장하지 않습니다. 연결 창은 사용 중 최소화해 둡니다. [OS별 사용법](clients/README.md)을 참조합니다. 서버의 웹 서비스는 아래처럼 설치합니다.

```bash
cd /home/joon/telegram/cfd_bot
python3 -m cfd_bot --config bot.json web --port 8766
```

브라우저에서 [http://localhost:8766](http://localhost:8766)를 엽니다. 별도 npm 설치나 frontend build는 필요 없습니다. `bin/cfd-ticket-web` 또는 패키지 설치 후 `cfd-ticket-web`도 같은 웹 서버를 실행합니다.

| 화면 | 기능 |
|---|---|
| 대시보드 | 매 조회마다 fresh ofps, 외부 실행 포함, 진행률·ETA·실제 CPU·최근 실행 이력 |
| 티켓 관리 | 개별/macro/child 폼, 검증·저장·복제·일괄 삭제, 파일 선택, 패턴 템플릿, 전후처리 |
| 매크로 편집 | Allrun 하위 케이스 검색, postProcessing 강조, 제외·순서 변경, 공통 CPU 개수와 자동 배정 |
| 작업 큐 | 대기/실행 상태, 새 계산 자동 시작 정지·재개, 대기 작업 취소, 실패 사유 |
| 결과 · 요청 데이터 | 등록 케이스 상세, Residual PNG·이미지 미리보기, 선언된 파일 다운로드 |

웹·GUI·Telegram은 같은 `TicketService`/`TicketRunner`, `tickets/*.json`, `ticket-patterns.json`, SQLite를 사용합니다. 각 편집기의 미저장 초안은 독립적이며, 다른 편집기가 먼저 저장하면 버전 충돌을 표시합니다. 실행 중에는 실행 버튼을 비활성화하지만 요청 데이터 경로는 수정할 수 있습니다. 입력 중인 폼은 10초 주기 현황 갱신으로 덮어쓰지 않습니다.

새 매크로의 **저장 및 큐 등록**은 child 티켓과 submission flag를 만듭니다. 개별 티켓은 저장 후 **실행**으로 등록합니다. 변경 중이면 **저장 후 실행**에서 한 번에 처리합니다. 기존 봇의 다음 Monitor 주기에 순차 등록·CPU 배정·실행·알림이 진행됩니다. 웹 서버 자체는 Scheduler나 Telegram 전송 worker를 시작하지 않으므로, 실행·감시·알림에는 기존 `cfd-bot.service`가 필요합니다. Telegram 메시지 정리(`/clean`)는 Telegram에서 수행합니다.

웹에서 티켓을 선택할 때는 Monitor가 저장한 최근 실행 상태를 사용하므로 `ofps` 전체 스캔을 기다리지 않습니다. 실제 실행 요청 시에는 fresh 상태를 다시 검사해 이미 실행 중인 케이스를 차단합니다.

상시 실행은 다음 user service를 설치합니다. 웹 서비스에는 Telegram token 환경 파일을 주입하지 않습니다.

```bash
cp deploy/cfd-bot-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cfd-bot-web.service
systemctl --user status cfd-bot-web.service
journalctl --user -u cfd-bot-web.service -n 50
```

`127.0.0.1`에만 바인딩하며 Host/Origin/CSRF를 검증합니다. 요청 데이터는 현재 티켓에 선언된 case 내부 파일만 제공하고, 49 MiB를 초과하는 파일과 경로 이탈은 거부합니다. 웹 요청 경로와 결과는 서비스 journal 및 `state/web.log`에 기록하며 입력 본문·토큰은 기록하지 않습니다. 파일 로그는 5 MiB마다 순환하며 이전 로그 3개를 유지합니다.

### 외부 PC에서 SSH로 사용

브라우저가 있는 **외부 PC**에서 터널을 엽니다. Tailscale을 포함해 기존 SSH 연결이 가능한 주소를 그대로 사용합니다.

```bash
ssh -N -T -o ExitOnForwardFailure=yes -L 127.0.0.1:8766:127.0.0.1:8766 joon@서버주소
```

그 PC에서 `http://localhost:8766`를 엽니다. 터널을 여는 터미널은 유지해야 합니다. 이미 SSH 세션이 열려 있어도 웹 포트를 전달하려면 이 터널이나 SSH 클라이언트의 Local forwarding 설정을 추가해야 합니다. 서버의 웹 서비스는 계속 실행 중이어야 하며 SSH 서버에서 TCP forwarding이 허용되어야 합니다.

로컬 8766 포트가 사용 중이면 `-L 127.0.0.1:18766:127.0.0.1:8766`로 연결하고 `http://localhost:18766`를 사용합니다. 브라우저 주소의 포트가 달라도 Host/Origin/CSRF 검증이 유지됩니다. VS Code Remote SSH의 Ports 메뉴에서도 서버 포트 8766를 전달할 수 있습니다.

동일 동작의 보조 스크립트는 [bin/cfd-web-tunnel](bin/cfd-web-tunnel)입니다. 외부 PC에 복사한 뒤 실행합니다.

```bash
sh cfd-web-tunnel joon@서버주소 18766 8766
# SSH 포트도 별도 지정하는 경우
sh cfd-web-tunnel joon@서버주소 18766 8766 -p 2222
```

## 실시간 현황과 자동 알림

`/stat`은 요청할 때마다 `ofps`를 새로 실행합니다. SQLite의 과거 상태로 현재 목록을 만들지 않으며, 티켓이 없는 계산도 표시합니다.

```text
케이스 이름 | 소유대상 | 코어수/코어위치 | 한국 시작시각/경과시간
```

백그라운드 Monitor도 같은 `ofps` 결과의 모든 CASE를 감시합니다. SSH, tmux, systemd, `nohup`, 외부 스크립트 또는 봇 큐에서 시작한 계산을 구분하지 않고 시작·완료·실패를 알립니다.

정상 종료는 다음 조건을 모두 만족해야 합니다.

1. `system/controlDict`의 `stopAt`이 `endTime`입니다.
2. 이번 실행에서 갱신된 로그의 최종 `Time`이 `controlDict.endTime` 이상입니다.
3. non-zero 종료 코드, fatal pattern 또는 갱신된 실패 표식이 없습니다.

로그의 `End` 문자열만으로는 정상 종료로 판정하지 않습니다. 완료 알림에는 실행 요약과 최신 Residual PNG 한 개만 첨부합니다.

## 티켓 생성과 편집

케이스별 설정은 계산 디렉터리가 아니라 `tickets/*.json`에서 관리합니다. 티켓이 없어도 자동 감시는 동작합니다. 티켓에는 표시 이름, 로그 순서, 실패 pattern·표식, Residual, `/data` 파일, 알림, 큐와 전·후처리를 지정합니다.

GUI·Telegram·웹은 같은 `TicketService`의 검증·저장·복제·삭제 로직을 사용합니다.

```bash
cfd-ticket-gui
# 또는
python3 -m cfd_bot --config bot.json gui
```

Telegram에서는 `/tickets`로 같은 티켓을 편집합니다. 개별 티켓과 매크로 티켓을 만들 수 있으며, 매크로는 선택한 하위 케이스를 child 티켓으로 관리합니다.

개별 티켓도 실행 설정을 지원합니다. Telegram은 **실행 · 작업 큐 설정**, GUI는 **작업 큐 → 실행 설정 방식**, 웹은 **실행 설정**에서 **티켓에서 지정**을 선택하고 코어 수(NP)와 실행 명령을 입력하세요. 기본 CPU 자동 배정은 소켓을 넘어 가용한 물리 코어를 사용하며 다른 계산과 겹치지 않습니다. 고급 설정에서 수동 범위도 지정할 수 있습니다.

**케이스 설정 사용**은 기존 Allrun / config/*Run의 NP와 실행 설정을 유지합니다. **티켓에서 지정**은 `resource_source: "ticket"`으로 저장되며 케이스의 기존 NP보다 우선 적용됩니다. 매크로는 공통 설정, child는 부모 매크로 설정을 상속합니다. 계산 중에는 실행 설정 변경이 제한되고 요청 데이터·감시 설정은 계속 편집할 수 있습니다. 세 화면의 기능은 함께 변경·검증합니다([AGENTS.md](AGENTS.md)).

예제: [기존 케이스 설정 사용](examples/ofps.json), [개별 티켓 실행 설정](examples/alone-case.json), [매크로 티켓](examples/macro-batch.json), [매크로 child](examples/child-case-a.json)

`case_dir`만 절대 경로입니다. 로그, Residual, export와 실패 표식은 케이스 기준 상대 경로로 저장하며 케이스 밖으로 나가는 경로는 거부합니다.

## Residual과 요청 데이터

`/data`에서 등록 케이스를 선택해 상세 상태, Residual 또는 export 파일을 요청합니다.

- Residual은 `residual_pattern`과 일치하는 최신 PNG 한 개를 보냅니다.
- export는 티켓의 `pattern`, `kind`, `max_files`에 따라 기존 이미지·CSV·로그를 보냅니다.
- 봇은 그래프나 contour를 새로 만들지 않고 기존 후처리 결과를 사용합니다.
- 등록된 export 전체를 계산 종료 때 자동 전송하지 않습니다.

후처리 스크립트가 결과를 만든 뒤 `/data`로 요청할 수 있도록 티켓에 상대 경로 pattern을 등록합니다.

## FIFO 계산 큐

`/queue`에서 대기·실행 상태를 확인하고 큐를 일시 정지, 재개하거나 대기 작업을 취소합니다. Scheduler는 실행 전에 다음을 검사합니다.

- 같은 케이스의 외부 계산 또는 기존 active job
- 다른 작업과의 CPU 중복
- CPU socket과 NUMA topology
- OpenFOAM과 MPI 실행 환경
- `ofps --check` 결과

자동 CPU 배정은 사용 가능한 물리 코어를 선택합니다. 고정 CPU가 필요한 티켓만 수동 범위를 사용합니다. 매크로 child는 저장된 순서대로 실행하며 한 child의 실패도 기록한 뒤 다음 child로 진행합니다.

실행 순서는 `전처리 → solver → 종료 판정 → 후처리 → 완료 알림`입니다. Solver 명령은 실제 종료 코드를 받을 수 있도록 foreground에서 기다려야 합니다.

## Telegram 명령과 메시지 정리

| 명령 | 기능 |
|---|---|
| `/stat` | 현재 실행 중인 모든 계산을 한 줄씩 표시 |
| `/data` | 상세 상태, Residual과 등록 데이터 요청 |
| `/tickets` | 티켓 생성·편집·복제·삭제와 실행 요청 |
| `/queue` | FIFO 큐 조회·정지·재개·취소 |
| `/clean` | 현재 대화에서 봇이 추적한 메시지 일괄 삭제 |
| `/cancel` | 진행 중인 Telegram 입력 취소 |
| `/start`, `/help` | 명령 안내 |

`/clean`은 Telegram `deleteMessages`를 최대 100개씩 사용합니다. Telegram 제한상 봇이 추적하기 전에 생성된 메시지, 삭제 가능 시간이 지난 메시지와 권한 밖의 메시지는 남을 수 있습니다.

화면 문구와 버튼은 `telegram-ui/`의 메뉴·시나리오 JSON에서 관리합니다. JSON을 바꾸면 `manifest.json`을 검증한 뒤 서비스를 재시작합니다. 이전 `text.json`은 호환용이며 새 문구의 원천으로 사용하지 않습니다.

## `ofps`와 운영 CLI

`~/.local/bin/ofps`는 자체 완결형 [bin/ofps](bin/ofps)를 가리킵니다. 별도 legacy scanner 없이 프로세스 scan과 bot 확장 명령을 모두 제공합니다.

```bash
ofps                         # 현재 프로세스 scan
ofps --watch 2               # 2초마다 갱신
ofps --check 0-7             # CPU 사용 가능 여부
ofps --status                # bot 형식 상태
ofps --json                  # JSON 상태
ofps --queue                 # FIFO 큐
ofps --enqueue tickets/my-case.json
```

전체 운영 명령은 `python3 -m cfd_bot --help`에서 확인합니다. 큐 실행 환경은 `python3 -m cfd_bot --config bot.json check --execution-env --mpi-probe`로 실제 계산 없이 검사합니다.

## 주요 설정과 파일

| 경로 | 역할 |
|---|---|
| `bot.json` | state, `ofps`, Telegram allowlist, poll 주기, scheduler 설정 |
| `tickets/*.json` | 케이스별 감시·데이터·실행 규칙 |
| `telegram-ui/**/*.json` | Telegram 문구, 버튼과 시나리오 template |
| `ticket-patterns.json` | GUI·Telegram·웹이 공유하는 실패 pattern template |
| `cfd_bot/web_static/` | 외부 의존성 없는 웹 HTML·CSS·JavaScript |
| `deploy/cfd-bot-web.service` | localhost 웹 UI 전용 user service |
| `clients/` | Windows/macOS SSH 실행 프로그램 소스 및 ZIP 생성기 |
| `state/web.log` | 웹 요청 method/path/status 및 서비스 오류 로그 |
| `state/state.sqlite3` | snapshot, queue, observed run, outbox와 편집 session |
| `deploy/openfoam-env.sh` | systemd worker의 OpenFOAM·MPI 환경 |

기본 설정 예시는 [examples/bot.json](examples/bot.json)을 참조합니다. `bot.json`이나 `telegram-ui/`를 변경하면 서비스를 재시작합니다. 티켓 변경은 다음 Monitor 주기부터 반영됩니다.

## 설계와 검증

- [HLD](docs/HLD.md): 시스템 경계와 주요 설계 원칙
- [LLD](docs/LLD.md): 모듈, DB와 상태 전이
- [Architecture](docs/ARCHITECTURE.md): 전체 유즈케이스와 flow chart
- [전체 기능 Flow Chart](docs/all-feature-flows.svg)
- [설계 변경 이력](docs/history/README.md)

```bash
python3 -m compileall -q cfd_bot tests
python3 -m unittest discover -s tests -q
python3 -m cfd_bot --config bot.json check
```

테스트는 실제 Telegram 메시지나 OpenFOAM 계산을 시작하지 않습니다.

웹 HTTP 테스트에는 loopback 소켓 권한이 필요합니다. 선택적인 브라우저 통합 검증은 한 터미널에서 `python3 -m tests.web_fixture`, 다른 터미널에서 `PLAYWRIGHT_MODULE=/path/to/playwright-core node tests/web_browser.cjs`로 실행합니다. 테스트용 Chromium과 Playwright는 검증 도구일 뿐 앱 실행 의존성은 아닙니다.
