"""Build queue text using read-only SQL; never send messages or change tickets."""

def main():
    from pathlib import Path
    Path('/tmp/cfd-architecture-review').mkdir(parents=True, exist_ok=True)
    import contextlib
    import datetime
    import json
    from pathlib import Path
    import signal
    import sqlite3
    import sys
    import time

    ROOT=Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(ROOT))
    from cfd_bot.config import load_bot
    from cfd_bot.storage import Store
    from cfd_bot.report import queue_text
    from cfd_bot.telegram import chunks
    from cfd_bot.ui import load_ui

    class ReadOnlyStore(Store):
        def __init__(self,path):
            self.path=path
            self.root=path.parent
            self.connections=0
            self.history_calls=0
        @contextlib.contextmanager
        def connect(self):
            self.connections+=1
            db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=2)
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            try:yield db
            finally:db.close()
        def runtime_history(self,*args,**kwargs):
            self.history_calls+=1
            return super().runtime_history(*args,**kwargs)

    def timeout(*args):raise TimeoutError('read-only queue diagnostic exceeded 60 seconds')
    signal.signal(signal.SIGALRM,timeout)
    signal.alarm(60)
    config=load_bot(ROOT/'bot.json')
    store=ReadOnlyStore(Path(config['state_dir'])/'state.sqlite3')
    start=time.perf_counter()
    body=queue_text(store,True,load_ui(config['_ui_dir']))
    elapsed=time.perf_counter()-start
    result={'measured_at':datetime.datetime.now().astimezone().isoformat(),
            'queue_text_seconds':round(elapsed,6),'runtime_history_calls':store.history_calls,
            'sqlite_connections':store.connections,'text_characters':len(body),
            'sendMessage_parts_before_macro_summary':len(list(chunks(body))),
            'notes':['Only queue_text measured; no catalog traversal, macro summary, dispatcher or Telegram transport.',
                     'Read-only SQL against current production data. No message was sent.']}
    Path('/tmp/cfd-architecture-review/queue-readonly-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
