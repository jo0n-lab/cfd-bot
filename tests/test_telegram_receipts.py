"""Real SQLite contention with fake Telegram; no network or solver execution."""
import json
import sqlite3
import threading
from unittest.mock import patch

from cfd_bot.bot import Bot, case_id
from cfd_bot import diagnostics
from cfd_bot.telegram import TelegramError
from cfd_bot.telegram_receipts import TelegramReceipts
from cfd_bot.ticket_chat import TicketChat
from tests.test_core import Environment, FakeAPI


class ReceiptTests(Environment):
    def receipts(self):
        receipts = TelegramReceipts(self.store)
        self.addCleanup(receipts.close)
        return receipts

    def update(self, mid=11, action='home'):
        return dict(update_id=mid, callback_query=dict(
            id='callback', data=action, message=dict(chat=dict(id=20), message_id=mid),
            **{'from': dict(id=10)}))

    def test_ack_starts_before_message_bookkeeping(self):
        api = FakeAPI()
        bot = Bot(self.config, self.store, api)
        ack = threading.Event()
        with patch.object(bot, 'acknowledge_callback', side_effect=lambda *a: ack.set()), \
                patch.object(self.store, 'remember_message', side_effect=lambda *a: self.assertTrue(ack.wait(1))):
            bot.handle(self.update())

    def test_locked_sqlite_does_not_block_residual_or_next_poll_checkpoint(self):
        self.case_data['residual_pattern'] = 'residual.png'
        self.write_case()
        (self.case_root / 'residual.png').write_bytes(b'fake image')
        receipts = self.receipts()
        api = FakeAPI()
        bot = Bot(self.config, self.store, api, receipts)
        attempted = threading.Event()
        finished = threading.Event()
        errors = []
        original = self.store.remember_messages

        def write(*args, **kwargs):
            attempted.set()
            return original(*args, **kwargs)

        def request():
            try:
                bot.handle(self.update(action='residual:' + case_id(self.case)))
                receipts.checkpoint(12)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        locker = sqlite3.connect(self.store.path)
        thread = threading.Thread(target=request, daemon=True)
        try:
            locker.execute('BEGIN IMMEDIATE')
            with patch.object(self.store, 'remember_messages', side_effect=write):
                receipts.remember_message(20, 9)
                self.assertTrue(attempted.wait(1))
                thread.start()
                self.assertTrue(finished.wait(1.5), 'Residual waited on the CFD database writer')
                self.assertFalse(errors)
                self.assertEqual(len(api.files), 1)
                self.assertEqual(receipts.offset, 12)
                self.assertEqual(self.store.chat_messages(20), [])
                self.assertIsNone(self.store.get('telegram_offset'))
                journal = json.loads(receipts.path.read_text())
                self.assertEqual({m[1] for m in journal['messages']}, {9, 11, 101})
                self.assertEqual(receipts.path.stat().st_mode & 0o777, 0o600)
        finally:
            locker.rollback()
            locker.close()
            if thread.ident is not None:
                thread.join(2)
        receipts.flush()
        self.assertEqual(self.store.chat_messages(20), [9, 11, 101])
        self.assertEqual(self.store.get('telegram_offset'), 12)

    def test_noneditor_requests_do_not_create_or_rewrite_idle_session(self):
        bot = Bot(self.config, self.store, FakeAPI())
        bot.ticket_ui = TicketChat(bot)
        key = bot.ticket_ui.key(20, 10)
        with patch.object(self.store, 'put', wraps=self.store.put) as put:
            bot.handle(self.update())
            put.assert_not_called()
        self.store.put(key, {'view': 'away', 'draft': {'name': 'retain me'}})
        with patch.object(self.store, 'put', wraps=self.store.put) as put:
            bot.handle(self.update())
            put.assert_not_called()
        self.assertEqual(self.store.get(key)['draft'], {'name': 'retain me'})

    def test_leaving_real_editor_still_cancels_pending_input(self):
        bot = Bot(self.config, self.store, FakeAPI())
        bot.ticket_ui = TicketChat(bot)
        key = bot.ticket_ui.key(20, 10)
        self.store.put(key, {'view': 'field', 'pending': 'name', 'draft': {'name': 'retain me'}})
        bot.handle(self.update())
        session = self.store.get(key)
        self.assertNotIn('pending', session)
        self.assertEqual(session['view'], 'away')
        self.assertEqual(session['draft'], {'name': 'retain me'})

    def test_failed_flush_recovers_ids_and_offset_after_restart(self):
        with patch.object(self.store, 'remember_messages', side_effect=sqlite3.OperationalError('database is locked')):
            receipts = self.receipts()
            receipts.remember_message(20, 7, 10)
            receipts.checkpoint(50)
            with self.assertLogs('cfd_bot.telegram_receipts', level='WARNING'):
                receipts.close()
        recovered = self.receipts()
        self.assertEqual(recovered.offset, 50)
        recovered.flush()
        self.assertEqual(self.store.chat_messages(20), [7])
        self.assertEqual(self.store.get('telegram_offset'), 50)
        # Replaying a journal written before a successful commit is idempotent.
        self.store.remember_messages([(20, 7, 11)], offset=49)
        self.assertEqual(self.store.get('telegram_offset'), 50)
        self.assertEqual(self.store.chat_messages(20, since=10.5), [])

    def test_clean_flushes_prior_ids_and_does_not_resurrect_deleted_messages(self):
        receipts = self.receipts()
        api = FakeAPI()
        bot = Bot(self.config, self.store, api, receipts)
        bot.handle(self.update())
        bot.handle(dict(update_id=12, message=dict(chat=dict(id=20), message_id=12,
                                                text='/clean', **{'from': dict(id=10)})))
        self.assertEqual(set(api.deleted[0][1]), {11, 12, 101})
        receipts.flush()
        self.assertEqual(self.store.chat_messages(20), [])
        # New messages sent during deletion remain tracked for the next /clean.
        receipts.remember_message(20, 13)
        with patch.object(api, 'delete_messages', side_effect=lambda *a: receipts.remember_message(20, 200)):
            bot.dispatch(20, 'clean', '13')
        receipts.flush()
        self.assertEqual(self.store.chat_messages(20), [200])

    def test_failed_delete_retains_records(self):
        receipts = self.receipts()
        bot = Bot(self.config, self.store, FakeAPI(), receipts)
        receipts.remember_message(20, 7)
        with patch.object(bot.api, 'delete_messages', side_effect=TelegramError('offline')):
            with self.assertRaises(TelegramError):
                bot.dispatch(20, 'clean', '13')
        self.assertEqual(self.store.chat_messages(20), [7])

    def test_batch_rollback_includes_checkpoint_and_retention_is_per_chat(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON chat_messages "
                       "WHEN NEW.message_id=11 BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.remember_messages([(20, 10, 1), (20, 11, 2)], offset=12)
        self.assertEqual(self.store.chat_messages(20), [])
        self.assertIsNone(self.store.get('telegram_offset'))
        self.store.remember_messages([(20, mid, mid) for mid in range(100, 1101)] + [(30, 1, 1)], offset=1200)
        self.assertEqual(self.store.chat_messages(20), list(range(101, 1101)))
        self.assertEqual(self.store.chat_messages(30), [1])

    def test_failed_journal_does_not_advance_polling_offset(self):
        receipts = self.receipts()
        with patch.object(receipts, '_journal', side_effect=OSError('no space')):
            with self.assertRaises(OSError):
                receipts.checkpoint(50)
        self.assertEqual(receipts.offset, 0)

    def test_basic_logs_distinguish_queued_receipts_from_db_commit(self):
        receipts = self.receipts()
        directory = self.root / 'diagnostics'
        diagnostics.configure({'diagnostic_logging': {'level': 'basic', 'directory': str(directory)}}, active=True)
        self.addCleanup(diagnostics.close)
        receipts.remember_message(20, 7)
        receipts.checkpoint(50)
        receipts.flush()
        diagnostics.flush()
        records = [r for path in directory.glob('*.jsonl*') for r in diagnostics.read_records(path)]
        events = {r['event'] for r in records}
        self.assertTrue({'telegram.receipts.queued', 'telegram.receipts.committed'} <= events)
        calls = {r.get('function') for r in records if r['event'] == 'function.return'}
        self.assertIn('telegram_receipts.TelegramReceipts.remember_message', calls)
        self.assertIn('storage.Store.remember_messages', calls)
