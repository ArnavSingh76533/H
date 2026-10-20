"""Offline behavioral tests; no Telegram credentials or network required."""
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from telethon import errors

from bot import BotService, Checker, Store, VALID, normalize


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite3")
        self.names = ["".join(c) + "ai" for c in itertools.islice(
            itertools.product("abcdefghijklmnopqrstuvwxyz", repeat=3), 173)]
        self.file = self.root / "names.txt"
        self.file.write_text("\n".join(self.names), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_all_random_batches_no_repeats_across_restart_or_upload(self):
        self.assertEqual(self.store.import_file(1, self.file), (173, 0, 0))
        seen = []
        sizes = []
        while names := self.store.next_names(1):
            sizes.append(len(names))
            batch = self.store.new_batch(1)
            for name in names:
                attempt = self.store.begin_attempt(batch, 1, name)
                self.store.finish(attempt, "occupied")
            seen.extend(names)
            self.store = Store(self.root / "state.sqlite3")
            self.store.recover()
        self.assertEqual(sizes, [50, 50, 50, 23])
        self.assertEqual(len(set(seen)), 173)
        self.assertEqual(set(seen), set(self.names))
        self.assertEqual(self.store.import_file(1, self.file), (0, 173, 0))
        self.assertEqual(self.store.stats(1), (173, 173, 0))
        self.assertEqual(self.store.import_file(2, self.file), (173, 0, 0))

    def test_dedupe_invalid_and_links(self):
        self.file.write_text("@Hello_AI HELLO_AI https://t.me/hello_ai\n"
                             "12345 abcd a-bcd abcde_", encoding="utf-8")
        self.assertEqual(self.store.import_file(1, self.file), (2, 2, 3))
        self.assertEqual(set(self.store.next_names(1)), {"hello_ai", "abcde_"})

    def test_corrupt_file_rolls_back_import(self):
        self.file.write_bytes(b"validname\n\xff")
        with self.assertRaises(UnicodeDecodeError):
            self.store.import_file(1, self.file)
        self.assertEqual(self.store.stats(1), (0, 0, 0))

    def test_crash_consumes_only_started_name_and_records_unknown(self):
        self.store.import_file(1, self.file)
        name = self.store.next_names(1)[0]
        batch = self.store.new_batch(1)
        self.store.begin_attempt(batch, 1, name)
        self.store.recover()
        self.assertEqual(self.store.results(batch)[0], [(name, "interrupted")])
        self.assertEqual(self.store.stats(1), (173, 1, 172))
        self.assertNotIn(name, self.store.next_names(1))

    def test_manual_check_does_not_reappear_in_later_files(self):
        batch = self.store.new_batch(1)
        attempt = self.store.begin_attempt(batch, 1, self.names[0])
        self.store.finish(attempt, "available")
        self.assertEqual(self.store.import_file(1, self.file), (172, 1, 0))


class CheckerTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_api_classification(self):
        cases = [
            (True, "available"), (False, "unavailable"),
            (errors.UsernameOccupiedError(None), "occupied"),
            (errors.UsernameInvalidError(None), "invalid"),
            (errors.UsernamePurchaseAvailableError(None), "purchase"),
            (OSError("offline"), "unknown"),
        ]
        for answer, expected in cases:
            with self.subTest(expected=expected):
                client = AsyncMock()
                if isinstance(answer, Exception):
                    client.side_effect = answer
                else:
                    client.return_value = answer
                self.assertEqual(await Checker(client, 0).check("hello_ai"), expected)

    async def test_invalid_input_makes_no_request(self):
        client = AsyncMock()
        for name in ("abcd", "12345", "héllo", "a-bcd", "a" * 33):
            self.assertEqual(await Checker(client, 0).check(name), "invalid")
        client.assert_not_called()

    async def test_flood_wait_preserves_remaining_and_cooldown_across_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            file = root / "names.txt"
            file.write_text("hello_ai world_ai other_ai")
            store = Store(root / "state.sqlite3")
            store.import_file(1, file)
            client = AsyncMock(side_effect=[True, errors.FloodWaitError(None, capture=60)])
            bot = AsyncMock()
            service = BotService(bot, Checker(client, 0), store, {1}, root)
            event = type("Event", (), {"sender_id": 1, "chat_id": 1})()
            await service.run_batch(event)
            self.assertEqual(store.stats(1), (3, 1, 2))
            batch = store.last_batch(1)
            self.assertEqual(len(store.results(batch)[0]), 1)
            self.assertIn("rate limit", store.results(batch)[1])
            checker = Checker(AsyncMock(), 2)
            BotService(bot, checker, store, {1}, root)
            import time
            self.assertGreater(checker.next_request_at, time.time() + 55)

    async def test_service_checks_and_reports_50_with_next_button(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            file = root / "names.txt"
            file.write_text(" ".join(f"name{i:03d}" for i in range(75)))
            store = Store(root / "state.sqlite3")
            store.import_file(1, file)
            client, bot = AsyncMock(return_value=True), AsyncMock()
            service = BotService(bot, Checker(client, 0), store, {1}, root)
            event = type("Event", (), {"sender_id": 1, "chat_id": 1})()
            await service.run_batch(event)
            self.assertEqual(client.await_count, 50)
            self.assertEqual(store.stats(1), (75, 50, 25))
            self.assertIsNotNone(bot.send_message.call_args.kwargs["buttons"])
            self.assertIn("50 result(s)", bot.send_message.call_args.args[1])

    async def test_unauthorized_and_group_updates_cannot_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bot, client = AsyncMock(), AsyncMock()
            service = BotService(bot, Checker(client, 0), Store(root / "db"), {1}, root)
            event = type("Event", (), {"sender_id": 2, "chat_id": 2, "is_private": True})()
            await service.handle(event)
            event.is_private = False
            event.sender_id = 1
            await service.handle(event)
            client.assert_not_called()
            self.assertEqual(bot.send_message.await_count, 1)

    async def test_next_button_uses_saved_queue_without_repeating(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            file = root / "names.txt"
            file.write_text(" ".join(f"name{i:03d}" for i in range(75)))
            store = Store(root / "db")
            store.import_file(1, file)
            client, bot = AsyncMock(return_value=True), AsyncMock()
            service = BotService(bot, Checker(client, 0), store, {1}, root)
            event = type("Event", (), {"sender_id": 1, "chat_id": 1,
                         "is_private": True, "data": b"next50", "answer": AsyncMock()})()
            await service.callback(event)
            await service.callback(event)
            sent = [call.args[0].username for call in client.await_args_list]
            self.assertEqual(len(sent), 75)
            self.assertEqual(len(set(sent)), 75)
            self.assertEqual(event.answer.await_count, 2)
            self.assertEqual(store.stats(1), (75, 75, 0))

    async def test_check_command_and_reply_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client, bot = AsyncMock(return_value=True), AsyncMock()
            service = BotService(bot, Checker(client, 0), Store(root / "db"), {1}, root)
            event = type("Event", (), {"sender_id": 1, "chat_id": 1,
                         "is_private": True, "raw_text": "/check@MyBot @Hello_AI",
                         "document": None, "is_reply": False})()
            await service.handle(event)
            event.raw_text = "/check"
            event.is_reply = True
            reply = type("Reply", (), {"document": None, "raw_text": "@WORLD_AI"})()
            event.get_reply_message = AsyncMock(return_value=reply)
            await service.handle(event)
            self.assertEqual([c.args[0].username for c in client.await_args_list],
                             ["hello_ai", "world_ai"])


if __name__ == "__main__":
    unittest.main()
