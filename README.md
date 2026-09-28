# ofps + Telegram CFD bot

기존 `ofps`의 OpenFOAM/Basilisk 프로세스 조회를 재사용하는 Linux용 봇입니다. Python 3.9 이상에서 동작하며 표준 라이브러리만 사용합니다. Residual은 케이스에서 생성한 PNG를 지정된 경로에서 읽어 전송합니다.

설계 문서는 역할별로 나뉩니다.

- [HLD](docs/HLD.md): 시스템 경계, 주요 설계 원칙, 배포와 신뢰성
- [LLD](docs/LLD.md): 모듈, 상태 전이, DB와 함수 수준 흐름
- [Architecture](docs/ARCHITECTURE.md): 전체 구성과 유즈케이스별 flow/sequence chart

![ofps Telegram CFD bot 아키텍처와 주요 흐름](docs/ofps-telegram-architecture.svg)

- `/stat`: `ofps` 즉시 scan으로 현재 실행 중인 계산을 케이스마다 한 줄로 표시하고 상세 버튼 제공
- `/clean`: 봇이 추적한 현재 대화의 사용자·봇 메시지 삭제
- `tickets/*.json`: 실제 계산 디렉터리 밖에서 케이스별 감시·판정·데이터 규칙 관리
- `telegram-ui/`: Telegram에 표시하는 문구·버튼·명령 목록을 메뉴와 시나리오별 JSON으로 관리
- FIFO 큐: CPU 중복과 토폴로지를 검사한 뒤 계산 실행
- 외부 계산 감시: 실행 경로와 티켓 등록 여부에 관계없이 기존 `ofps`가 찾은 모든 케이스를 자동 추적하고, `controlDict`의 `endTime`과 로그의 최종 `Time`으로 종료 판정
- `/data`: 케이스별로 미리 등록한 이미지·CSV·로그 파일을 요청 시 전송
- 코어·CPU는 케이스 경로와 일치하는 ofps 관측값으로 표시하고, 예상 시간 미설정 시 controlDict 종료값과 최근 로그 속도로 추정

## 1. 계산 티켓

감시 설정은 실제 케이스 안에 두지 않고 봇의 `tickets/` 디렉터리에서 관리합니다. `cfd-ticket-gui`의 **새 티켓**에서 케이스 경로, 표시 이름, 로그 파일을 입력합니다. 저장 형식의 예시는 [examples/ofps.json](examples/ofps.json)입니다.

티켓은 자동 알림의 전제 조건이 아닙니다. Monitor는 매 주기 `ofps`가 반환한 모든 `CASE:` 경로를 감시합니다. 티켓이 없는 외부 계산도 처음 발견하면 시작 알림을 보내고, 프로세스가 사라지면 케이스 최상위의 최신 `log*` 파일과 `system/controlDict`를 읽어 정상 종료 또는 실패를 알립니다. 티켓을 등록하면 표시 이름, 정확한 다단계 로그 순서, 케이스별 실패 패턴, Residual 경로와 `/data` 항목을 추가로 적용합니다.

```json
{
  "version": 1,
  "case_dir": "/home/user/OpenFOAM/run/my-case",
  "name": "my-case",
  "residual_pattern": "plots/residual*.png",
  "watcher": {
    "logs": [
      "log.solver"
    ],
    "failure": {
      "patterns": [
        "failed stage:"
      ],
      "include_openfoam_defaults": true,
      "updated_files": []
    }
  },
  "notifications": {
    "events": [
      "started",
      "succeeded",
      "failed",
      "interrupted"
    ]
  },
  "exports": [
    {
      "name": "contour",
      "pattern": "postProcessing/telegram/contour*.png",
      "kind": "photo",
      "max_files": 2,
      "on": [],
      "on_complete": false
    }
  ]
}
```

`case_dir`만 절대 경로이며 로그, 실패 표식, export와 후처리 결과의 상대 경로는 모두 실제 케이스 디렉터리를 기준으로 해석됩니다.

| 키 | 의미 |
|---|---|
| `case_dir` | 감시하거나 실행할 실제 OpenFOAM 케이스 디렉터리 |
| `name` | Telegram에 표시할 이름 |
| `residual_pattern` | 케이스 기준 Residual PNG 상대 경로/패턴. 최신 이미지 한 개 전송, 빈칸이면 사용하지 않음 |
| `cores`, `cpu_set` | 기존 큐 실행 설정과의 호환용. GUI 입력 대상이 아니며 상태 표시는 ofps 관측값 사용 |
| `command` | 실행 파일과 인수 배열. 생략하면 조회 전용 |
| `expected_seconds` | 예상 총 경과시간(초, 1 이상). 값이 있으면 우선 사용하고, 생략·`null`·빈 문자열이면 자동 예측 |
| `watcher` | 진행 로그 선택, 실패 증거와 프로세스 소멸을 판정하는 규칙 |
| `notifications.events` | 알릴 `started`, `succeeded`, `failed`, `interrupted` 사건 |
| `exports` | `/data` 요청으로 받을 파일 경로·형식·개수 |
| `preprocess` | 봇 큐 계산 전에 실행할 명령 배열. 새 티켓 기본값 `./Allclean` |
| `postprocess` | 봇 큐 계산이 성공한 뒤 실행할 명령 배열. 새 티켓 기본값 `./Allpost` |

