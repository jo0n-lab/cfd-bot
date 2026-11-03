"""Block fake uploads while exercising real routing, receipts and /clean."""
import threading
from unittest.mock import patch

from cfd_bot.bot import Bot, case_id
from cfd_bot.telegram import TelegramError
from cfd_bot.telegram_receipts import TelegramReceipts
from cfd_bot.telegram_transfers import TelegramTransfers
from tests.test_core import Environment, FakeAPI


class TransferTests(Environment):
    def make_bot(self, workers=2, capacity=16):
        api = FakeAPI()
        receipts = TelegramReceipts(self.store)
        self.addCleanup(receipts.close)
        bot = Bot(self.config, self.store, api, receipts)
        bot.transfers = TelegramTransfers(api, bot._remember, bot.ui,
                                          workers=workers, capacity=capacity)
        self.addCleanup(bot.transfers.close)
        return bot, api

    def idle(self, transfers):
        with transfers._condition:
            self.assertTrue(transfers._condition.wait_for(lambda: not transfers._jobs, timeout=3))

    def block_first(self, api):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = api.file

        def upload(chat, item):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test upload was not released')
            return original(chat, item)
        api.file = upload
        return entered, release

    def update(self, action, number=1):
        return dict(update_id=number, callback_query=dict(id=str(number), data=action,
                    message=dict(chat=dict(id=20), message_id=number), **{'from': dict(id=10)}))

    def test_blocked_file_does_not_block_stat_next_checkpoint_or_other_file(self):
        self.case_data['residual_pattern'] = 'residual.png'
        self.write_case()
        (self.case_root / 'residual.png').write_bytes(b'image')
        bot, api = self.make_bot()
        entered, release = self.block_first(api)
        bot.handle(self.update('residual:' + case_id(self.case)))
        self.assertTrue(entered.wait(1))
        bot.receipts.checkpoint(2)
        with patch('cfd_bot.bot.process_snapshot', return_value=dict(cases={}, raw='')) as scan:
            bot.handle(self.update('status', 2))
        scan.assert_called_once()
        self.assertTrue(api.messages)
        bot.file(20, dict(path=str(self.case_root / 'shape.png'), kind='photo'))
        with bot.transfers._condition:
            self.assertTrue(bot.transfers._condition.wait_for(lambda: len(api.files) == 1, timeout=2))
        self.assertFalse(release.is_set())
        self.assertEqual(bot.receipts.offset, 2)
        release.set()
        self.idle(bot.transfers)
        bot.receipts.flush()
        self.assertEqual(len(api.files), 2)
        self.assertIn(102, self.store.chat_messages(20))

    def test_duplicates_are_bounded_and_pending_item_is_replaced(self):
        bot, api = self.make_bot(workers=1, capacity=2)
        entered, release = self.block_first(api)
        first = dict(path=str(self.case_root / 'residual.png'), kind='photo')
        second = dict(path=str(self.case_root / 'shape.png'), kind='photo', caption='old')
        self.assertTrue(bot.file(20, first))
        self.assertTrue(entered.wait(1))
        self.assertFalse(bot.file(20, first))
        self.assertTrue(bot.file(20, second))
        self.assertFalse(bot.file(20, dict(second, caption='latest')))
        with self.assertRaises(ValueError):
            bot.file(20, dict(path=str(self.case_root / 'third.png')))
        release.set()
        self.idle(bot.transfers)
        self.assertEqual(len(api.files), 2)
        self.assertEqual(api.files[1][1]['caption'], 'latest')
        self.assertTrue(bot.file(20, first))
        self.idle(bot.transfers)
        self.assertEqual(len(api.files), 3)

    def test_clean_cancels_pending_and_deletes_late_upload(self):
        bot, api = self.make_bot(workers=1)
        entered, release = self.block_first(api)
        bot.file(20, dict(path=str(self.case_root / 'active.png')))
        self.assertTrue(entered.wait(1))
        bot.file(20, dict(path=str(self.case_root / 'pending.png')))
        with patch.object(api, 'delete_messages', return_value=[101]) as delete:
            bot.handle(self.update('clean', 4))
            release.set()
            self.idle(bot.transfers)
        self.assertEqual(len(api.files), 1)
        self.assertIn((20, [101]), [c.args for c in delete.call_args_list])
        bot.receipts.flush()
        self.assertNotIn(101, self.store.chat_messages(20))
        self.assertTrue(bot.file(20, dict(path=str(self.case_root / 'active.png'))))
        self.idle(bot.transfers)
        self.assertEqual(len(api.files), 2)

    def test_failed_upload_reports_error_and_releases_slot_for_retry(self):
        bot, api = self.make_bot(workers=1, capacity=1)
        item = dict(path=str(self.case_root / 'residual.png'))
        with patch.object(api, 'file', side_effect=TelegramError('network timeout')), \
                self.assertLogs('cfd_bot.telegram_transfers', level='WARNING'):
            bot.file(20, item)
            self.idle(bot.transfers)
        self.assertIn('residual.png', api.messages[-1][1])
        self.assertTrue(bot.file(20, item))
        self.idle(bot.transfers)
        self.assertEqual(len(api.files), 1)

    def test_close_discards_waiting_files_and_rejects_new_submission(self):
        bot, api = self.make_bot(workers=1)
        entered, release = self.block_first(api)
        bot.file(20, dict(path=str(self.case_root / 'first.png')))
        self.assertTrue(entered.wait(1))
        bot.file(20, dict(path=str(self.case_root / 'pending.png')))
        bot.transfers.close(timeout=0)
        with self.assertRaises(ValueError):
            bot.file(20, dict(path=str(self.case_root / 'new.png')))
        release.set()
        self.idle(bot.transfers)
        self.assertEqual(len(api.files), 1)

    def test_queued_file_reuses_shared_path_guard_after_symlink_change(self):
        bot, api = self.make_bot(workers=1)
        entered, release = self.block_first(api)
        bot.file(20, dict(path=str(self.case_root / 'first.png')))
        self.assertTrue(entered.wait(1))
        path = self.case_root / 'pending.png'
        path.write_bytes(b'approved')
        bot.file(20, dict(path=str(path)), case=self.case)
        path.unlink()
        path.symlink_to(self.bot_path)
        with self.assertLogs('cfd_bot.telegram_transfers', level='WARNING'):
            release.set()
            self.idle(bot.transfers)
        self.assertEqual(len(api.files), 1)
        self.assertIn('pending.png', api.messages[-1][1])
