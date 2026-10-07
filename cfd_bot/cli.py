from . import diagnostics as _diagnostics
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .config import ConfigError, cases_for, load_bot
from .processes import DaemonLock, snapshot
from .queue_control import cancel_queued_jobs
from .report import queue_text
from .storage import Store


@_diagnostics.trace
def parser():
    p = argparse.ArgumentParser(description='ofps 확장: OpenFOAM 상태, 로컬 대기 큐, Telegram 알림')
    p.add_argument('--config', default='bot.json', help='봇 설정 JSON')
    commands = p.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', help='설정 및 케이스 JSON 검증')
    check.add_argument('--execution-env', action='store_true', help='셸 설정 없는 환경에서 OpenFOAM/MPI 실행 확인 (계산 시작 안 함)')
    check.add_argument('--mpi-probe', action='store_true', help='MPI 1개 프로세스로 foamRun -help 실행 확인 (--execution-env 필요)')
    commands.add_parser('identify', help='Telegram /start 요청의 user_id/chat_id 조회 (메시지 전송 없음)')
    commands.add_parser('gui', help='케이스 티켓을 항목별 입력 폼으로 편집')
    web = commands.add_parser('web', help='localhost 웹 UI (기존 봇의 티켓·상태·큐 공유)')
    web.add_argument('--port', type=int, default=8766, help='localhost 포트 (기본 8766)')
    status = commands.add_parser('status', help='현재 프로세스, 관리 중인 계산과 큐')
    status.add_argument('--json', action='store_true')
    commands.add_parser('queue', help='대기/실행 중인 계산 목록')
    enqueue = commands.add_parser('enqueue', help='등록된 케이스를 FIFO 큐에 추가')
    enqueue.add_argument('case', help='케이스 디렉터리 또는 ofps.json 경로')
    cancel = commands.add_parser('cancel', help='대기 중인 작업만 취소')
    cancel.add_argument('job', nargs='+')
    commands.add_parser('pause', help='새 계산의 자동 시작 일시 정지')
    commands.add_parser('resume', help='자동 시작 재개 (scheduler.enabled=true 필요)')
    monitor = commands.add_parser('monitor', help='Telegram 없이 상태 수집/큐 실행; 알림은 보관')
    monitor.add_argument('--once', action='store_true')
    commands.add_parser('serve', help='Telegram 조회, 상태 감시, 알림 및 큐 실행')
    work = commands.add_parser('_worker', help=argparse.SUPPRESS)
    work.add_argument('--state', required=True)
    work.add_argument('--job', required=True)
    return p