Residual 버튼은 `residual_pattern`에서 수정 시각이 가장 최근인 PNG 한 개를 보냅니다. 종료 알림에는 이번 실행에서 갱신된 PNG 한 개를 보관해 첨부합니다. 경로가 비어 있거나 일치하는 파일이 없으면 이미지를 첨부하지 않습니다. 봇에서 Residual 그래프나 CSV를 생성하지 않습니다. `exports[].on`을 빈 배열로 두면 해당 결과는 `/data` 요청으로만 전송합니다.

### 티켓 GUI

다음 명령은 `tickets/*.json` 전용 편집기를 엽니다.

```bash
cd /home/joon/telegram/cfd_bot
python3 -m cfd_bot gui
```

작업 디렉터리와 관계없이 설치된 실행 명령을 사용할 수도 있습니다.

```bash
cfd-ticket-gui
```

GUI는 JSON을 직접 편집할 필요 없이 항목별 입력칸으로 티켓을 만들고 수정합니다. 새 파일은 `macro-`, `child-`, `alone-` 접두어로 저장합니다. 기존 파일명도 읽을 수 있고 GUI 저장 시 접두어가 적용됩니다. 새 티켓은 빈 **Case directory** 입력칸에서 시작하며, 경로를 직접 입력하거나 **폴더 선택**으로 지정할 수 있습니다. 표시 이름과 티켓 파일명은 폴더 이름으로 자동 제안됩니다.

GUI와 Telegram의 케이스 폴더 선택은 처음에 `~/OpenFOAM/<사용자>-dev/run`에서 시작합니다. 현재 계정에서는 `/home/joon/OpenFOAM/joon-dev/run`입니다. 이미 케이스를 지정했다면 그 폴더에서 시작합니다.

기존 티켓을 선택하고 **선택 복제**를 누르면 설정을 복사한 새 티켓을 편집합니다. 표시 이름에는 `복사본`, 파일명에는 `-copy`가 붙으며 같은 파일명이 있으면 번호를 붙입니다. **Case directory**를 새 케이스로 변경하고 **저장**하면 별도 파일로 등록됩니다. 같은 케이스 경로의 중복 등록은 저장 전에 확인합니다.

티켓 목록에서 **Ctrl / Shift**로 여러 파일을 선택한 뒤 **선택 삭제**로 한 번에 삭제합니다. 확인 화면에 소속 child를 포함한 삭제 대상을 표시합니다. 매크로를 삭제하면 소속 child도 함께 삭제하며, child만 삭제하려면 먼저 부모 매크로를 함께 선택해야 합니다. 선택한 항목 중 대기·실행 중인 작업이나 편집 충돌이 있으면 삭제 전에 전체 요청을 중단합니다. 삭제 대상은 티켓 JSON이며 케이스 폴더·계산 결과는 유지합니다.

- **기본 설정**: 필수 Case directory, 표시 이름, 로그 파일, Residual PNG 경로, End Time / Iteration, 알림 체크박스
- **종료·실패 판정**: `controlDict` 기반 정상 종료 기준 안내, 추가 실패 로그 패턴, OpenFOAM 기본 오류 감지, 실패 표식 파일
- **요청 데이터**: `/data → 케이스 → 요청 이름`으로 받을 파일의 상대 경로/패턴, 전송 형식, 최대 개수
- **작업 큐**: alone/child 소속, 상대 매크로 경로, 매크로 공통 실행 설정과 선택할 하위 케이스 목록
- **전·후처리**: 계산 전·후 실행할 스크립트 명령 입력. 새 티켓 기본값은 전처리 `./Allclean`, 후처리 `./Allpost`

**전·후처리**는 GUI 입력칸 또는 Telegram의 같은 이름 버튼으로 수정합니다. 한 줄에 한 명령을 입력하며 인수가 있으면 `./Allpost --format png`처럼 지정합니다. 공백이 있는 경로·인수는 따옴표로 감쌉니다. 빈칸이면 해당 단계를 생략하고 **기본값** 버튼으로 되돌릴 수 있습니다. 매크로에서는 각 child의 케이스 폴더에서 같은 명령을 실행합니다.

```json
"preprocess": [{"command": ["./Allclean"]}],
"postprocess": [{"command": ["./Allpost"]}]
```

실행 순서는 **전처리 → 계산 → 정상 종료 후 후처리 → 결과 파일 보관·완료 알림 → 다음 작업**입니다. 지정한 전처리 스크립트가 없거나 실패·시간 초과이면 계산을 시작하지 않습니다. 지정한 후처리가 실패하면 계산 성공 여부는 유지하고 후처리 오류를 별도로 알립니다. 두 단계 모두 OpenFOAM 실행 환경과 예약한 CPU·NP 설정을 사용하며, 출력은 작업별 `preprocess.log`, `postprocess.log`에 기록합니다. JSON의 각 명령에 `timeout_seconds`를 지정할 수 있으며 기본 제한은 120초입니다. 기존 제한값은 입력칸에서 명령을 수정해도 보존됩니다.

`Allclean`은 기존 계산 결과를 정리하므로 이어서 계산할 케이스는 전처리를 비웁니다. 기존 티켓에서 생략된 전·후처리는 계속 비활성으로 유지하며 **기본값** 버튼으로 지정할 수 있습니다. 실행 중인 티켓의 전·후처리 명령 변경은 제한합니다.

