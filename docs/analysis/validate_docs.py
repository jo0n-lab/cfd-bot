"""Validate documentation links, source fingerprints, SVG XML/text bounds and PNG rendering."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from PIL import Image, ImageFont

ROOT=Path(__file__).resolve().parents[2]
DOC=ROOT/'docs'
OUTPUT=Path('/tmp/cfd-architecture-review/rendered')
OUTPUT.mkdir(parents=True,exist_ok=True)
errors=[];counts={}
# Local file links and source line references (GitHub #L syntax).
mds=[DOC/'HLD.md',DOC/'LLD.md',DOC/'ARCHITECTURE.md',*sorted((DOC/'lld').glob('*.md')),*sorted((DOC/'analysis').glob('*.md'))]
link_count=0
for p in mds:
    text=p.read_text()
    for target in re.findall(r'\]\(([^)]+)\)',text):
        target=target.strip('<>')
        if target.startswith(('http:','https:','mailto:')):continue
        name,_,fragment=target.partition('#');dest=(p.parent/name).resolve() if name else p
        if not dest.exists():errors.append(f'{p.relative_to(ROOT)}: missing {target}');continue
        link_count+=1
        if fragment.startswith('L') and fragment[1:].isdigit():
            if int(fragment[1:])>len(dest.read_text().splitlines()):errors.append('invalid line '+target)
        elif fragment and dest.suffix=='.md':
            source=dest.read_text()
            anchors=set(re.findall(r'<a id="([^"]+)"',source))
            for heading in re.findall(r'^#+ (.+)$',source,re.M):
                h=re.sub(r'[^\w\- ]','',heading.lower()).replace(' ','-');anchors.add(h)
            if fragment not in anchors:errors.append(f'{p.relative_to(ROOT)}: missing anchor {target}')
counts['markdown_files']=len(mds);counts['local_links']=link_count
manifest=json.loads((DOC/'analysis/source-manifest.json').read_text())
for name,want in manifest['sha256'].items():
    if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=want:errors.append('source drift: '+name)
counts['source_fingerprints']=len(manifest['sha256'])
svgs=sorted([*DOC.glob('*.svg'),*(DOC/'diagrams').glob('*.svg')]);counts['svg_files']=len(svgs)
font_path=subprocess.check_output(['fc-match','-f','%{file}','Noto Sans CJK KR'],text=True)
font_cache={};bound_warnings=[]
for p in svgs:
    tree=ET.parse(p);r=tree.getroot();view=list(map(float,r.attrib['viewBox'].split()));width,height=view[2:]
    for node in r.iter():
        href=node.get('href') or node.get('{http://www.w3.org/1999/xlink}href')
        if href and not href.startswith(('http:','https:','#')):
            if not (p.parent/href.split('#')[0]).exists():errors.append(f'{p.name}: missing SVG link {href}')
        if node.tag.endswith('}text') and 'transform' not in node.attrib and p.parent.name=='diagrams':
            size=int(float(node.get('font-size','15')))
            font=font_cache.setdefault(size,ImageFont.truetype(font_path,size))
            content=''.join(node.itertext());text_width=font.getlength(content)
            x=float(node.get('x','0'));y=float(node.get('y','0'));anchor=node.get('text-anchor','start')
            left=x-text_width/2 if anchor=='middle' else x-text_width if anchor=='end' else x
            right=left+text_width
            if left<0 or right>width or y>height:bound_warnings.append((p.name,content[:75],round(left),round(right),width))
    dest=OUTPUT/p.name.replace('.svg','.png')
    subprocess.run(['rsvg-convert','-w','1400',str(p),'-o',str(dest)],check=True,capture_output=True)
    with Image.open(dest) as img:
        img.verify()
counts['rendered_png']=len(svgs)
report=dict(counts=counts,errors=errors,text_bounds=bound_warnings)
(OUTPUT.parent/'validation-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))
raise SystemExit(bool(errors or bound_warnings))
