# CFD 웹 연결

압축을 풀고 더블클릭합니다.

- Windows: `Start CFD.cmd`
- Mac: `CFD Control Room.app`

**SSH config의 Host 목록에서 번호 선택 → SSH 연결 → 브라우저 열기.** 비밀번호가 필요하면 SSH 창에 입력합니다. 사용 중에는 연결 창을 열어 두고, 종료할 때 Enter를 누릅니다.

Windows는 `%USERPROFILE%/.ssh/config`, Mac은 `~/.ssh/config`의 Host 별칭을 읽습니다. Include 파일도 읽고, 중복과 `Host *` 같은 패턴은 제외합니다. 목록에서 번호만 선택하면 기존 User·Port·IdentityFile·ProxyJump 설정은 SSH가 그대로 적용합니다. 별도 설정을 저장하거나 기존 config를 수정하지 않습니다. 웹 포트는 `8766`입니다.

서버의 웹 서비스가 실행 중이어야 합니다. Windows는 기본 OpenSSH Client가 필요합니다. Mac에서 처음 실행이 차단되면 우클릭 → 열기를 사용합니다.

빌드: `python3 clients/build.py`. Windows/Mac 실기기 실행은 미검증이며 Linux에서 ZIP·구문·가짜 SSH 실행 흐름을 확인했습니다.