매크로 공통 NP는 전처리 전에 케이스 설정에 반영됩니다. 따라서 케이스의 `Allclean`은 현재 NP뿐 아니라 이전 `processorsN`의 계산 결과도 처리해야 합니다. `akita_ext`에서 NP를 24→48로 바꿀 때 예전 `processors24/100` 등이 남아 초기장 준비 단계가 중단되던 문제는 공통 정리 스크립트에서 모든 병렬 디렉터리를 검사하도록 수정했습니다. 이 파이프라인의 감시 로그는 실제 출력인 `log.foamRun.preFlow`를 사용합니다. `Allclean --check`로 삭제 없이 정리 범위를 확인할 수 있습니다.

솔버/실행 스크립트가 비정상 종료하면 종료 코드와 함께 감지된 오류 문구를 실패 사유에 기록합니다. 기본 오류 감지는 줄 처음의 `ERROR:`·`FAILED:`도 포함하므로 초기 준비 단계의 실패 원인도 작업 상세와 Telegram 종료 알림에서 확인할 수 있습니다.

**로그 파일**은 입력칸에 한 줄씩 입력하거나 **파일 선택**에서 여러 파일을 선택할 수 있습니다. 선택한 파일은 케이스 기준 상대 경로로 입력되며, 선택을 취소하면 기존 입력을 유지합니다.

**Residual PNG 경로**에 `residual*.png` 또는 `plots/residual*.png`처럼 케이스 기준 상대 경로를 입력합니다. **파일 선택**으로 특정 PNG를 고를 수도 있습니다. 케이스에서 이미지를 생성·갱신하고 봇은 최신 파일을 읽어 보냅니다. 이 경로는 `log.solver`와 별개이며, 파일이 없으면 미생성 상태를 안내합니다.

개별 작업에는 코어 수·CPU 범위·실행 명령 입력을 추가하지 않습니다. 매크로에서는 작업 큐 탭에 공통 NP와 실행 명령을 지정하며 CPU는 기본적으로 자동 배정합니다. 고정 CPU가 필요한 경우에만 **고급: CPU 직접 지정**을 선택합니다. Telegram 상태의 코어와 CPU 범위는 ofps 실제 관측값입니다. End Time / Iteration 입력은 기본적으로 각 케이스의 controlDict endTime을 사용하며, 직접 지정하면 봇의 완료 판정·자동 ETA 기준에 적용합니다. 이 입력으로 controlDict 자체를 수정하지 않습니다.

`ofps.json` 또는 티켓의 `expected_seconds`에 값이 있으면 `설정 시간 − 실제 경과시간`으로 잔여시간을 계산합니다. 설정 시간을 넘으면 초과 상태를 표시합니다. GUI는 이 값을 입력받지 않지만 기존 설정값은 저장 시 보존합니다.

`expected_seconds`가 없거나 `null`, 빈 문자열(공백 포함)이면 자동 예측합니다. 케이스의 `system/controlDict`에서 `endTime`을 읽고, 로그의 최근 최대 20개 유효 구간에서 `Time` 증가량 대비 `ClockTime` 증가량을 계산합니다.

`예상 잔여시간 = (endTime − 최근 완료 Time) × (최근 ClockTime 증가량 ÷ 최근 Time 증가량)`

정상상태 계산에서는 Time이 반복 횟수에 해당하며, 비정상 계산에서는 시뮬레이션 시간 기준으로 같은 식을 사용합니다. 초기 준비 시간 대신 최근 구간의 속도를 반영하고, 재시작·로그 교체·단계 전환·ClockTime 초기화 때 표본을 분리합니다. 계산 중 `controlDict`의 종료값이 바뀌면 다음 조회와 종료 판정에 반영합니다. 일반 숫자와 케이스 내부 `#include` 및 단순 변수 참조를 읽으며 `#calc`·`#codeStream`은 실행하지 않습니다. 프로세스가 살아 있는 동안 종료값에 도달하면 종료 확인 대기로 표시합니다.

자동 예측 중 종료값이나 최근 시간 표본이 부족한 경우에만 같은 케이스·로그 목록·실행 명령의 최근 정상 종료 최대 5회 소요 시간 중앙값을 보조 추정으로 사용합니다. 현재 코어 수를 알면 같은 코어 수의 이력만 사용하며, 어느 근거를 사용했는지 상세 현황에 표시합니다. 두 근거 모두 없으면 미정입니다. end_time이 지정되면 controlDict endTime 대신 사용합니다.

**현재 패턴 저장**으로 추가 실패 패턴과 기본 오류 감지 옵션에 이름을 붙여 저장하고, 다른 티켓에서 **불러오기**로 적용할 수 있습니다. `OpenFOAM 기본` 템플릿을 제공하며 사용자 템플릿은 `tickets/` 옆의 `ticket-patterns.json`에 저장됩니다. 케이스 경로와 실패 표식 파일은 템플릿에 복사하지 않습니다.

**요청 데이터**는 이미 생성된 파일을 요청할 때 읽어 보내는 설정입니다. 예를 들어 `contour` / `postProcessing/contour*.png` / `photo` / `1`을 등록하면 Telegram의 contour 버튼으로 가장 최근 이미지를 받습니다. GUI에서 저장한 요청 데이터는 자동 첨부하지 않습니다. 기존 티켓을 GUI로 저장할 때도 요청 전송 방식으로 바뀝니다.

