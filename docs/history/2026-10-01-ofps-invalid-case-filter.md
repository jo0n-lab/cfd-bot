# 유효 CFD case가 없는 scanner 오탐 제거

- 날짜: 2026-10-01
- GitHub Issue: [#12](https://github.com/jo0n-lab/cfd-bot/issues/12)

## 배경

`/home/joon/.local/bin`이 `bin · 미등록 자동 감시`로 반복 등록되어 시작·실패 알림이 발송됐다. 저장된 관측에는 52코어 affinity의 일반 CLI 프로세스가 있었지만 계산 로그와 종료 코드는 없었다.

직접 원인은 `~/.local/bin/ofps` symlink의 호출 디렉터리를 기본 Basilisk root로 사용한 데 있다. 이 때문에 `~/.local/bin`의 native executable이 모두 Basilisk 후보가 됐고, `basilisk_case_from_process()`가 Makefile과 C source를 찾지 못해도 executable directory를 case로 반환했다.

OpenFOAM 경로에도 `system/controlDict`를 못 찾은 `CASE: <cwd> [controlDict not found]`를 parser가 정상 경로로 바꾸는 같은 종류의 경계 오류가 있었다.

## As-Is HLD

```mermaid
flowchart LR
    PROC[프로세스 이름 또는 경로 일치] --> FIND[case 탐색]
    FIND --> MISS[case 증거 없음]
    MISS --> RAW[cwd 또는 executable directory를 CASE로 출력]
    RAW --> PARSE[정상 case로 수용]
    PARSE --> WATCH[미등록 자동 감시와 실패 알림]
```

## To-Be HLD

```mermaid
flowchart LR
    PROC[프로세스 후보] --> KIND{engine}
    KIND -->|OpenFOAM| CONTROL[system/controlDict 확인]
    KIND -->|Basilisk| SOURCE[Makefile + C source 확인]
    CONTROL --> VALID{유효 case}
    SOURCE --> VALID
    VALID -->|예| RAW[CASE 출력]
    VALID -->|아니오| DROP[관측에서 제외]
    RAW --> WATCH[현황과 자동 감시]
```

## As-Is LLD

```mermaid
sequenceDiagram
    participant O as bin/ofps
    participant P as parse_snapshot
    participant M as Monitor
    O->>O: symlink 호출 경로를 Basilisk root로 사용
    O->>O: case 탐색 실패 후 executable directory 반환
    O-->>P: CASE ~/.local/bin
    P-->>M: ~/.local/bin 활성 case
    M->>M: 자동 감시 및 terminal 실패 알림
```

## To-Be LLD

```mermaid
sequenceDiagram
    participant O as bin/ofps
    participant P as parse_snapshot
    participant M as Monitor
    O->>O: 실제 ofps 파일 위치를 기본 Basilisk root로 사용
    alt OpenFOAM controlDict 없음
        O->>O: process record 제외
    else Basilisk Makefile/C source 없음
        O->>O: process record 제외
    else 유효 case
        O-->>P: CASE root
        P-->>M: 활성 case
    end
    P->>P: legacy case-not-found block도 폐기
```

## 호환성과 검증

- `system/controlDict`가 있는 OpenFOAM case, `-case` 지정과 case 내부 하위 디렉터리 실행은 유지한다.
- `OFPS_BASILISK_ROOT`로 지정한 실제 tree와 Makefile/C source가 있는 Basilisk case는 유지한다.
- symlink 설치 경로의 일반 executable은 계산으로 분류하지 않는다.
- scanner 통합 테스트와 parser 단위 테스트로 두 engine의 invalid case가 snapshot에 들어오지 않는지 확인한다.
