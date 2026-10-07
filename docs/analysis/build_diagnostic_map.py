"""Map every documented sequence node to its diagnostic boundary; no runtime hooks."""
import ast
import hashlib
import json
from pathlib import Path
import runpy

ROOT=Path(__file__).resolve().parents[2]


def main():
    entries=[]
    for path in sorted((ROOT/'cfd_bot').glob('*.py')):
        if path.name=='diagnostics.py':continue
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
    functions={e['function']:e for e in entries if 'function' in e}
    js=(ROOT/'cfd_bot/web_static/app.js').read_text()
    import re
    js_ids=re.findall(r'"(web\.ui\.[^"\n]+)"',js)
    shell=(ROOT/'bin/ofps').read_text()
    shell_names=set(re.findall(r'^([a-z_]+)\(\)',shell,re.M))
    boundaries={
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
            if name in functions:item['function']=name
            elif name.startswith('web_static.app.js:'):
                js_name=name.split(':',1)[1].split('(')[0]
                item['functions']=[key for key in js_ids if key.startswith('web.ui.'+js_name+':')]
                if not item['functions']:item['boundary']='ui.click/change/navigation → app.js callbacks + HTTP response'
            elif name.startswith('bin/ofps:'):
                fn=name.split(':',1)[1]
                item['boundary']='shell.call/return/error: '+fn if fn in shell_names else 'shell.start/exit + subprocess.request/response'
            elif name in boundaries:item['boundary']=boundaries[name]
            else:item['unmapped']=True
            result.append(item)
        for child in node['children']:result.extend(mapping(child))
        return result
    for flow in runpy.run_path(str(ROOT/'docs/diagrams/build_flows.py'))['charts']:
        flows.append(dict(id=flow['key'],title=flow['title'],trigger=flow['trigger'],nodes=mapping(flow['root'])))
    unknown=sorted({n['node'] for flow in flows for n in flow['nodes'] if n.get('unmapped')})
    document=dict(schema_version=1,base_revision='ebf494f',entries=entries,javascript_functions=js_ids,
                  shell_functions=sorted(shell_names),flows=flows,unmapped=unknown,
                  branch_encoding='steps[] = [entries index, sequence number, UTC timestamp ns, optional fields]; no sampling',
                  source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                                 for pattern in ('cfd_bot/*.py','cfd_bot/web_static/*.js','bin/*','clients/macos/*','clients/windows/*')
                                 for p in sorted(ROOT.glob(pattern)) if p.is_file()})
    (ROOT/'cfd_bot/diagnostic_map.json').write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(functions=len(functions),steps=sum('step'in e for e in entries),javascript=len(js_ids),flows=len(flows),unmapped=unknown),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