계산 중에도 GUI와 Telegram에서 요청 데이터의 경로·형식·개수를 수정하거나 항목을 추가·삭제할 수 있습니다. 매크로에서 저장하면 실행 중인 child를 포함한 모든 소속 티켓에 반영되고, 다음 `/data` 요청부터 새 설정을 사용합니다. 작업 ID·큐 순서·실행 상태는 유지하며 재접수하지 않습니다. 실행 중인 매크로의 공통 CPU·코어 수·실행 명령 변경은 계속 제한합니다.

기존 개별 티켓의 실행 명령·CPU 예약·후처리 및 expected_seconds는 보존합니다. 구형 simulation/progress의 시작·목표값은 제거하며, 새 종료값 설정은 end_time으로 저장합니다. **검증**과 **저장**은 경로 존재 여부, 정규식, 케이스 밖으로 나가는 상대 경로를 검사합니다. `Ctrl+S`로도 저장할 수 있습니다. Python의 `tkinter`를 사용합니다.

### 매크로 작업 큐

1. GUI의 **작업 종류 → 매크로 작업**을 선택하고 상위 **Case directory**를 지정합니다.
2. **작업 큐** 탭에서 공통 **코어 수(NP)**와 **실행 명령**을 입력합니다. 예: `24`, `./Allrun`. **CPU 배정 → 자동 배정 (권장)**을 사용하면 CPU 번호를 입력하지 않습니다.
3. **하위 케이스 검색**을 누릅니다. `Allrun`이 있는 하위 폴더를 경로순으로 검색하고 `*-template`, ofps 실행 중 케이스를 제외합니다. 심볼릭 링크 디렉터리는 따라가지 않습니다.
4. `postProcessing` 유무와 완료 여부에 관계없이 검색 대상에 포함합니다. `postProcessing`이 있는 행은 GUI에서 노란색으로, Telegram에서는 🟨 표시로 구분합니다. 체크포인트·후처리의 최신 CFD 시간과 목표를 비교해 미완료·종료값 도달 여부를 안내하며, 종료값을 읽을 수 없는 케이스도 목록에 남깁니다. 분산 체크포인트는 모든 processor의 공통 진행 시점을 사용하며, 후처리 시작 폴더 `0`뿐 아니라 `.dat`/`.csv`의 마지막 시간 표본도 확인합니다.
5. 제외할 케이스는 **행 오른쪽 삭제**를 누릅니다. 공통 로그·Residual PNG·실패 규칙·요청 데이터는 child 기준 상대 경로로 지정합니다.
6. **저장**하면 `macro-*.json`과 모든 `child-*.json`을 생성하고 봇에 큐 접수 플래그를 전달합니다. 개별 `alone-*.json`은 **실행**을 눌렀을 때 큐에 접수됩니다. 일반 저장은 감시 설정만 저장합니다.

매크로 예시는 [macro-batch.json](examples/macro-batch.json), child 예시는 [child-case-a.json](examples/child-case-a.json)입니다.

| 필드 | 의미 |
|---|---|
| `task_type` | `macro` 또는 `single` |
| `role` | 개별 작업의 `alone` 또는 `child` |
| `macro_ticket` | child에서 매크로 JSON까지의 상대 경로 |
| `end_time` | 지정 종료 Time/Iteration. `null`이면 케이스 controlDict 기본값 |
| `resource_source` | 매크로 공통 실행 설정은 `macro`; 개별 케이스 설정은 `case` |
| `cpu_policy` | `auto`: 실행 직전 전체 소켓의 빈 물리 코어를 자동 배정. `manual` 또는 생략: 기존 고정 CPU 설정 사용 |
| `cases` | 매크로가 실행할 절대 `case_dir`, 상대 `ticket`, 상태의 순서 있는 배열 |
| `queue.state` | `waiting` = 계산대기, `running` = 계산중, `finished` = 계산종료 |
| `queue.result` | 종료 상세 결과: `succeeded`, `failed`, `interrupted`, `cancelled` |
| `queue.submit` | GUI에서 봇으로 보내는 접수 플래그. 접수 후 봇이 `false`로 변경 |
| `queue.request_id` | 재시작·재조회 시 중복 접수를 막는 고유 ID |

봇은 기존 SQLite 큐에 매크로 전체를 한 트랜잭션으로 접수합니다. 일부 child가 다른 큐에 있으면 전체 접수를 대기하며 Telegram으로 이유를 알립니다. 같은 매크로는 `max_parallel` 값에 관계없이 한 번에 한 child만 실행합니다. 실패한 child의 결과를 기록하고 다음 child로 진행하며, 자동 재시도하지 않습니다. 생성된 매크로를 다시 저장해도 재접수하지 않습니다.

저장된 매크로·개별 티켓에는 **실행** 버튼을 제공합니다. ofps에서 해당 케이스가 조회되거나 worker가 시작·계산·후처리 중이면 실행을 비활성화합니다. 매크로는 child 중 하나라도 실행 중이면 비활성화합니다. GUI는 상태를 주기적으로 갱신하고 Telegram은 티켓을 열거나 **실행 상태 새로고침**을 누를 때 반영합니다. 버튼을 누른 순간에도 ofps와 큐 상태를 다시 확인하므로 이전 화면의 실행 버튼으로 중복 실행할 수 없습니다.

