"""Public-page and mode integration tests. No live account or network needed."""
import asyncio
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from bot import BotService, Checker, Store, main
from web_checker import (MAX_HTML_BYTES, WebChecker, WebRateLimited,
                         fragment_state, retry_seconds, telegram_state)


# Representative HTML fixtures, not snapshots claimed to be fetched live.
def contact(name="testname"):
    return f'''<html><head><meta property="og:title" content="Telegram: Contact @{name}"></head>
    <body><div class="tgme_page_wrap"><div class="tgme_page_description">
    If you have <strong>Telegram</strong>, you can contact <a>@{name}</a> right away.
    </div></div></body></html>'''


def profile(name="testname"):
    return f'''<html><head><title>Telegram: Contact Test Person</title></head><body>
    <div class="tgme_page_wrap"><div class="tgme_page_title">Test Person</div>
    <a href="tg://resolve?domain={name}">Send Message</a></div></body></html>'''


def fragment(status="Unavailable", name="testname"):
    return f'''<html><head><title>{name} - Fragment</title></head><body>
    <div class="tm-section-header"><span class="tm-section-header-status">{status}</span></div>
    </body></html>'''


class ParserTests(unittest.TestCase):
    def test_exact_contact_sentence_is_candidate(self):
        self.assertEqual(telegram_state(contact(), "testname"), "contact")
        self.assertEqual(telegram_state(contact("FireByWorld92983838"), "firebyworld92983838"), "contact")

    def test_contact_matches_exact_username_not_prefix(self):
        self.assertEqual(telegram_state(contact("testname_extra"), "testname"), "unknown")

    def test_real_profile_and_generic_http_200_are_distinct(self):
        self.assertEqual(telegram_state(profile(), "testname"), "occupied")
        self.assertEqual(telegram_state("<html><title>Site Unavailable</title></html>", "testname"), "unknown")
        self.assertEqual(telegram_state("<html>Telegram: Contact @testname</html>", "testname"), "unknown")

    def test_profile_description_cannot_impersonate_missing_contact(self):
        html = profile().replace("</body>", contact() + "</body>")
        self.assertEqual(telegram_state(html, "testname"), "occupied")

    def test_script_only_contact_text_does_not_count(self):
        html = '<div class="tgme_page_wrap"><script>' + contact() + '</script></div>'
        self.assertEqual(telegram_state(html, "testname"), "unknown")

    def test_fragment_status_mapping_uses_exact_word(self):
        for label, expected in [("Unavailable", "not_listed"), ("Available", "purchase"),
                                ("On auction", "purchase"), ("For sale", "purchase"),
                                ("Taken", "fragment_taken"), ("Sold", "fragment_taken")]:
            with self.subTest(label=label):
                self.assertEqual(fragment_state(fragment(label), "testname"), expected)

    def test_generic_unavailable_or_different_asset_does_not_count(self):
        self.assertEqual(fragment_state("<html>Unavailable</html>", "testname"), "unknown")
        self.assertEqual(fragment_state(fragment(name="different"), "testname"), "unknown")
        self.assertEqual(fragment_state(fragment(name="testname_extra"), "testname"), "unknown")

    def test_blocked_fragment_and_conflicting_statuses_stay_unknown(self):
        html = fragment().replace("</body>", '<form id="challenge-form"></form></body>')
        self.assertEqual(fragment_state(html, "testname"), "unknown")
        html = fragment() + '<div class="tm-section-header-status">Available</div>'
        self.assertEqual(fragment_state(html, "testname"), "unknown")

    def test_retry_after_values(self):
        self.assertEqual(retry_seconds("30"), 30)
        self.assertEqual(retry_seconds(None), 60)
        self.assertEqual(retry_seconds("nonsense"), 60)
        self.assertEqual(retry_seconds("99999999999"), 86400)


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def check_using(self, handler, name="testname"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await WebChecker(0, client=client).check(name)

    async def test_contact_then_fragment_unavailable_is_only_likely_available(self):
        urls = []
        def handler(request):
            urls.append(str(request.url))
            html = contact() if request.url.host == "t.me" else fragment()
            return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})
        self.assertEqual(await self.check_using(handler), "likely_available")
        self.assertEqual(urls, ["https://t.me/testname", "https://fragment.com/username/testname"])

    async def test_taken_telegram_profile_does_not_request_fragment(self):
        urls = []
        def handler(request):
            urls.append(str(request.url))
            return httpx.Response(200, text=profile(), headers={"Content-Type": "text/html"})
        self.assertEqual(await self.check_using(handler), "occupied")
        self.assertEqual(len(urls), 1)

    async def test_fragment_purchase_and_taken_are_not_free(self):
        for label, expected in [("Available", "purchase"), ("Taken", "fragment_taken")]:
            def handler(request):
                html = contact() if request.url.host == "t.me" else fragment(label)
                return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})
            self.assertEqual(await self.check_using(handler), expected)

    async def test_http_errors_and_redirects_never_mean_available(self):
        for status in [301, 302, 403, 404, 500, 503]:
            for failure_host in ["t.me", "fragment.com"]:
                with self.subTest(status=status, host=failure_host):
                    def handler(request):
                        if request.url.host == failure_host:
                            return httpx.Response(status, text="Unavailable")
                        return httpx.Response(200, text=contact(), headers={"Content-Type": "text/html"})
                    self.assertEqual(await self.check_using(handler), "unknown")

    async def test_network_timeout_does_not_mean_available(self):
        def handler(request):
            raise httpx.ConnectTimeout("mock timeout", request=request)
        self.assertEqual(await self.check_using(handler), "unknown")

    async def test_rate_limit_exposes_retry_after_and_sets_cooldown(self):
        def handler(request):
            return httpx.Response(429, headers={"Retry-After": "90"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            checker = WebChecker(0, client=client)
            with self.assertRaises(WebRateLimited) as exc:
                await checker.check("testname")
            self.assertEqual(exc.exception.seconds, 90)
            self.assertGreater(checker.next_request_at, time.time() + 85)

    async def test_invalid_names_never_make_http_requests(self):
        handler = Mock(side_effect=AssertionError("HTTP must not be called"))
        for name in ["abcd", "@hello", "12345", "éabcd", "hello/path"]:
            self.assertEqual(await self.check_using(handler, name), "invalid")
        handler.assert_not_called()

    async def test_non_html_and_oversized_responses_are_unknown(self):
        for content, ctype in [("Unavailable", "application/json"), ("x" * (MAX_HTML_BYTES + 1), "text/html")]:
            def handler(request):
                return httpx.Response(200, content=content.encode(), headers={"Content-Type": ctype})
            self.assertEqual(await self.check_using(handler), "unknown")


def event(text="", user=1, **kwargs):
    return SimpleNamespace(sender_id=user, chat_id=user, is_private=True,
                           raw_text=text, document=None, is_reply=False, **kwargs)


class ModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "db")
        self.bot = AsyncMock()
        self.api_client = AsyncMock(return_value=True)
        self.web = SimpleNamespace(check=AsyncMock(return_value="likely_available"),
                                   delay=0, next_request_at=0)
        self.service = BotService(self.bot, Checker(self.api_client, 0), self.store,
                                  {1, 2}, self.root, web_checker=self.web)

    def tearDown(self):
        self.temp.cleanup()

    def queue(self, count=170):
        path = self.root / "list.txt"
        path.write_text(" ".join(f"name{i:03d}" for i in range(count)))
        self.store.import_file(1, path)

    async def test_botonly_activation_runs_80_and_next_button_without_user_api(self):
        self.queue()
        await self.service.handle(event("/botonly"))
        await self.service.handle(event("/next"))
        self.assertEqual(self.web.check.await_count, 80)
        self.api_client.assert_not_awaited()
        self.assertEqual(self.store.stats(1), (170, 80, 90))
        batch = self.store.last_batch(1)
        self.assertEqual(self.store.batch_mode(batch), "botonly")
        self.assertEqual(len(self.store.results(batch)[0]), 80)
        self.assertEqual(self.bot.send_message.call_args.kwargs["buttons"][0][0].text, "🎲 Next 80")
        for call in self.bot.send_message.await_args_list:
            self.assertLessEqual(len(call.args[1]), 3500)
        button = event(data=b"next", answer=AsyncMock())
        await self.service.callback(button)
        names = [c.args[0] for c in self.web.check.await_args_list]
        self.assertEqual(len(names), 160)
        self.assertEqual(len(set(names)), 160)

    async def test_check_80_direct_names_is_allowed_only_in_botonly_mode(self):
        names = " ".join(f"name{i:03d}" for i in range(80))
        await self.service.handle(event("/check " + names))
        self.api_client.assert_not_awaited()
        await self.service.handle(event("/botonly"))
        await self.service.handle(event("/check " + names))
        self.assertEqual(self.web.check.await_count, 80)

    async def test_single_check_uses_selected_checker(self):
        await self.service.handle(event("/botonly"))
        await self.service.handle(event("/check @testname"))
        self.web.check.assert_awaited_once_with("testname")
        self.api_client.assert_not_awaited()

    async def test_full_80_name_flow_runs_telegram_then_fragment_for_each_name(self):
        self.queue(90)
        urls = []
        def handler(request):
            urls.append(str(request.url))
            name = request.url.path.rsplit("/", 1)[-1]
            html = contact(name) if request.url.host == "t.me" else fragment(name=name)
            return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.service.web_checker = WebChecker(0, client=client)
            await self.service.handle(event("/botonly"))
            await self.service.handle(event("/next"))
        rows, _ = self.store.results(self.store.last_batch(1))
        self.assertEqual(len(rows), 80)
        self.assertEqual({status for _, status in rows}, {"likely_available"})
        self.assertEqual(len(urls), 160)
        self.assertTrue(all(urls[i].startswith("https://t.me/") and
                            urls[i+1].startswith("https://fragment.com/username/")
                            for i in range(0, 160, 2)))
        self.api_client.assert_not_awaited()

    async def test_switch_back_to_account_keeps_queue_and_history(self):
        self.queue()
        await self.service.handle(event("/botonly"))
        await self.service.handle(event("/next"))
        await self.service.handle(event("/accountmode"))
        await self.service.handle(event("/next"))
        self.assertEqual(self.api_client.await_count, 50)
        web_names = {c.args[0] for c in self.web.check.await_args_list}
        api_names = {c.args[0].username for c in self.api_client.await_args_list}
        self.assertFalse(web_names & api_names)
        self.assertEqual(self.store.stats(1), (170, 130, 40))

    async def test_mode_is_per_user_and_survives_restart(self):
        await self.service.handle(event("/botonly"))
        self.assertEqual(self.service.mode(1), "botonly")
        self.assertEqual(self.service.mode(2), "account")
        store = Store(self.root / "db")
        restarted = BotService(self.bot, None, store, {1}, self.root, web_checker=self.web)
        self.assertEqual(restarted.mode(1), "botonly")

    async def test_missing_account_does_not_prevent_botonly(self):
        loader = AsyncMock(side_effect=RuntimeError("No user session; use /botonly"))
        service = BotService(self.bot, None, self.store, {1}, self.root,
                             web_checker=self.web, account_loader=loader, default_mode="botonly")
        await service.handle(event("/botonly"))
        await service.handle(event("/check testname"))
        loader.assert_not_awaited()
        await service.handle(event("/accountmode"))
        self.assertEqual(service.mode(1), "botonly")
        loader.assert_awaited_once()

    async def test_web_rate_limit_is_persisted_separately(self):
        self.queue()
        self.web.next_request_at = 0
        async def limited(name):
            self.web.next_request_at = time.time() + 90
            raise WebRateLimited(90)
        self.web.check.side_effect = limited
        await self.service.handle(event("/botonly"))
        await self.service.handle(event("/next"))
        self.assertEqual(self.store.stats(1), (170, 0, 170))
        self.assertTrue((self.root / "web_cooldown.txt").exists())
        self.assertFalse((self.root / "cooldown.txt").exists())
        fresh = SimpleNamespace(check=AsyncMock(), delay=0, next_request_at=0)
        BotService(self.bot, None, self.store, {1}, self.root, web_checker=fresh)
        self.assertGreater(fresh.next_request_at, time.time() + 85)

    async def test_unknown_stops_batch_without_consuming_remaining_names(self):
        self.queue()
        self.web.check.return_value = "unknown"
        await self.service.handle(event("/botonly"))
        await self.service.handle(event("/next"))
        self.assertEqual(self.web.check.await_count, 1)
        self.assertEqual(self.store.stats(1), (170, 1, 169))

    async def test_upload_in_botonly_mode_triggers_80(self):
        await self.service.handle(event("/botonly"))
        content = "\n".join(f"name{i:03d}" for i in range(100))
        async def download(*, file):
            Path(file).write_text(content)
        message = SimpleNamespace(file=SimpleNamespace(name="names.txt", size=len(content)),
                                  download_media=download)
        await self.service.load_file(event(), message)
        self.assertEqual(self.web.check.await_count, 80)
        self.assertEqual(self.store.stats(1), (100, 80, 20))

    async def test_unauthorized_user_cannot_change_mode(self):
        await self.service.handle(event("/botonly", user=999))
        self.assertEqual(self.store.get_mode(999), "account")

    def test_old_database_migration_preserves_history(self):
        path = self.root / "legacy.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE batches (id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '')")
            db.execute("INSERT INTO batches(owner,note) VALUES(1,'old result')")
        upgraded = Store(path)
        self.assertEqual(upgraded.batch_mode(1), "account")
        self.assertEqual(upgraded.results(1), ([], "old result"))

    async def test_startup_without_user_session_never_constructs_user_client(self):
        bot_client = AsyncMock()
        bot_client.add_event_handler = Mock()
        bot_client.get_me.return_value = SimpleNamespace(bot=True, id=123, username="testbot")
        env = {"API_ID": "123", "API_HASH": "example", "BOT_TOKEN": "123:example",
               "ALLOWED_USER_IDS": "1", "DATA_DIR": str(self.root)}
        fake_web = Mock()
        fake_web.close = AsyncMock()
        fake_web.next_request_at = 0
        with patch.dict(os.environ, env, clear=True), patch("bot.TelegramClient", return_value=bot_client) as factory, \
             patch("bot.WebChecker", return_value=fake_web), redirect_stdout(io.StringIO()):
            await main(False)
        self.assertEqual(factory.call_count, 1)
        self.assertTrue(factory.call_args.args[0].endswith("checker_bot"))
        bot_client.start.assert_awaited_once_with(bot_token="123:example")
        self.assertFalse((self.root / "checker_user.session").exists())
        fake_web.close.assert_awaited_once()

    async def test_main_lazily_loads_account_checker_when_requested(self):
        bot_client = AsyncMock()
        bot_client.add_event_handler = Mock()
        bot_client.get_me.return_value = SimpleNamespace(bot=True, id=123, username="testbot")
        user_client = AsyncMock(return_value=True)
        user_client.is_user_authorized.return_value = True
        user_client.is_bot.return_value = False
        async def simulate_chat():
            handler = bot_client.add_event_handler.call_args_list[0].args[0]
            await handler(event("/accountmode"))
            await handler(event("/check testname"))
        bot_client.run_until_disconnected.side_effect = simulate_chat
        env = {"API_ID": "123", "API_HASH": "example", "BOT_TOKEN": "123:example",
               "ALLOWED_USER_IDS": "1", "DATA_DIR": str(self.root)}
        fake_web = Mock()
        fake_web.close = AsyncMock()
        fake_web.next_request_at = 0
        with patch.dict(os.environ, env, clear=True), \
             patch("bot.TelegramClient", side_effect=[bot_client, user_client]) as factory, \
             patch("bot.WebChecker", return_value=fake_web), redirect_stdout(io.StringIO()):
            await main(False)
        self.assertEqual(factory.call_count, 2)
        user_client.connect.assert_awaited_once()
        user_client.assert_awaited_once()
        self.assertEqual(user_client.await_args.args[0].username, "testname")
        self.assertEqual(self.store.get_mode(1), "account")
        user_client.start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
