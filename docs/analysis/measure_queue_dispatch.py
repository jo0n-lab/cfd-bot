"""Measure the actual queue dispatcher with read-only SQL and fake Telegram calls."""

def main():
    import contextlib
    from datetime import datetime
    import json
    from pathlib import Path
    import signal
    import sqlite3
    import sys
    import time

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    import cfd_bot.bot as module
    import cfd_bot.config as config_module
    from cfd_bot.storage import Store
    from cfd_bot.telegram import Telegram

    class ReadOnlyStore(Store):
        def __init__(self, path):
            self.path, self.root = path, path.parent
            self.connections = self.history_calls = 0
        @contextlib.contextmanager
        def connect(self):
            self.connections += 1
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            try:
                yield db
            finally:
                db.close()
        def runtime_history(self, *args, **kwargs):
            self.history_calls += 1
            return super().runtime_history(*args, **kwargs)

    class FakeTelegram(Telegram):
        def __init__(self):
            self.parts = self.characters = self.keyboards = 0
        def call(self, method, payload, *args, **kwargs):
            assert method == 'sendMessage'
            self.parts += 1
            self.characters += len(payload['text'])
            self.keyboards += int('reply_markup' in payload)
            return None  # No message ID: Bot._remember cannot write message tracking.

    def expired(*args):
        raise TimeoutError('read-only dispatcher diagnostic exceeded 90 seconds')
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(90)
    timings = {}
    def trace(label, fn):
        def wrapped(*args, **kwargs):
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                row = timings.setdefault(label, {'calls': 0, 'seconds': 0})
                row['calls'] += 1
                row['seconds'] += time.perf_counter() - start
        return wrapped

    original = config_module.tickets_for
    config_module.tickets_for = module.tickets_for = trace('tickets_for', original)
    for name in ('cases_for', 'queue_text', 'running_macro_views'):
        setattr(module, name, trace(name, getattr(module, name)))
    config = config_module.load_bot(root / 'bot.json')
    store = ReadOnlyStore(Path(config['state_dir']) / 'state.sqlite3')
    api = FakeTelegram()
    bot = module.Bot(config, store, api)
    start = time.perf_counter()
    bot.dispatch(0, 'queue', 'readonly-diagnostic')
    elapsed = time.perf_counter() - start
    signal.alarm(0)
    for row in timings.values():
        row['seconds'] = round(row['seconds'], 6)
    result = dict(measured_at=datetime.now().astimezone().isoformat(),
                  dispatch_seconds=round(elapsed, 6), timings=timings,
                  sqlite_connections=store.connections, runtime_history_calls=store.history_calls,
                  fake_sendMessage_calls=api.parts, text_characters=api.characters,
                  keyboard_payloads=api.keyboards,
                  notes=['Real Bot.dispatch(queue) and Telegram.send; transport replaced by a fake.',
                         'SQL read-only/query_only; no sends, snapshot, scheduler, or message tracking writes.',
                         'cases_for includes its tickets_for time; timing rows overlap, do not sum them.',
                         'Sandbox process; queue path does not inspect host /proc. No request backlog or network time.'])
    output = Path('/tmp/cfd-architecture-review/queue-dispatch-results.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