실행 중이 아니면 **실행**으로 새 큐 요청을 만들 수 있습니다. 매크로 재실행은 저장된 모든 child를 지정한 순서대로 새 요청에 등록합니다. 이미 대기 중인 작업에는 기존 큐를 유지하며 중복 등록하지 않습니다. 종료·실패한 티켓의 재실행은 **저장** 대신 **실행**을 사용합니다. GUI에서 미저장 변경이 있으면 저장 후 실행합니다. Telegram에서 변경한 티켓의 **저장 후 실행**을 누르면 설정 확인 화면으로 이어지며 **저장 및 실행 확정**으로 한 번에 저장하고 큐에 등록합니다.

종료된 매크로는 하위 케이스를 다시 검색해 추가·제외·순서 변경할 수 있습니다. 일반 저장은 새 계산을 시작하지 않으며, 변경한 목록으로 **실행**을 확정하면 새 큐 요청을 만듭니다. 제외한 child의 감시 티켓은 `alone-` 독립 티켓으로 보존하고 케이스 폴더와 결과는 유지합니다. 대기·실행 중인 매크로의 목록과 순서는 변경할 수 없습니다.

저장 후 실행 흐름은 다음과 같습니다.

1. 편집기가 순서가 있는 macro와 child JSON을 저장하고 `queue.state=waiting`, `queue.submit=true`, 고유 `request_id`를 기록합니다. 접수 플래그는 매크로에 둡니다.
2. 봇이 ofps 조회에 성공한 다음 감시 주기에 접수 플래그를 읽습니다. 기본 주기는 5초이며 스캔에 걸린 시간이 추가됩니다. 각 child를 SQLite FIFO 큐에 한 번만 등록하고 매크로의 `submit`을 `false`로 바꿉니다.
3. 자동 실행이 켜져 있고 큐가 일시 정지 상태가 아니면 맨 앞 작업을 검사합니다. 케이스의 외부 실행, 기존 CPU 예약, 가용 CPU를 확인합니다. 조건을 만족하지 못하면 앞 작업을 대기시킵니다.
4. `scheduler.openfoam_bashrc`를 불러와 OpenFOAM·MPI 실행 환경을 구성합니다. `foamDictionary`, `foamRun`, `decomposePar`, `wmake`, `mpicc`, `mpirun`과 MPI 구현 일치를 확인한 뒤, 자동 배정이면 ofps를 새로 조회해 가용 코어를 선택합니다. ofps로 선택된 CPU의 충돌·소켓/NUMA 정책을 최종 검사합니다. 환경 초기화나 정책 검사에 실패하면 계산을 시작하지 않고 대기 사유를 알립니다.
5. 별도 실행기가 공통 NP/CPU_SET을 적용하고 지정 CPU에서 `Allrun --foreground`를 실행합니다. 상태와 시작 알림을 갱신합니다.
6. 종료 코드·최신 로그·종료값으로 결과를 판정하고 child 및 macro 상태를 동기화합니다. 종료 알림을 보내고 다음 감시 주기에 다음 child를 실행합니다. 실패도 종료된 작업으로 취급하므로 다음 child로 진행합니다.

실행 직전 ofps로 실제 CPU 충돌·가용성·소켓/NUMA 정책을 검사하고 통과할 때만 시작합니다. 공통 NP/CPU_SET은 환경변수와 taskset으로 전달하며, `Allrun`과 `config/*Run`에 선언된 NP/CPU_SET도 같은 값으로 맞춥니다. 변경 전 파일은 `state/jobs/<id>/case-settings-before/`에 백업합니다. 자체적으로 분리 실행하는 `Allrun`은 `--foreground` 지원 시 그 옵션으로 실행하며, 지원을 확인할 수 없으면 완료까지 대기하는 명령을 지정할 때까지 대기합니다. 임의의 사용자 명령도 지정된 CPU 범위와 완료 대기 규칙을 지켜야 합니다.

자동 배정 티켓은 `"cpu_policy": "auto"`, 필요한 `cores`, `"allow_cross_socket": true`를 저장하고 고정 `cpu_set`은 저장하지 않습니다. 매 실행마다 Linux의 온라인 CPU·서비스 affinity·소켓/NUMA/물리 코어 구성을 읽고, ofps에서 관측한 CPU와 시작·계산·전후처리 중인 큐 작업의 예약 CPU를 제외합니다. 한 소켓/NUMA에 요청 수만큼 여유가 있으면 그중 남은 코어가 가장 적은 곳을 우선 사용합니다. 한 곳에 부족하면 가용 코어가 많은 NUMA 노드부터 필요한 수만큼 선택하고, 선택한 노드 사이에 가능한 균등하게 배분합니다. 각 노드에서는 CPU 번호순으로 선택합니다. SMT의 같은 물리 코어에 속한 스레드는 중복 배정하거나 다른 작업과 공유하지 않습니다.

전체 소켓의 빈 물리 코어 총수가 요청 NP보다 부족하면 FIFO 대기하며, 다음 감시 주기에 다시 배정합니다. NP를 임의로 줄이지 않습니다. 요청이 서비스에서 사용할 수 있는 전체 물리 코어 수보다 크면 코어 수를 줄이라는 이유를 표시합니다. 예를 들어 26코어 소켓이 두 개인 장비는 48코어를 24+24, 52코어를 26+26으로 배정할 수 있습니다. ofps 최종 검사에는 `--allow-cross-socket`을 전달하되 CPU 중복 검사는 그대로 유지합니다. 실제 배정 CPU는 작업 기록과 시작 알림에 남습니다. 새 개별 티켓도 자동 배정을 사용하며 NP는 기존 케이스 설정에서 읽습니다. 이전 자동 배정 티켓의 `allow_cross_socket: false`도 새 자동 정책에서는 허용으로 해석합니다. `cpu_policy`가 없는 구형 티켓은 기존 수동 설정을 보존하고, 매크로는 GUI/Telegram에서 자동 배정으로 전환할 수 있습니다.

