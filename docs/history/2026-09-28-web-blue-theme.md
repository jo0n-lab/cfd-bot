# 웹 UI 블루 테마

GitHub Issue: [#5](https://github.com/jo0n-lab/cfd-bot/issues/5)

첨부 이미지는 CFD 결과의 선명한 blue/cyan 계열 색감만 참고한다. 톱니와 이미지는 웹 asset으로 사용하지 않는다. 추가로 사용자가 지정한 Instagram 게시물의 1~4페이지만 참고해, 토스 계열 화면의 강한 명암 분리·굵은 제목·넓은 여백·둥근 색상 면을 기존 레이아웃 안에서 CSS로 번역한다.

## Visual system

| 의미 | Border | Background | 적용 대상 |
|---|---|---|---|
| 활성·정상 | blue 40~60% alpha | blue 8~13% alpha | 실행 가능, 실행 중, 정상 종료, 선택 행 |
| 대기·주의 | yellow 45~70% alpha | yellow 3~13% alpha | 계산 대기, 시작 중, 후처리, postProcessing 존재 |
| 실패·비활성 | red 40~70% alpha | red 3~10% alpha | 실패, 중단, 취소, 설정 오류 |

전체 배경은 밝은 회청색, sidebar는 navy/blue, panel은 흰색과 얕은 shadow를 사용한다. 장식용 gradient는 sidebar와 통계 카드의 작은 색상 면으로 제한한다. 주요 버튼·입력 focus·progress·선택 항목은 blue로 통일한다. 보조 버튼은 옅은 회색의 weak style, 상태 badge는 blue/yellow/red weak style을 사용한다. 설정용 boolean checkbox는 켜짐 blue, 꺼짐 red로 표시한다. 티켓 선택 checkbox는 선택 도구이므로 중립 상태를 유지한다.

기능, 정보 구조, desktop/mobile breakpoint는 유지한다. Chromium으로 dashboard, ticket/editor, queue, data, mobile 화면과 console·가로 overflow를 확인하고 기존 기능 테스트 후 웹 서비스를 재시작한다.

## Result

- 기존 HTML 배치와 화면 문구, JavaScript 동작은 유지하고 `web_static/style.css`의 시각 토큰과 컴포넌트 표현만 변경했다.
- navy sidebar와 회청색 작업 영역, 흰색 18px radius card, 굵은 제목, 단색 blue primary button, gray weak button을 적용했다.
- 실행·정상 상태는 blue, 대기·후처리는 yellow, 실패·중단은 red의 alpha border/background로 표시한다. 표·큐·티켓 행의 왼쪽 border에도 같은 의미를 적용했다.
- boolean 설정은 checked blue / unchecked red, ticket 선택 checkbox는 중립 선택 도구로 유지했다.
- Chromium desktop/mobile 전체 시나리오와 실제 서비스 read-only smoke가 통과했다. Python 전체 테스트는 238개 통과, 1개 환경 의존 테스트가 skip되었다.
- `cfd-bot-web.service`를 재시작하고 `127.0.0.1:8766`에서 새 스타일 적용을 확인했다.
