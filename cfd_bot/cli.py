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
from .report import queue_text
from .storage import Store


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
    cancel.add_argument('job')
    commands.add_parser('pause', help='새 계산의 자동 시작 일시 정지')
    commands.add_parser('resume', help='자동 시작 재개 (scheduler.enabled=true 필요)')
    monitor = commands.add_parser('monitor', help='Telegram 없이 상태 수집/큐 실행; 알림은 보관')
    monitor.add_argument('--once', action='store_true')
    commands.add_parser('serve', help='Telegram 조회, 상태 감시, 알림 및 큐 실행')
    work = commands.add_parser('_worker', help=argparse.SUPPRESS)
    work.add_argument('--state', required=True)
    work.add_argument('--job', required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.command == '_worker':
        from .jobs import worker
        return worker(args.state, args.job)
    if args.command == 'gui':
        from .gui import launch
        try:
            launch(Path(args.config).resolve().parent / 'tickets', args.config)
            return 0
        except RuntimeError as exc:
            print('오류: ' + str(exc), file=sys.stderr)
            return 2
    try:
        config = load_bot(args.config)
        from .texts import load_text
        load_text(config.get('_text_file'))
        cases = cases_for(config)
        if args.command == 'check':
            if args.mpi_probe and not args.execution_env:
                raise ValueError('--mpi-probe에는 --execution-env가 필요합니다.')
            if args.execution_env:
                import subprocess
                from .execution import (clean_service_environment, openfoam_environment,
                                        validate_openfoam_environment)
                env = clean_service_environment()
                env = openfoam_environment(config['scheduler'].get('openfoam_bashrc'), env)
                binaries = validate_openfoam_environment(env)
                probes = [(name, [binaries[name], '-help']) for name in ('foamDictionary', 'decomposePar', 'foamRun')]
                if args.mpi_probe:
                    probes.append(('MPI foamRun -help', [binaries['mpirun'], '-np', '1', binaries['foamRun'], '-help']))
                for label, command in probes:
                    try:
                        probe = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                    except subprocess.TimeoutExpired:
                        raise ValueError(f'{label} 실행 확인 시간 초과') from None
                    if probe.returncode:
                        detail = probe.stderr.decode(errors='replace')[-1200:]
                        raise ValueError(f'{label} 실행 확인 실패: 종료 코드 {probe.returncode}\n{detail}')
                    print(f'실행 확인 정상: {label}')
                print(f'OpenFOAM 환경 정상: {binaries["foamDictionary"]}')
                print(f'MPI 환경 정상: {binaries["mpirun"]}')
            print(f"설정 정상: {len(cases)}개 케이스 · 자동 실행 {'켜짐' if config['scheduler']['enabled'] else '꺼짐'}")
            for case in cases:
                print(f"  {case['name']} · {case['_config']}")
            return 0
        if args.command == 'identify':
            from .telegram import Telegram
            token = os.environ.get(config['telegram']['token_env'])
            if not token:
                raise ValueError(f"{config['telegram']['token_env']} 환경변수를 설정하세요.")
            api = Telegram(token)
            me = api.call('getMe', {})
            print('봇: @' + me.get('username', ''))
            updates = api.updates(0)
            found = set()
            for update in updates:
                message = update.get('message', {})
                user = message.get('from', {}).get('id')
                chat = message.get('chat', {}).get('id')
                if user and chat and (user, chat) not in found:
                    print(f'user_id={user} chat_id={chat}')
                    found.add((user, chat))
            if not found:
                print('Telegram에서 이 봇에 /start를 보낸 후 다시 실행하세요.')
            return 0
        store = Store(config['state_dir'])
        if args.command == 'web':
            from .web import serve as serve_web
            serve_web(config, store, args.port)
        elif args.command == 'status':
            snap = snapshot(config['ofps_command'])
            snap['at'] = time.time()
            from .tickets import sync_ticket_states
            sync_ticket_states(config, store, snap)
            if args.json:
                from .logs import estimate
                jobs = store.jobs()
                for job in jobs:
                    job['estimate'] = estimate(job['case'], job.get('telemetry', {}),
                                               time.time() - job.get('started', time.time()),
                                               store.runtime_history(job['case'], job.get('actual_cores')))
                print(json.dumps(dict(snapshot=snap, jobs=jobs,
                                      observed=[store.get('observed:' + c['_root']) for c in cases],
                                      scheduler=config['scheduler'], queue_paused=store.get('queue_paused', False)),
                                 ensure_ascii=False, indent=2))
            else:
                from .bot import Bot
                store.put('snapshot', snap)
                print(Bot(config, store, None).status())
        elif args.command == 'queue':
            print(queue_text(store, config['scheduler']['enabled'] and not store.get('queue_paused', False)))
        elif args.command == 'enqueue':
            selected = Path(args.case).resolve()
            case = next((c for c in cases if selected in (Path(c['_root']), Path(c['_config']))), None)
            if case is None:
                raise ValueError('bot.json에 등록된 케이스 경로를 지정하세요.')
            job = store.enqueue(case)
            print(f"대기 큐 등록: {job['id']} · {case['name']}")
        elif args.command == 'cancel':
            job = store.update_job(args.job, expected=('queued',), status='cancelled', finished=time.time())
            if job is None:
                raise ValueError('대기 중인 작업만 취소할 수 있습니다.')
            print('대기 취소: ' + args.job)
        elif args.command in ('pause', 'resume'):
            store.put('queue_paused', args.command == 'pause')
            print('큐 일시 정지' if args.command == 'pause' else '큐 재개 (scheduler.enabled 설정 적용)')
        elif args.command in ('monitor', 'serve'):
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            if args.command == 'serve':
                from .bot import serve
                serve(config, store, stop)
            else:
                from .monitor import Monitor
                monitor = Monitor(config, store)
                with DaemonLock(store.root / 'daemon.lock'):
                    while not stop.is_set():
                        ok = monitor.run_once()
                        if args.once:
                            if not ok:
                                print(store.get('monitor_error')['message'], file=sys.stderr)
                            return 0 if ok else 1
                        stop.wait(config['poll_seconds'])
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