`ofps`, `ofps --watch`, 봇의 주기 감시, Telegram `/stat`에서 성공적으로 조회한 상태를 child와 매크로 JSON에 함께 반영합니다. 프로세스가 사라졌다는 이유만으로 성공 처리하지 않으며 실제 worker 종료 코드·로그·종료값으로 성공/실패를 판정합니다. 조회 실패 시 이전 상태를 보존합니다. 시작·종료·CPU 대기 알림은 기존 Telegram outbox로 전달합니다.

Telegram **/queue**에서 진행 순서, 일시 정지·재개, 대기 취소를 제어합니다. 이미 실행 중인 solver는 큐 일시 정지로 중단하지 않습니다. `bot.json`의 `scheduler.enabled=true`가 필요합니다. GUI와 봇의 동시 저장은 파일 잠금과 원자적 교체로 보호합니다.

### 티켓별 watcher

| 키 | 판정 방식 |
|---|---|
| `log` | 단일 로그의 케이스 상대 경로 |
| `logs` | 다단계 로그 배열. 수정 시각이 가장 최근인 파일을 현재 단계로 선택 |
| `progress.pattern`, `group` | 진행값을 추출할 정규식과 capture group |
| `progress.start`, `end` | 구형 티켓 호환용. 정상 종료 기준에는 사용하지 않으며 GUI 저장 시 제거 |
| `failure.patterns` | 케이스별 실패 정규식 |
| `failure.include_openfoam_defaults` | OpenFOAM fatal, MPI abort, segfault, OOM 기본 패턴 사용 여부 |
| `failure.updated_files` | 이번 실행에서 갱신되면 실패로 보는 파일 |
| `missing_polls` | 프로세스 소멸 확정 전 연속 확인 횟수 |

`log`와 `logs`는 하나만 사용합니다. `logs`를 사용하면 다단계 `Allrun`의 현재 로그로 자동 전환하며, 단계 전환 시 파서 상태도 새 로그에 맞게 초기화합니다.

티켓이 없는 자동 감시에서는 케이스 최상위의 `log*` 일반 파일을 수정 시각순으로 검색하고 OpenFOAM 기본 fatal 패턴을 사용합니다. 케이스마다 로그 이름이나 실패 규칙이 다르면 티켓의 `watcher.logs`와 `watcher.failure`에 명시해야 정확한 판정과 Residual 첨부가 가능합니다.

정상 종료는 프로세스가 사라진 뒤 다음을 모두 만족할 때만 `succeeded`입니다.

1. `system/controlDict`의 `stopAt`이 `endTime`입니다.
2. 선택된 최신 로그의 최종 진행값 `Time`이 `controlDict.endTime` 이상입니다.
3. 0이 아닌 종료 코드, 실패 정규식, 이번 실행에서 갱신된 실패 표식이 없습니다.

최종 `Time`이 `endTime`보다 작거나, 최종 `Time` 또는 유효한 `controlDict`를 읽을 수 없거나, `stopAt`이 `endTime`이 아니면 `failed`로 알립니다. 따라서 로그의 `End` 문구만으로는 정상 종료가 되지 않습니다. 기존 티켓의 `success`와 `incomplete_status` 키는 읽을 수 있지만 최종 판정에는 사용하지 않으며, GUI로 저장하면 제거됩니다.

`command`는 계산이 끝날 때까지 기다리는 명령이어야 합니다. 자체 백그라운드 실행 후 즉시 반환하는 스크립트는 foreground/wait 모드로 호출해야 종료 코드를 수집할 수 있습니다.

## 2. Telegram UI 리소스

Telegram에 직접 표시되는 고정 문구, 버튼 라벨, 명령 설명, 상태·오류·알림 템플릿은 `telegram-ui/`에서 관리합니다. Android의 `resources`와 화면별 리소스 파일처럼 표시 내용과 Python 동작 코드를 분리했습니다.

```text
telegram-ui/
├── manifest.json
├── strings.json
├── menus/
│   ├── home.json
│   ├── cases.json
│   ├── queue.json
│   └── tickets.json
└── scenarios/
    ├── status.json
    ├── data.json
    ├── run.json
    ├── launch.json
    ├── notifications.json
    ├── outcomes.json
    ├── artifacts.json
    ├── jobs.json
    ├── runtime.json
    └── diagnostics.json
```

- `manifest.json`: 실행에 필요한 리소스 키 목록. Android의 생성된 resource table처럼 시작 시 누락 키를 검증
- `strings.json`: 공통 기호와 값
- `menus/*.json`: 홈, 케이스, 큐, 티켓 편집 화면의 제목·버튼·안내 문구
- `scenarios/*.json`: 실시간 상태, 데이터 전송, 계산 실행, 종료 판정, 알림, 오류 상황의 문구와 템플릿

코드에서는 파일 경로와 객체 경로를 합친 키를 사용합니다. 예를 들어 `menus/home.json`의 `help`는 `menus.home.help`, `scenarios/run.json`의 `completion`은 `scenarios.run.completion`으로 읽습니다. `{case_name}`, `{status}` 같은 Python format 치환값을 템플릿에서 사용할 수 있습니다. 상세·종료 템플릿의 전체 치환값은 다음과 같습니다.