@_diagnostics.trace
def main(argv=None):
    args = parser().parse_args(argv)
    _diagnostics.component(args.command)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.command == '_worker':
        _diagnostics.configure(state_dir=args.state, component='worker')
        if _diagnostics.enabled: _diagnostics.step('cli.main:L50:then')
        from .jobs import worker
        return worker(args.state, args.job)
    if args.command == 'gui':
        if _diagnostics.enabled: _diagnostics.step('cli.main:L53:then')
        from .gui import launch
        try:
            launch(Path(args.config).resolve().parent / 'tickets', args.config)
            return 0
        except RuntimeError as exc:
            if _diagnostics.enabled: _diagnostics.step('cli.main:L58:except')
            print('오류: ' + str(exc), file=sys.stderr)
            return 2
    try:
        config = load_bot(args.config)
        from .texts import load_text
        load_text(config.get('_text_file'))
        if args.command == 'check':
            if _diagnostics.enabled: _diagnostics.step('cli.main:L65:then')
            cases = cases_for(config, force=True)
            if args.mpi_probe and not args.execution_env:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L67:then')
                raise ValueError('--mpi-probe에는 --execution-env가 필요합니다.')
            if args.execution_env:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L69:then')
                import subprocess
                from .execution import (clean_service_environment, openfoam_environment,
                                        validate_openfoam_environment)
                env = clean_service_environment()
                env = openfoam_environment(config['scheduler'].get('openfoam_bashrc'), env)
                binaries = validate_openfoam_environment(env)
                probes = [(name, [binaries[name], '-help']) for name in ('foamDictionary', 'decomposePar', 'foamRun')]
                if args.mpi_probe:
                    if _diagnostics.enabled: _diagnostics.step('cli.main:L77:then')
                    probes.append(('MPI foamRun -help', [binaries['mpirun'], '-np', '1', binaries['foamRun'], '-help']))
                for label, command in probes:
                    if _diagnostics.enabled: _diagnostics.step('cli.main:L79:loop', label=label, command=command)
                    try:
                        probe = _diagnostics.run_process(command, env=env, stdin=subprocess.DEVNULL,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                    except subprocess.TimeoutExpired:
                        if _diagnostics.enabled: _diagnostics.step('cli.main:L83:except')
                        raise ValueError(f'{label} 실행 확인 시간 초과') from None
                    if probe.returncode:
                        if _diagnostics.enabled: _diagnostics.step('cli.main:L85:then')
                        detail = probe.stderr.decode(errors='replace')[-1200:]
                        raise ValueError(f'{label} 실행 확인 실패: 종료 코드 {probe.returncode}\n{detail}')
                    print(f'실행 확인 정상: {label}')
                print(f'OpenFOAM 환경 정상: {binaries["foamDictionary"]}')
                print(f'MPI 환경 정상: {binaries["mpirun"]}')
            print(f"설정 정상: {len(cases)}개 케이스 · 자동 실행 {'켜짐' if config['scheduler']['enabled'] else '꺼짐'}")
            for case in cases:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L92:loop', case=case)
                print(f"  {case['name']} · {case['_config']}")
            return 0
        if args.command == 'identify':
            if _diagnostics.enabled: _diagnostics.step('cli.main:L95:then')
            from .telegram import Telegram
            token = os.environ.get(config['telegram']['token_env'])
            if not token:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L98:then')
                raise ValueError(f"{config['telegram']['token_env']} 환경변수를 설정하세요.")
            api = Telegram(token)
            me = api.call('getMe', {})
            print('봇: @' + me.get('username', ''))
            updates = api.updates(0)
            found = set()
            for update in updates:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L105:loop', update=update)
                message = update.get('message', {})
                user = message.get('from', {}).get('id')
                chat = message.get('chat', {}).get('id')
                if user and chat and (user, chat) not in found:
                    if _diagnostics.enabled: _diagnostics.step('cli.main:L109:then')
                    print(f'user_id={user} chat_id={chat}')
                    found.add((user, chat))
            if not found:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L112:then')
                print('Telegram에서 이 봇에 /start를 보낸 후 다시 실행하세요.')
            return 0
        store = Store(config['state_dir'])
        if args.command == 'web':
            if _diagnostics.enabled: _diagnostics.step('cli.main:L116:then')
            from .web import serve as serve_web
            serve_web(config, store, args.port)
        elif args.command == 'status':
            if _diagnostics.enabled: _diagnostics.step('cli.main:L119:then')
            snap = snapshot(config['ofps_command'])
            snap['at'] = time.time()
            from .tickets import sync_ticket_states
            sync_ticket_states(config, store, snap)
            if args.json:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L124:then')
                from .logs import estimate
                jobs = store.jobs()
                for job in jobs:
                    if _diagnostics.enabled: _diagnostics.step('cli.main:L127:loop', job=job)
                    job['estimate'] = estimate(job['case'], job.get('telemetry', {}),
                                               time.time() - job.get('started', time.time()),
                                               store.runtime_history(job['case'], job.get('actual_cores')))
                print(json.dumps(dict(snapshot=snap, jobs=jobs,
                                      observed=[store.get('observed:' + c['_root']) for c in cases_for(config)],
                                      scheduler=config['scheduler'], queue_paused=store.get('queue_paused', False)),
                                 ensure_ascii=False, indent=2))
            else:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L124:else')
                from .bot import Bot
                store.put('snapshot', snap)
                print(Bot(config, store, None).status())
        elif args.command == 'queue':
            if _diagnostics.enabled: _diagnostics.step('cli.main:L139:then')
            print(queue_text(store, config['scheduler']['enabled'] and not store.get('queue_paused', False)))
        elif args.command == 'enqueue':
            if _diagnostics.enabled: _diagnostics.step('cli.main:L141:then')
            cases = cases_for(config)
            selected = Path(args.case).resolve()
            case = next((c for c in cases if selected in (Path(c['_root']), Path(c['_config']))), None)
            if case is None:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L145:then')
                raise ValueError('bot.json에 등록된 케이스 경로를 지정하세요.')
            job = store.enqueue(case)
            print(f"대기 큐 등록: {job['id']} · {case['name']}")
        elif args.command == 'cancel':
            if _diagnostics.enabled: _diagnostics.step('cli.main:L149:then')
            result = cancel_queued_jobs(store, args.job)
            if not result['cancelled']:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L151:then')
                raise ValueError('대기 중인 작업만 취소할 수 있습니다.')
            print(f"대기 취소: {len(result['cancelled'])}개" +
                  (f" · 상태 변경 {len(result['unavailable'])}개 제외" if result['unavailable'] else ''))
        elif args.command in ('pause', 'resume'):
            if _diagnostics.enabled: _diagnostics.step('cli.main:L155:then')
            store.put('queue_paused', args.command == 'pause')
            print('큐 일시 정지' if args.command == 'pause' else '큐 재개 (scheduler.enabled 설정 적용)')
        elif args.command in ('monitor', 'serve'):
            if _diagnostics.enabled: _diagnostics.step('cli.main:L158:then')
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                if _diagnostics.enabled: _diagnostics.step('cli.main:L160:loop', sig=sig)
                signal.signal(sig, lambda *_: stop.set())
            if args.command == 'serve':
                if _diagnostics.enabled: _diagnostics.step('cli.main:L162:then')
                from .bot import serve
                serve(config, store, stop)
            else:
                if _diagnostics.enabled: _diagnostics.step('cli.main:L162:else')
                from .monitor import Monitor
                monitor = Monitor(config, store)
                with DaemonLock(store.root / 'daemon.lock'):
                    while not stop.is_set():
                        if _diagnostics.enabled: _diagnostics.step('cli.main:L169:loop')
                        ok = monitor.run_once()
                        if args.once:
                            if _diagnostics.enabled: _diagnostics.step('cli.main:L171:then')
                            if not ok:
                                if _diagnostics.enabled: _diagnostics.step('cli.main:L172:then')
                                print(store.get('monitor_error')['message'], file=sys.stderr)
                            return 0 if ok else 1
                        stop.wait(config['poll_seconds'])
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        if _diagnostics.enabled: _diagnostics.step('cli.main:L177:except')
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
