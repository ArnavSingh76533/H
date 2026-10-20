"""Login behavior tests; no account, phone number, or external network needed."""
import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, types
from telethon.tl.custom.qrlogin import QRLogin

from login_flow import code_login, login_error, login_user, qr_login, render_qr


def sent_code(code_hash="first", timeout=0, next_type=None):
    return types.auth.SentCode(type=types.auth.SentCodeTypeApp(length=5),
                              phone_code_hash=code_hash, timeout=timeout, next_type=next_type)


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.temp.cleanup()

    def client(self):
        client = AsyncMock()
        client.is_user_authorized.return_value = False
        client.get_me.return_value = SimpleNamespace(id=42, bot=False)
        client.send_code_request.return_value = sent_code()
        return client

    async def test_qr_waiter_starts_before_qr_is_displayed(self):
        waiting = False

        async def wait():
            nonlocal waiting
            waiting = True
            return "authorized"

        def display(url, path):
            self.assertTrue(waiting)
            path.write_text("temporary token")

        qr = SimpleNamespace(url="tg://login?token=test", wait=wait)
        client = self.client()
        client.qr_login.return_value = qr
        with patch("login_flow.render_qr", side_effect=display):
            self.assertEqual(await qr_login(client, self.root), "authorized")
        self.assertFalse((self.root / "login-qr.svg").exists())

    async def test_qr_expiration_refreshes_then_completes(self):
        qr = SimpleNamespace(url="tg://login?token=test", recreate=AsyncMock(),
                             wait=AsyncMock(side_effect=[asyncio.TimeoutError(), "user"]))
        client = self.client()
        client.qr_login.return_value = qr
        with patch("login_flow.render_qr") as render:
            self.assertEqual(await qr_login(client, self.root), "user")
            self.assertEqual(render.call_count, 2)
        qr.recreate.assert_awaited_once()

    async def test_qr_timeout_has_bounded_refreshes_and_cleanup(self):
        qr = SimpleNamespace(url="tg://login?token=test", recreate=AsyncMock(),
                             wait=AsyncMock(side_effect=asyncio.TimeoutError()))
        client = self.client()
        client.qr_login.return_value = qr
        with patch("login_flow.render_qr"), self.assertRaisesRegex(RuntimeError, "timed out"):
            await qr_login(client, self.root, max_refreshes=2)
        self.assertEqual(qr.wait.await_count, 2)
        qr.recreate.assert_awaited_once()

    async def test_qr_cancellation_removes_local_token_image(self):
        started = asyncio.Event()

        async def wait():
            started.set()
            await asyncio.Event().wait()

        qr = SimpleNamespace(url="tg://login?token=test", wait=wait)
        client = self.client()
        client.qr_login.return_value = qr

        def display(url, path):
            path.write_text("temporary token")

        with patch("login_flow.render_qr", side_effect=display):
            task = asyncio.create_task(qr_login(client, self.root))
            await started.wait()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse((self.root / "login-qr.svg").exists())

    async def test_qr_supports_two_step_verification_and_password_retry(self):
        qr = SimpleNamespace(url="tg://login?token=test",
                             wait=AsyncMock(side_effect=errors.SessionPasswordNeededError(None)))
        client = self.client()
        client.qr_login.return_value = qr
        client.sign_in.side_effect = [errors.PasswordHashInvalidError(None), "user"]
        with patch("login_flow.render_qr"), patch("login_flow.ask", new=AsyncMock(side_effect=["wrong", "correct"])):
            self.assertEqual(await qr_login(client, self.root), "user")
        self.assertEqual(client.sign_in.await_count, 2)

    async def test_code_login_shows_delivery_and_passes_hash(self):
        client = self.client()
        with patch("login_flow.ask", new=AsyncMock(side_effect=["+1234567890", "12345"])):
            await code_login(client, self.root)
        client.sign_in.assert_awaited_once_with(phone="+1234567890", code="12345", phone_code_hash="first")
        self.assertIn("service chat", self.output.getvalue())

    async def test_missing_code_can_switch_to_qr_without_another_send(self):
        client = self.client()
        with patch("login_flow.ask", new=AsyncMock(side_effect=["+1234567890", "", "qr"])), \
             patch("login_flow.qr_login", new=AsyncMock(return_value="user")) as qr:
            self.assertEqual(await code_login(client, self.root), "user")
        client.send_code_request.assert_awaited_once()
        client.sign_in.assert_not_awaited()
        qr.assert_awaited_once_with(client, self.root)

    async def test_resend_respects_telegram_timer(self):
        client = self.client()
        client.send_code_request.return_value = sent_code(timeout=60, next_type=types.auth.CodeTypeSms())
        with patch("login_flow.ask", new=AsyncMock(side_effect=["+1234567890", "resend", "qr"])), \
             patch("login_flow.qr_login", new=AsyncMock()):
            await code_login(client, self.root)
        client.assert_not_awaited()
        self.assertIn("requires waiting", self.output.getvalue())

    async def test_resend_uses_new_hash_when_telegram_allows_it(self):
        client = self.client()
        client.send_code_request.return_value = sent_code(timeout=0, next_type=types.auth.CodeTypeSms())
        client.return_value = sent_code(code_hash="second")
        with patch("login_flow.ask", new=AsyncMock(side_effect=["+1234567890", "resend", "12345"])):
            await code_login(client, self.root)
        client.assert_awaited_once()
        client.sign_in.assert_awaited_once_with(phone="+1234567890", code="12345", phone_code_hash="second")

    async def test_expired_code_falls_back_to_qr(self):
        client = self.client()
        client.sign_in.side_effect = errors.PhoneCodeExpiredError(None)
        with patch("login_flow.ask", new=AsyncMock(side_effect=["+1234567890", "12345"])), \
             patch("login_flow.qr_login", new=AsyncMock(return_value="user")) as qr:
            self.assertEqual(await code_login(client, self.root), "user")
        qr.assert_awaited_once()

    async def test_login_reuses_existing_session(self):
        client = self.client()
        client.is_user_authorized.return_value = True
        await login_user(client, self.root)
        client.qr_login.assert_not_awaited()
        client.send_code_request.assert_not_awaited()
        self.assertIn("verified and saved", self.output.getvalue())

    async def test_bot_session_is_rejected(self):
        client = self.client()
        client.is_user_authorized.return_value = True
        client.get_me.return_value = SimpleNamespace(id=42, bot=True)
        with self.assertRaisesRegex(RuntimeError, "USER account"):
            await login_user(client, self.root)

    async def test_login_flood_wait_is_reported_without_automatic_retry(self):
        client = self.client()
        client.send_code_request.side_effect = errors.FloodWaitError(None, capture=77)
        with patch("login_flow.ask", new=AsyncMock(return_value="+1234567890")), \
             self.assertRaisesRegex(RuntimeError, "wait 77 seconds"):
            await login_user(client, self.root, method="code")
        client.send_code_request.assert_awaited_once()
        client.qr_login.assert_not_awaited()

    async def test_real_telethon_qr_wait_handles_scan_update(self):
        # Exercise Telethon's real QRLogin.wait, not a stub of its handshake.
        user = types.User(id=42, first_name="Test")
        success = types.auth.LoginTokenSuccess(authorization=types.auth.Authorization(user=user))

        class LocalClient:
            api_id, api_hash = 123, "local-test"

            def __init__(self):
                self.handlers = []
                self.calls = 0
                self._on_login = AsyncMock()

            async def __call__(self, request):
                self.calls += 1
                if self.calls == 1:
                    return types.auth.LoginToken(expires=datetime.now(timezone.utc) + timedelta(seconds=10), token=b"local-test")
                return success

            def add_event_handler(self, handler, event):
                self.handlers.append(handler)

            def remove_event_handler(self, handler):
                self.handlers.remove(handler)

        local = LocalClient()
        qr = QRLogin(local, [])
        await qr.recreate()
        local.qr_login = AsyncMock(return_value=qr)
        scan_tasks = []

        def display(url, path):
            self.assertEqual(len(local.handlers), 1)
            scan_tasks.append(asyncio.create_task(local.handlers[0](types.UpdateLoginToken())))

        with patch("login_flow.render_qr", side_effect=display):
            result = await qr_login(local, self.root)
        await asyncio.gather(*scan_tasks)
        self.assertEqual(result.id, 42)
        local._on_login.assert_awaited_once_with(user)
        self.assertEqual(local.handlers, [])

    def test_actual_svg_qr_generation(self):
        path = self.root / "qr.svg"
        render_qr("tg://login?token=local-test", path)
        self.assertIn("<svg", path.read_text())


if __name__ == "__main__":
    unittest.main()