`case_name`, `status`, `case_root`, `run_id`, `owner`, `cores`, `cpu_list`, `started_at`, `finished_at`, `elapsed`, `simulation_time`, `clock_time`, `eta`, `progress`, `return_code`, `reason`, `observed`, `log_path`, `residuals`, `errors`, `tail`, `postprocess_errors`

서비스 시작과 `check` 명령은 모든 JSON을 읽어 객체 구조와 템플릿 형식을 검증합니다. 필수 키가 없거나 JSON 또는 `{placeholder}` 문법이 잘못되면 조용히 기본값으로 대체하지 않고 오류로 중단합니다. 리소스는 프로세스에서 캐시하므로 수정 후 서비스를 재시작해야 합니다.

이전 버전의 `text_file`과 `text.json`은 상세·종료 템플릿 override 호환용으로만 지원합니다. 현재 `bot.json`은 이를 사용하지 않으며 `telegram-ui/scenarios/run.json`이 단일 원천입니다.

## 3. 봇 설정

```json
{
  "version": 1,
  "state_dir": "state",
  "ui_dir": "telegram-ui",
  "ofps_command": ["/home/joon/.local/bin/ofps"],
  "cases": [],
  "case_globs": ["tickets/*.json"],
  "poll_seconds": 5,
  "missing_polls": 2,
  "telegram": {
    "token_env": "TELEGRAM_BOT_TOKEN",
    "allowed_user_ids": [123456789],
    "chat_ids": [123456789]
  },
  "scheduler": {
    "enabled": false,
    "max_parallel": 1,
    "openfoam_bashrc": "deploy/openfoam-env.sh"
  }
}
```

티켓 변경은 다음 감시 주기부터 반영됩니다. 대기 작업은 실행 직전에 최신 티켓 설정을 다시 읽고, 이미 실행 중인 작업은 시작 시점의 설정을 사용합니다. `bot.json` 또는 `telegram-ui/`를 변경하면 서비스를 재시작합니다.

`scheduler.openfoam_bashrc`는 큐 실행기가 사용할 환경 초기화 파일입니다. 이 설치의 `deploy/openfoam-env.sh`는 `mpi/openmpi-x86_64` 모듈을 먼저 로드한 뒤 `/opt/OpenFOAM-dev/etc/bashrc`를 불러옵니다. SYSTEMOPENMPI는 초기화 시 `mpicc`로 라이브러리 경로를 찾으므로 이 순서가 필요합니다. 사용자 셸의 초기화 파일에 의존하지 않으며, 초기화된 PATH·라이브러리 경로를 worker, Allrun과 후처리에 전달합니다. 생략하거나 `null`로 두면 기존 서비스 환경을 사용합니다.

`python3 -m cfd_bot check --execution-env --mpi-probe`는 셸에서 설정한 MPI/OpenFOAM 경로를 제거한 환경에서 필수 명령과 MPI 구현을 확인합니다. `foamDictionary`, `decomposePar`, `foamRun`의 도움말 실행 및 MPI 1개 프로세스를 통한 `foamRun -help`까지 검사하며, 케이스 계산이나 분할은 시작하지 않습니다. MPI 실행 검사는 로컬 소켓 사용이 가능한 서버 환경에서 수행해야 합니다.

2026-09-26의 큐 실패는 `log.foamRun.preFlow`의 `exec: mpirun: not found`로 확인했습니다. OpenFOAM bashrc만 읽은 systemd 환경에는 `/usr/lib64/openmpi/bin`이 없어 분할 후 솔버 시작 단계에서 종료 코드 127이 발생했습니다. MPI 모듈 선행 로드와 셸 설정 없는 환경에서의 검사를 추가했습니다. 시작 전에 전체 실행 명령을 검사하므로 같은 환경 누락은 대기 상태에서 보고됩니다.

## 4. Telegram

BotFather에서 발급한 token은 JSON에 넣지 않고 환경변수로 전달합니다.

```bash
cd /home/joon/telegram/cfd_bot
read -r -s TELEGRAM_BOT_TOKEN
export TELEGRAM_BOT_TOKEN
python3 -m cfd_bot check
python3 -m cfd_bot serve
```

명령:

- `/stat`: 실시간 계산 현황
- `/data`: 케이스별 데이터
- `/tickets`: 티켓 생성·수정·복제와 매크로 큐 등록
- `/cancel`: 현재 티켓 입력·파일 선택·검색 취소 (초안 보관)
- `/queue`: 대기 큐
- `/clean`: 현재 대화의 추적된 메시지 삭제
- `/start`: 명령 안내

Telegram에서도 `/tickets` 또는 `/start`의 **티켓 만들기·편집** 버튼으로 GUI와 같은 티켓을 편집합니다. 케이스 데이터 화면의 **티켓 편집** 버튼으로 해당 티켓을 바로 열 수도 있습니다.

명령이나 외부 메뉴의 편집 버튼으로 들어오면 채팅 맨 아래에 새 편집 화면을 보냅니다. 화면 안에서 항목을 이동할 때는 그 메시지를 갱신합니다. `/clean` 후에는 이전 화면과 입력 대기를 해제하며 저장하지 않은 초안은 보관합니다.

