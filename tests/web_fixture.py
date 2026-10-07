"""Temporary web workspace for browser checks; no scheduler or Telegram client.

Run: python3 -m tests.web_fixture
The printed JSON file describes the fixture. Stop with Ctrl-C to remove it.
"""
import base64
import json
import select
import socket
import socketserver
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from cfd_bot.config import load_bot, load_case
from cfd_bot.tickets import atomic_json
from cfd_bot.web import WebApp, WebServer


class Forwarder(socketserver.BaseRequestHandler):
    """Emulate ssh -L TCP forwarding, including a different browser-side port."""
    def handle(self):
        with socket.create_connection(('127.0.0.1', self.server.backend_port)) as backend:
            streams = [self.request, backend]
            while True:
                ready, _, _ = select.select(streams, [], [], 10)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (backend if source is self.request else self.request).sendall(data)


def main():
    with tempfile.TemporaryDirectory(prefix='cfd-web-fixture-') as directory:
        root = Path(directory)
        for name in ('demo', 'new-case', 'copy-case', 'queue-one', 'queue-two', 'running-job',
                     'batch/alpha', 'batch/beta', 'batch/skip-template', 'batch/group/deep'):
            case = root / name
            (case / 'system').mkdir(parents=True)
            (case / 'system/controlDict').write_text('startTime 0; stopAt endTime; endTime 2500;')
            (case / 'Allrun').write_text('#!/bin/sh\nexit 0\n')
            (case / 'log.solver').write_text('Time = 80\nExecutionTime = 16 s  ClockTime = 17 s\nTime = 100\nExecutionTime = 20 s  ClockTime = 21 s\n')
        (root / 'batch/beta/postProcessing').mkdir()
        for name in ('alpha', 'beta'):
            target = root / 'batch' / name / 'monitoring/shape.png'
            target.parent.mkdir()
            target.write_bytes(name.encode())
        (root / 'demo/residual.png').write_bytes(base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='))
        config_file = root / 'bot.json'
        atomic_json(config_file, dict(version=1, state_dir='state', case_globs=['tickets/*.json'],
                                     telegram=dict(allowed_user_ids=[1], chat_ids=[1]),
                                     scheduler=dict(enabled=True)))
        app = WebApp(load_bot(config_file))
        atomic_json(app.service.path('alone-demo.json'), dict(version=1, name='Demo steady flow',
                    case_dir=str(root / 'demo'), residual_pattern='residual.png', cpu_policy='auto',
                    command=['./Allrun'], watcher=dict(log='log.solver')))
        tracked = app.store.enqueue(load_case(app.service.path('alone-demo.json')))
        app.store.update_job(tracked['id'], status='succeeded', started=1, finished=2)
        untracked = app.store.enqueue(dict(name='Deleted ticket case', _root=str(root / 'new-case'),
                                           command=['./Allrun'], watcher={}))
        app.store.update_job(untracked['id'], status='failed', started=2, finished=3)
        for name in ('queue-one', 'queue-two'):
            app.store.enqueue(dict(name=name, _root=str(root / name), command=['./Allrun'], watcher={}))
        running = app.store.enqueue(dict(name='Browser running', _root=str(root / 'running-job'),
                                         cores=1, cpu_set='0', command=['./Allrun'], watcher={}))
        app.store.update_job(running['id'], status='running', started=4)
        with patch('cfd_bot.ticket_run.snapshot', return_value={'cases': {}, 'raw': 'No active jobs'}):
            server = WebServer(app, 0)
            forwarder = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Forwarder)
            forwarder.daemon_threads = True
            forwarder.backend_port = server.server_port
            threading.Thread(target=forwarder.serve_forever, daemon=True).start()
            description = Path('/tmp/cfd-web-fixture.json')
            description.write_text(json.dumps(dict(url=f'http://127.0.0.1:{forwarder.server_address[1]}',
                                                    backend_port=server.server_port, root=str(root))))
            print(description, flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
                forwarder.shutdown()
                forwarder.server_close()


if __name__ == '__main__':
    main()
