# ofps + Telegram CFD bot

OpenFOAM과 Basilisk 계산의 상태, 알림, 실행 큐와 결과 데이터를 Telegram·GUI·웹에서 관리합니다.

## 기능

| 기능 | 제공 내용 |
|---|---|
| 실시간 현황 | 실행 중인 계산의 이름, 실행 주체, 사용 코어, 시작 시각과 경과 시간 |
| 자동 알림 | 계산 시작, 정상 종료, 실패, 실행 요약과 최신 Residual 이미지 |
| 결과 데이터 | Residual, contour, 이미지, CSV와 로그 조회·다운로드 |
| 계산 큐 | 순차 실행, 일시 정지·재개, 다중 취소, CPU 자동 배정과 예상 대기 시간 |
| 매크로 계산 | 하위 케이스 일괄 등록, 순차 실행, 진행률과 예상 남은 시간 |
| 티켓 관리 | 케이스·매크로 생성, 감시·데이터·실행 설정, 검증·복제·삭제 |
| 외부 계산 감시 | SSH, tmux, systemd, 백그라운드와 외부 스크립트로 시작한 계산 감시 |

티켓이 없는 계산도 현황과 알림에 포함됩니다. 케이스별 설정은 `tickets/*.json`에서 관리하며 예시는 [examples](examples)를 참고하세요.

## 사용자 화면

| 화면 | 용도 |
|---|---|
| Telegram | 상태 조회, 알림, 데이터 요청, 큐와 티켓 관리 |
| `cfd-ticket-gui` | 서버 데스크톱에서 티켓과 작업 큐 관리 |
| localhost 웹 | 대시보드, 티켓 편집, 큐와 결과 데이터 관리 |

## Telegram 명령

| 명령 | 기능 |
|---|---|
| `/stat` | 현재 실행 중인 계산 조회 |
| `/data` | 상세 현황과 결과 데이터 요청 |
| `/tickets` | 티켓 관리와 실행 요청 |
| `/queue` | 계산 큐 조회·정지·재개·취소 |
| `/clean` | 봇 대화 메시지 일괄 삭제 |
| `/cancel` | 진행 중인 입력 취소 |
| `/start`, `/help` | 명령 안내 |

## 설치

Linux와 Python 3.9 이상이 필요합니다. 계산 실행에는 OpenFOAM과 MPI 환경이 필요합니다.

`ofps` 소스는 `bin/ofps`입니다. 별도 빌드 없이 symlink로 설치합니다.

```bash
cd /home/joon/telegram/cfd_bot
chmod +x bin/ofps
mkdir -p ~/.local/bin
ln -sfn "$PWD/bin/ofps" ~/.local/bin/ofps
ln -sfn "$PWD/bin/cfd-ticket-gui" ~/.local/bin/cfd-ticket-gui
ofps --help
```

Telegram 설정과 서비스 실행:

```bash
bin/set-telegram-token
python3 -m cfd_bot --config bot.json identify
python3 -m cfd_bot --config bot.json check

mkdir -p ~/.config/systemd/user
cp deploy/cfd-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cfd-bot.service
```

`identify` 전에 Telegram에서 봇에 `/start`를 보내고, 확인된 `user_id`와 `chat_id`를 `bot.json`에 등록하세요.

## GUI와 웹

```bash
cfd-ticket-gui

cp deploy/cfd-bot-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cfd-bot-web.service
```

웹 주소는 [http://localhost:8766](http://localhost:8766)입니다. 외부 PC 연결과 Windows/macOS 실행 방법은 [clients/README.md](clients/README.md)를 참고하세요.

상세 설계와 개발 문서는 [docs](docs)에서 확인할 수 있습니다.
