"""Build HLD summaries and retain the existing diagram URLs."""
from pathlib import Path
import html
import json
import subprocess

here=Path(__file__).resolve().parent;docs=here.parent
for source,target in [('system-hld.dot','hld-system-design.svg'),('platform-architecture.dot','ofps-telegram-architecture.svg')]:
    subprocess.run(['dot','-Tsvg',str(here/source),'-o',str(docs/target)],check=True)
# Copies use the same sequence content and rewrite local detail links for docs/.
for source,target in [('D-03.svg','lld-component-design.svg'),('UC-02-web.svg','web-interface-design.svg')]:
    text=(here/source).read_text()
    text=text.replace('href="D-','href="diagrams/D-')
    (docs/target).write_text(text)
items=[('UC-01~06','진입 · 현황 · 상세 · 결과','lld/flows.md'),('UC-07~14','티켓 CRUD · 검증 · 저장','lld/flows.md'),('UC-15~18','매크로 · 실행 설정 · 실행','lld/flows.md'),('UC-19~25','큐 · 템플릿 · 취소 · 정리','lld/flows.md'),('Telegram','명령 / callback / session','lld/telegram.md'),('cfd-ticket-gui','Tk callback / worker / UI','lld/gui.md'),('Web','Browser / HTTP / domain','lld/web.md'),('CLI / launcher','운영 명령 / SSH / 브라우저','lld/runtime.md'),('D-01~15','공용 함수 요청 · 반환','lld/domain.md'),('BG-01~03','scan / 관측 / 제출 접수','lld/runtime.md'),('BG-04~07','admission / worker / 알림 / 복구','lld/runtime.md'),('성능 분석','반복 호출 · lock · I/O · 개선안','analysis/performance.md')]
parts=['<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="730" viewBox="0 0 1600 730"><title>전체 유즈케이스 함수 요청 응답 그림 탐색</title><rect width="1600" height="730" fill="#f8fafc"/><g font-family="Noto Sans CJK KR, sans-serif"><text x="50" y="60" font-size="30" font-weight="700">전체 유즈케이스 → 플랫폼별 함수 요청·응답 시퀀스</text><text x="50" y="100" font-size="18" fill="#475569">각 상자를 눌러 실제 함수명·입력·반환·외부 호출·오류가 표시된 그림으로 이동</text>']
for i,(title,sub,url) in enumerate(items):
    x=50+(i%4)*385;y=140+(i//4)*175
    parts.append(f'<a href="{url}"><rect x="{x}" y="{y}" width="355" height="135" rx="10" fill="white" stroke="#86efac"/><rect x="{x}" y="{y}" width="355" height="44" rx="10" fill="#16a34a"/><text x="{x+18}" y="{y+30}" fill="white" font-size="20" font-weight="700">{html.escape(title)}</text><text x="{x+18}" y="{y+83}" font-size="17">{html.escape(sub)}</text><text x="{x+18}" y="{y+112}" font-size="14" fill="#047857">요청 → 실제 함수 → 반환</text></a>')
parts.append('<text x="50" y="704" font-size="15" fill="#64748b">#24 NP 기반 자동 quota·동적 macro 반영 · 2026-10-07 · 미지원 플랫폼 기능은 ARCHITECTURE 표에서 N/A로 구분</text></g></svg>')
(docs/'all-feature-flows.svg').write_text(''.join(parts)+'\n')
print('Rendered 5 overview / compatibility SVGs')