1. 목록에서 티켓을 열거나 **새 개별 티켓 / 새 매크로 티켓**을 선택합니다.
2. **기본 설정**에서 Case directory, 표시 이름, 로그, Residual PNG 경로, 종료값과 알림을 설정합니다. 항목 버튼을 누른 뒤 채팅에 값을 보내며, **폴더 선택 / 로그 파일 선택 / PNG 파일 선택**은 봇이 실행되는 서버의 파일을 탐색합니다. 로그는 여러 개를 선택할 수 있습니다.
3. **종료·실패 판정**에서 실패 패턴과 공통 템플릿을 관리하고, **요청 데이터**에서 `/data`로 받을 파일을 추가·수정·삭제합니다.
4. 매크로는 **작업 큐 설정**에서 공통 NP·명령을 입력한 뒤 **하위 케이스 검색**을 실행합니다. CPU는 기본 자동 배정이며 **고급: CPU 직접 지정**을 선택한 경우에만 범위 입력·소켓 간 실행 허용 버튼을 표시합니다. 순서대로 표시된 행의 오른쪽 **삭제** 버튼으로 제외할 케이스를 고릅니다.
5. **검증**, **저장** 또는 **저장 및 큐 등록**을 선택하고 확인합니다. 새 매크로를 저장하면 child 티켓 생성과 순차 큐 접수가 함께 이뤄집니다. 접수된 매크로를 다시 저장하면 기존 큐 상태를 유지합니다. 이후 실행 제어는 `/queue`에서 합니다.

**선택 복제 / 티켓 삭제**도 같은 화면에서 제공합니다. GUI와 Telegram은 동일한 `TicketService`의 검증·저장·복제·삭제와 같은 패턴 템플릿 파일을 사용합니다. 초안은 사용자·대화별로 저장되어 봇 재시작 후 **편집 이어가기**로 복구합니다. 이미 지난 화면이나 다른 사용자의 편집 버튼은 적용하지 않으며, 다른 편집기에서 설정이 변경되었으면 다시 열도록 안내합니다. ofps의 계산 상태 갱신은 편집 충돌로 취급하지 않습니다.

여러 티켓을 지우려면 **티켓 목록 → 여러 티켓 선택·삭제**에서 체크하고 **선택 삭제 → 삭제 확정**을 누릅니다. 페이지 이동 후에도 선택이 유지되며 **현재 페이지 전체 선택 / 전체 선택 해제**를 사용할 수 있습니다. 매크로의 child 자동 포함과 실행 중 삭제 제한은 GUI와 동일합니다.

`/stat`은 각 계산을 다음 형식의 한 줄로 표시합니다.

```text
케이스 이름 | 소유대상 | 코어수/코어위치 | 한국 시작시각/경과시간
```

소유대상은 프로세스 계보와 환경을 읽어 SSH 원격 주소·TTY·sshd PID, 터미널, tmux/screen, systemd 또는 백그라운드 PID로 표시합니다. `/stat`은 `ofps`가 찾은 모든 실행을 표시하며, 티켓이 없는 경로에는 `미등록`을 붙입니다. 상세 버튼은 티켓이 등록된 실행에만 붙습니다. 종료된 계산의 상세 기록과 결과 파일은 `/data`에서 조회합니다.

`/clean`은 SQLite에 기록된 해당 대화의 메시지를 Telegram `deleteMessages`로 최대 100개씩 한 번에 삭제합니다. 수신 시각이 아니라 Telegram 메시지의 실제 생성 시각을 저장해 48시간 제한을 넘은 ID가 배치 삭제를 방해하지 않게 합니다. 배치가 특정 ID 때문에 거부될 때도 `deleteMessage`를 메시지마다 호출하지 않고 `deleteMessages` 묶음을 나누어 삭제 가능한 항목을 처리합니다. Telegram Bot API에는 과거 대화 내역을 다시 나열하는 기능이 없으므로 기능 적용 전에 주고받은 메시지는 찾을 수 없고, Telegram의 삭제 시간과 권한 제한에 걸린 메시지는 남을 수 있습니다.

## 5. 터미널

```bash
python3 -m cfd_bot check
python3 -m cfd_bot status
python3 -m cfd_bot enqueue tickets/my-case.json
python3 -m cfd_bot queue
python3 -m cfd_bot cancel JOB_ID
python3 -m cfd_bot pause
python3 -m cfd_bot resume
python3 -m cfd_bot monitor --once
python3 -m cfd_bot gui
```

기존 스캐너는 `/home/joon/.local/bin/ofps-legacy`로 보존하고 `/home/joon/.local/bin/ofps`를 이 저장소의 `bin/ofps`에 연결합니다. 확장 명령은 `--status`, `--json`, `--queue`, `--enqueue`, `--bot`을 추가하고 기존 옵션은 원본 `ofps`로 전달합니다.

## 6. 서비스

```bash
mkdir -p ~/.config/systemd/user ~/.config/cfd-bot
cp deploy/cfd-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cfd-bot
```

token은 `~/.config/cfd-bot/telegram.env`에 `TELEGRAM_BOT_TOKEN=...` 형식으로 저장하고 권한을 `600`으로 설정합니다. `KillMode=process`는 봇만 재시작할 때 독립 실행기와 solver를 계속 실행시키기 위한 설정입니다.

## 7. 검증

```bash
python3 -m unittest discover -s tests -v
```

테스트는 임시 케이스와 가짜 solver를 사용하며 실제 OpenFOAM 계산이나 Telegram 전송을 수행하지 않습니다.
