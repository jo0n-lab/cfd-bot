"""Map every documented sequence node to its diagnostic boundary; no runtime hooks."""
import ast
import hashlib
import json
from pathlib import Path
import runpy

ROOT=Path(__file__).resolve().parents[2]


def main():
    policy=runpy.run_path(str(ROOT/'cfd_bot/diagnostic_policy.py'))['basic_function']
    entries=[]
    for path in sorted((ROOT/'cfd_bot').glob('*.py')):
        if path.name in ('diagnostics.py', 'diagnostic_codec.py', 'diagnostic_policy.py'):continue
        tree=ast.parse(path.read_text())
        def visit(node,parents=()):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                names=(*parents,node.name)
                if not isinstance(node,ast.ClassDef):
                    entries.append(dict(function=path.stem+'.'+'.'.join(names),file=str(path.relative_to(ROOT)),line=node.lineno,
                                        events=['function.call','function.return','function.raise']))
                parents=names
            for child in ast.iter_child_nodes(node):visit(child,parents)
        visit(tree)
        for node in ast.walk(tree):
            if (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and
                isinstance(node.func.value,ast.Name) and node.func.value.id=='_diagnostics' and node.func.attr=='step'):
                entries.append(dict(step=node.args[0].value,file=str(path.relative_to(ROOT)),line=node.lineno,events=['sequence.step','steps[]']))
    for entry in entries:
        entry['detailed']='all occurrences'
        entry['basic']=('call/return/raise' if policy(entry['function']) else 'exception only; normal calls omitted') if 'function' in entry else ('handled exception' if entry['step'].endswith(':except') else 'omitted')
    functions={e['function']:e for e in entries if 'function' in e}
    js=(ROOT/'cfd_bot/web_static/app.js').read_text()
    import re
    js_recorder=(ROOT/'cfd_bot/web_static/diagnostics.js').read_text()
    js_basic=set(re.search(r"const basicFunctions=new Set\('([^']+)'",js_recorder)[1].split())
    js_ids=re.findall(r'"(web\.ui\.[^"\n]+)"',js)
    shell=(ROOT/'bin/ofps').read_text()
    shell_names=set(re.findall(r'^([a-z_]+)\(\)',shell,re.M))
    boundaries={
        'os.getpgid':'queue_control.interrupt_process_groups result/exception; detailed branch',
        'os.killpg':'process.signal.request → existing signal call; exception propagation',
        'tkinter.messagebox.askyesno':'gui.TicketEditor.interrupt_active_job / cancel_queue_selection → request/result/exception',
        'subprocess.run':'subprocess.request/response/error', 'subprocess.Popen':'subprocess.spawn/started/error',
        'urllib.request.urlopen':'telegram.Telegram.call → function.return/raise',
        'pathlib.Path.unlink':'TicketService.delete_many → function.return/raise + sequence steps',
        'pathlib.Path.open':'web.WebApp.file / Handler.handle_request → function.return/raise',
        'OpenSSH ssh':'launcher ssh.started/exec/cleanup; stderr and native exit remain with OpenSSH',
        'gui.TicketEditor.submit / enqueue':'gui.TicketEditor.submit → function.call/return/raise',
        'statistics.median':'logs.estimate input/result + sequence steps',
        'tkinter.filedialog.askopenfilenames':'gui.TicketEditor.choose_logs input/result + sequence steps',
        'tkinter.messagebox.askyesnocancel':'gui.TicketEditor.confirm_switch input/result + sequence steps',
    }
    flows=[]
    def mapping(node):
        name=node['name'];result=[]
        if name:
            item={'node':name}
            lookup=re.sub(r'\([^)]*\)$','',name)
            if lookup in functions:
                item['function']=lookup
                item['basic']=functions[lookup]['basic']
            elif name.startswith('web_static.app.js:'):
                js_name=name.split(':',1)[1].split('(')[0]
                item['functions']=[key for key in js_ids if key.startswith('web.ui.'+js_name+':')]
                item['basic']='call/return/raise' if js_name in js_basic else 'UI event / exception; helper normal calls omitted'
                if not item['functions']:item['boundary']='ui.click/change/navigation → app.js callbacks + HTTP response'
            elif name.startswith('bin/ofps:'):
                fn=name.split(':',1)[1]
                item['boundary']='shell.call/return/error: '+fn if fn in shell_names else 'shell.start/exit + subprocess.request/response'
            elif name in boundaries:item['boundary']=boundaries[name]
            else:item['unmapped']=True
            item.setdefault('basic','existing boundary events; shell normal helpers detailed only')
            item['detailed']='full mapped boundary'
            result.append(item)
        for child in node['children']:result.extend(mapping(child))
        return result
    for flow in runpy.run_path(str(ROOT/'docs/diagrams/build_flows.py'))['charts']:
        flows.append(dict(id=flow['key'],title=flow['title'],trigger=flow['trigger'],nodes=mapping(flow['root'])))
    unknown=sorted({n['node'] for flow in flows for n in flow['nodes'] if n.get('unmapped')})
    document=dict(schema_version=1,base_revision='ebf494f',entries=entries,javascript_functions=js_ids,
                  shell_functions=sorted(shell_names),flows=flows,unmapped=unknown,
                  collection_levels={'default_on':'basic','basic':'business calls + explicit events + warnings/errors; helper normal calls and branches omitted','detailed':'all instrumented calls/branches'},
                  branch_encoding='detailed steps[] = [entries index, sequence number, UTC timestamp ns, optional fields]; no sampling',
                  source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                                 for pattern in ('cfd_bot/*.py','cfd_bot/web_static/*.js','bin/*','clients/macos/*','clients/windows/*')
                                 for p in sorted(ROOT.glob(pattern)) if p.is_file()})
    (ROOT/'cfd_bot/diagnostic_map.json').write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n')
    lines=['# HLD/LLD 로그 대응표 (#18 / #30)', '', '[기록 계약](../DIAGNOSTICS.md) · [기계 지도와 노드별 수준](../../cfd_bot/diagnostic_map.json)', '',
           '기본(basic)은 업무 경계와 명시적 사건·경고·예외를 기록한다. 내부 helper의 정상 호출/분기는 상세(detailed) 전용이며 기본 기록에서 복원할 수 없다. UI callback은 실제 사용자 Event일 때 기본 기록한다. 아래는 정적 대응이며 전체 시나리오의 실제 실행 검증을 뜻하지 않는다.', '',
           '| 시퀀스 | 목적 | 노드 | basic Python 업무 함수 | 상세 전용 Python 정상 호출 | UI/외부 경계 |', '|---|---|---:|---:|---:|---:|']
    for flow in flows:
        nodes=flow['nodes'];normal=sum(n.get('basic')=='call/return/raise' and 'function' in n for n in nodes);helpers=sum('function' in n for n in nodes)-normal
        lines.append(f"| [{flow['id']}](../diagrams/{flow['id']}.svg) | {flow['title']} | {len(nodes)} | {normal} | {helpers} | {sum('function' not in n for n in nodes)} |")
    lines += ['', 'Python 노드별 basic/detailed 구분은 source map entries/flows에 있다. 기본 업무 함수가 없는 순수 내부 계산 시퀀스는 호출한 상위 업무 경계와 오류로 관측한다. 상세 함수 호출/분기 재구성이 필요하면 사전에 detailed를 켠다.', '']
    (ROOT/'docs/analysis/diagnostic-flow-coverage.md').write_text('\n'.join(lines))
    print(json.dumps(dict(functions=len(functions),steps=sum('step'in e for e in entries),javascript=len(js_ids),flows=len(flows),unmapped=unknown),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
