#!/usr/bin/env python3
"""Standalone username checker. Original plugin idea/credits: Charlie.

Supports public-page bot-only checks and official account checks, without
claiming names. Run with your bot token; user-account login is optional.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import logging
import os
import random
import re
import sqlite3
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from telethon import Button, TelegramClient, errors, events, functions

from login_flow import login_error, login_user
from web_checker import WebChecker, WebRateLimited

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
LOG = logging.getLogger("username_checker")
BATCH_SIZE = 50
WEB_BATCH_SIZE = 80
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOKENS = 1_000_000
CREDITS = "Credits: Charlie | Telegram Username Checker"
VALID = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z", re.ASCII)
SPLIT = re.compile(r"[\s,;]+")
STATUSES = {
    "available": "✅ available",
    "likely_available": "🟢 likely available (web)",
    "fragment_taken": "💎 collectible / unavailable",
    "occupied": "❌ taken",
    "purchase": "💎 purchase on Fragment",
    "unavailable": "❌ unavailable for this account",
    "invalid": "⚠ invalid / not assignable",
    "unknown": "❓ unknown (request failed)",
    "interrupted": "❓ interrupted (not rechecked)",
}


def normalize(token: str) -> str:
    token = token.strip().strip("\ufeff")
    token = re.sub(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/", "", token,
                   flags=re.IGNORECASE)
    return token.removesuffix("/").removeprefix("@").lower()


def tokens_from_file(path: Path):
    # Strict decoding prevents quietly changing a corrupt username.
    count = 0
    with path.open("r", encoding="utf-8-sig") as source:
        for line in source:
            for token in SPLIT.split(line.strip()):
                if token:
                    count += 1
                    if count > MAX_TOKENS:
                        raise ValueError(f"File exceeds {MAX_TOKENS:,} entries.")
                    yield normalize(token)


class Store:
    """SQLite queue + durable history. One connection per method/thread."""

    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS names (
                    owner INTEGER NOT NULL, username TEXT NOT NULL,
                    random_key REAL NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner, username));
                CREATE INDEX IF NOT EXISTS queue_idx
                    ON names(owner, done, random_key);
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY, owner INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL,
                    owner INTEGER NOT NULL, username TEXT NOT NULL,
                    status TEXT NOT NULL, checked_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS history_idx
                    ON attempts(owner, username);
                CREATE TABLE IF NOT EXISTS preferences (
                    owner INTEGER PRIMARY KEY, mode TEXT NOT NULL);
            """)
            if "mode" not in {r[1] for r in db.execute("PRAGMA table_info(batches)")}:
                db.execute("ALTER TABLE batches ADD COLUMN mode TEXT NOT NULL DEFAULT 'account'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        # Committed before a request: after a crash its result is unknowable.
        # Keep these consumed instead of silently checking them a second time.
        with self.connect() as db:
            db.execute("UPDATE attempts SET status='interrupted' WHERE status='running'")

    def import_file(self, owner: int, path: Path) -> tuple[int, int, int]:
        added = duplicate = invalid = 0
        with self.connect() as db:
            for name in tokens_from_file(path):
                if not VALID.fullmatch(name):
                    invalid += 1
                    continue
                exists = db.execute(
                    "SELECT 1 FROM attempts WHERE owner=? AND username=? LIMIT 1",
                    (owner, name)).fetchone()
                if exists:
                    duplicate += 1
                    continue
                cursor = db.execute(
                    "INSERT OR IGNORE INTO names(owner,username,random_key) VALUES(?,?,?)",
                    (owner, name, random.random()))
                if cursor.rowcount:
                    added += 1
                else:
                    duplicate += 1
        return added, duplicate, invalid

    def next_names(self, owner: int, limit: int = BATCH_SIZE) -> list[str]:
        if not 1 <= limit <= WEB_BATCH_SIZE:
            raise ValueError("Invalid batch size")
        with self.connect() as db:
            return [r[0] for r in db.execute(
                "SELECT username FROM names WHERE owner=? AND done=0 "
                "ORDER BY random_key LIMIT ?", (owner, limit))]

    def new_batch(self, owner: int, mode: str = "account") -> int:
        with self.connect() as db:
            return db.execute("INSERT INTO batches(owner,mode) VALUES(?,?)", (owner, mode)).lastrowid

    def batch_mode(self, batch: int) -> str:
        with self.connect() as db:
            return db.execute("SELECT mode FROM batches WHERE id=?", (batch,)).fetchone()[0]

    def get_mode(self, owner: int, default: str = "account") -> str:
        with self.connect() as db:
            row = db.execute("SELECT mode FROM preferences WHERE owner=?", (owner,)).fetchone()
            return row[0] if row and row[0] in {"account", "botonly"} else default

    def set_mode(self, owner: int, mode: str):
        if mode not in {"account", "botonly"}:
            raise ValueError("Invalid check mode")
        with self.connect() as db:
            db.execute("INSERT INTO preferences(owner,mode) VALUES(?,?) "
                       "ON CONFLICT(owner) DO UPDATE SET mode=excluded.mode", (owner, mode))

    def begin_attempt(self, batch: int, owner: int, name: str) -> int:
        with self.connect() as db:
            db.execute("UPDATE names SET done=1 WHERE owner=? AND username=?", (owner, name))
            return db.execute(
                "INSERT INTO attempts(batch,owner,username,status,checked_at) VALUES(?,?,?,?,?)",
                (batch, owner, name, "running", int(time.time()))).lastrowid

    def finish(self, attempt: int, status: str):
        with self.connect() as db:
            db.execute("UPDATE attempts SET status=?,checked_at=? WHERE id=?",
                       (status, int(time.time()), attempt))

    def undo_rejected(self, attempt: int, owner: int, name: str):
        # FLOOD_WAIT rejects the request, so no availability result was obtained.
        with self.connect() as db:
            db.execute("DELETE FROM attempts WHERE id=?", (attempt,))
            has_history = db.execute(
                "SELECT 1 FROM attempts WHERE owner=? AND username=? LIMIT 1",
                (owner, name)).fetchone()
            if not has_history:
                db.execute("UPDATE names SET done=0 WHERE owner=? AND username=?", (owner, name))

    def set_note(self, batch: int, note: str):
        with self.connect() as db:
            db.execute("UPDATE batches SET note=? WHERE id=?", (note, batch))

    def last_batch(self, owner: int):
        with self.connect() as db:
            row = db.execute("SELECT id FROM batches WHERE owner=? ORDER BY id DESC LIMIT 1",
                             (owner,)).fetchone()
            return row[0] if row else None

    def results(self, batch: int):
        with self.connect() as db:
            rows = db.execute("SELECT username,status FROM attempts WHERE batch=? ORDER BY id",
                              (batch,)).fetchall()
            note = db.execute("SELECT note FROM batches WHERE id=?", (batch,)).fetchone()[0]
            return rows, note

    def stats(self, owner: int):
        with self.connect() as db:
            total, done = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(done),0) FROM names WHERE owner=?", (owner,)).fetchone()
            return total, done, total - done


class RateLimited(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds


class Checker:
    def __init__(self, client, delay: float):
        self.client = client
        self.delay = delay
        self.next_request_at = 0.0

    async def check(self, username: str) -> str:
        if not VALID.fullmatch(username):
            return "invalid"
        await asyncio.sleep(max(0, self.next_request_at - time.time()))
        self.next_request_at = time.time() + self.delay
        try:
            available = await asyncio.wait_for(
                self.client(functions.account.CheckUsernameRequest(username)), timeout=30)
            return "available" if available else "unavailable"
        except errors.UsernameOccupiedError:
            return "occupied"
        except errors.UsernameInvalidError:
            return "invalid"
        except errors.UsernamePurchaseAvailableError:
            return "purchase"
        except errors.FloodWaitError as exc:
            # Shared across all chats, and persisted by BotService.
            self.next_request_at = time.time() + exc.seconds + 1
            raise RateLimited(exc.seconds + 1) from exc
        except errors.RPCError as exc:
            if getattr(exc, "message", "") == "USERNAME_PURCHASE_AVAILABLE":
                return "purchase"
            LOG.warning("Check failed: %s", type(exc).__name__)
            return "unknown"
        except (OSError, asyncio.TimeoutError):
            LOG.warning("Network error during check")
            return "unknown"


class BotService:
    def __init__(self, bot, checker: Checker | None, store: Store, allowed: set[int], data_dir: Path,
                 *, web_checker=None, account_loader=None, default_mode="account"):
        self.bot, self.checker, self.store = bot, checker, store
        self.allowed, self.data_dir = allowed, data_dir
        self.web_checker, self.account_loader = web_checker, account_loader
        self.default_mode = default_mode
        self.job_lock = asyncio.Lock()
        self.cooldown_file = data_dir / "cooldown.txt"
        self.web_cooldown_file = data_dir / "web_cooldown.txt"
        self.restore_cooldown(checker, self.cooldown_file)
        self.restore_cooldown(web_checker, self.web_cooldown_file)

    @staticmethod
    def restore_cooldown(checker, path: Path):
        if checker is not None and path.exists():
            try:
                checker.next_request_at = max(checker.next_request_at,
                                             float(path.read_text()))
            except ValueError:
                raise RuntimeError(f"Invalid {path.name}; inspect it before restarting.")

    def mode(self, owner: int) -> str:
        return self.store.get_mode(owner, self.default_mode)

    def batch_size(self, owner: int) -> int:
        return WEB_BATCH_SIZE if self.mode(owner) == "botonly" else BATCH_SIZE

    async def get_checker(self, mode: str):
        if mode == "botonly":
            if self.web_checker is None:
                raise RuntimeError("Public-page checker is not configured.")
            return self.web_checker
        if self.checker is None:
            if self.account_loader is None:
                raise RuntimeError("No user session. Run python bot.py --login in your terminal, "
                                   "or send /botonly to check without a user login.")
            self.checker = await self.account_loader()
            self.restore_cooldown(self.checker, self.cooldown_file)
        return self.checker

    async def send(self, event, text: str, *, next_button=False):
        # Plain text avoids username underscores being parsed as formatting.
        lines = (text + "\n\n" + CREDITS).splitlines()
        chunks, chunk = [], ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 3500:
                chunks.append(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            chunks.append(chunk)
        for i, part in enumerate(chunks):
            size = self.batch_size(event.sender_id)
            buttons = [[Button.inline(f"🎲 Next {size}", b"next")]] if next_button and i == len(chunks)-1 else None
            await self.bot.send_message(event.chat_id, part, parse_mode=None,
                                        link_preview=False, buttons=buttons)

    async def report(self, event, batch: int):
        rows, note = self.store.results(batch)
        lines = [f"Batch #{batch} — {len(rows)} result(s)"]
        mode = self.store.batch_mode(batch)
        lines.append("Mode: bot-only (t.me + Fragment)" if mode == "botonly" else "Mode: account (official API)")
        lines.extend(f"{i}. @{name} — {STATUSES.get(status, status)}"
                     for i, (name, status) in enumerate(rows, 1))
        if note:
            lines.append(note)
        if mode == "botonly":
            lines.append("Web availability is an estimate, not a guarantee of registration.")
        _, done, left = self.store.stats(event.sender_id)
        lines.append(f"File queue: {done:,} consumed | {left:,} remaining")
        await self.send(event, "\n".join(lines), next_button=left > 0)

    async def run_batch(self, event, names: list[str] | None = None):
        # Called only while job_lock is held. Names not yet attempted stay pending.
        mode = self.mode(event.sender_id)
        try:
            checker = await self.get_checker(mode)
        except RuntimeError as exc:
            await self.send(event, str(exc))
            return
        wait = int(checker.next_request_at - time.time())
        if wait > checker.delay + 1:
            await self.send(event, f"{mode} cooldown: try again in {wait + 1} seconds.", next_button=True)
            return
        names = names if names is not None else self.store.next_names(event.sender_id, self.batch_size(event.sender_id))
        if not names:
            await self.send(event, "No unchecked usernames left. Upload another .txt file.")
            return
        batch = self.store.new_batch(event.sender_id, mode)
        await self.send(event, f"Checking {len(names)} username(s) in {mode} mode. Results will follow as one batch.")
        for name in names:
            attempt = self.store.begin_attempt(batch, event.sender_id, name)
            try:
                status = await checker.check(name)
            except (RateLimited, WebRateLimited) as exc:
                self.store.undo_rejected(attempt, event.sender_id, name)
                cooldown = self.web_cooldown_file if mode == "botonly" else self.cooldown_file
                cooldown.write_text(str(checker.next_request_at))
                self.store.set_note(batch, f"{mode} rate limit: retry in {exc.seconds} seconds. "
                                    "Unattempted file entries remain in the queue.")
                break
            except asyncio.CancelledError:
                self.store.finish(attempt, "interrupted")
                raise
            except Exception as exc:
                LOG.warning("Unexpected check failure: %s", type(exc).__name__)
                self.store.finish(attempt, "unknown")
                self.store.set_note(batch, "Checker error; batch stopped. See server logs.")
                break
            self.store.finish(attempt, status)
            if status == "unknown":
                self.store.set_note(batch, "Request failed, access was blocked, or the page was unrecognized; "
                                    "batch stopped. Remaining entries were preserved.")
                break
        await self.report(event, batch)

    async def load_file(self, event, message):
        if not message.file or not (message.file.name or "").lower().endswith(".txt"):
            await self.send(event, "Upload a UTF-8 .txt file containing usernames.")
            return
        if message.file.size is None or message.file.size > MAX_FILE_BYTES:
            await self.send(event, "File too large: maximum 20 MiB.")
            return
        # Never trust an uploaded filename as a filesystem path.
        with tempfile.TemporaryDirectory(dir=self.data_dir) as temp:
            path = Path(temp) / "upload.txt"
            await message.download_media(file=str(path))
            if not path.exists() or path.stat().st_size > MAX_FILE_BYTES:
                await self.send(event, "Download failed or file exceeds 20 MiB.")
                return
            try:
                added, duplicate, invalid = await asyncio.to_thread(
                    self.store.import_file, event.sender_id, path)
            except (UnicodeError, ValueError):
                await self.send(event, "Import cancelled: use UTF-8 text with at most 1,000,000 entries.")
                return
        await self.send(event, f"Added: {added:,}\nDuplicates/already attempted: {duplicate:,}"
                        f"\nInvalid entries skipped: {invalid:,}\nNew names join your saved random queue.")
        await self.run_batch(event)

    async def handle(self, event):
        if not event.is_private:
            return
        if event.sender_id not in self.allowed:
            await self.send(event, "This is a private bot. Your Telegram ID is " + str(event.sender_id))
            return
        text = (event.raw_text or "").strip()
        parts = text.split(maxsplit=1)
        command = parts[0].split("@")[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        if command in ("/start", "/help"):
            size = self.batch_size(event.sender_id)
            await self.send(event, f"Current mode: {self.mode(event.sender_id)}\n"
                            "/botonly → t.me + Fragment, 80 names, no user login\n"
                            "/accountmode → official API, 50 names, user session required\n"
                            f"Send a .txt file → checks {size} random usernames.\n"
                            f"/next → next {size} without repeating\n"
                            f"/check username → check one (or up to {size})\n"
                            "Reply /check to a text message or .txt file.\n"
                            "/status → queue counts\n/last → resend latest results\n"
                            "Plain @username or t.me/username also works.\n"
                            "History survives restart and re-upload. Manual /check can explicitly recheck a name.")
            return
        if command == "/status":
            total, done, left = self.store.stats(event.sender_id)
            await self.send(event, f"Mode: {self.mode(event.sender_id)} | batch: {self.batch_size(event.sender_id)}\n"
                            f"File queue: {total:,}\nConsumed: {done:,}\nRemaining: {left:,}", next_button=left > 0)
            return
        if command == "/last":
            batch = self.store.last_batch(event.sender_id)
            if batch is None:
                await self.send(event, "No results yet.")
            else:
                await self.report(event, batch)
            return
        if self.job_lock.locked():
            await self.send(event, "A check/import is already running. Wait for its results.")
            return
        async with self.job_lock:
            if command in ("/botonly", "/accountmode", "/usermode"):
                mode = "botonly" if command == "/botonly" else "account"
                try:
                    await self.get_checker(mode)
                except RuntimeError as exc:
                    await self.send(event, str(exc))
                    return
                self.store.set_mode(event.sender_id, mode)
                if mode == "botonly":
                    await self.send(event, "Bot-only mode activated: 80 usernames per batch.\n"
                                    "Checks the exact Telegram contact page, then Fragment.\n"
                                    "No user-account login is used. Web availability is an estimate.\n"
                                    "Send a .txt file, /next, or /check username.")
                else:
                    await self.send(event, "Account mode activated: official Telegram API, 50 names per batch.\n"
                                    "Send /botonly to switch back without changing your saved queue.")
            elif event.document:
                await self.load_file(event, event.message)
            elif command in ("/next", "/next50", "/next80"):
                await self.run_batch(event)
            elif command == "/check" or not command.startswith("/"):
                if command == "/check" and not arg and event.is_reply:
                    reply = await event.get_reply_message()
                    if reply and reply.document:
                        await self.load_file(event, reply)
                        return
                    arg = reply.raw_text if reply else ""
                raw = arg if command == "/check" else text
                names = list(dict.fromkeys(normalize(t) for t in SPLIT.split(raw or "") if t))
                size = self.batch_size(event.sender_id)
                if not names or len(names) > size:
                    await self.send(event, f"Use /check username (maximum {size} names in this mode), or upload a .txt file.")
                    return
                if any(len(n) > 64 for n in names):
                    await self.send(event, "Invalid input. Send usernames, not prose or invite links.")
                    return
                await self.run_batch(event, names)
            else:
                await self.send(event, "Unknown command. Use /help.")

    async def callback(self, event):
        if event.data not in (b"next", b"next50", b"next80"):
            return
        if not event.is_private or event.sender_id not in self.allowed:
            await event.answer("Not authorized.", alert=True)
            return
        await event.answer()
        if self.job_lock.locked():
            await self.send(event, "A check/import is already running. Wait for its results.")
            return
        async with self.job_lock:
            await self.run_batch(event)


async def main(login: bool, login_method: str = "qr"):
    os.umask(0o077)
    api_id = int(os.environ.get("API_ID", "0"))
    api_hash = os.environ.get("API_HASH", "").strip()
    if not api_id or not api_hash:
        raise RuntimeError("Set API_ID and API_HASH in .env first.")
    data_dir = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    user = bot = web_checker = None
    try:
        if login:
            user = TelegramClient(str(data_dir / "checker_user"), api_id, api_hash,
                                  flood_sleep_threshold=0, request_retries=5,
                                  raise_last_call_error=True, receive_updates=True)
            await login_user(user, data_dir, method=login_method)
            return
        token = os.environ.get("BOT_TOKEN", "").strip()
        allowed = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "")) if x}
        if not token or not allowed:
            raise RuntimeError("Set BOT_TOKEN and ALLOWED_USER_IDS in .env.")
        delay = float(os.environ.get("CHECK_DELAY", "2.0"))
        if delay < 1 or not delay < float("inf"):
            raise RuntimeError("CHECK_DELAY must be a finite number of seconds >= 1.")
        web_delay = float(os.environ.get("WEB_CHECK_DELAY", "1.0"))
        if web_delay < 1 or not web_delay < float("inf"):
            raise RuntimeError("WEB_CHECK_DELAY must be a finite number of seconds >= 1.")
        default_mode = os.environ.get("DEFAULT_MODE", "").strip().lower()
        if not default_mode:
            default_mode = "account" if (data_dir / "checker_user.session").exists() else "botonly"
        if default_mode not in {"account", "botonly"}:
            raise RuntimeError("DEFAULT_MODE must be botonly or account.")
        # Start the bot independently. An absent/expired checker user session
        # must not prevent /botonly from working.
        bot = TelegramClient(str(data_dir / "checker_bot"), api_id, api_hash)
        await bot.start(bot_token=token)
        me = await bot.get_me()
        expected_bot_id = int(token.split(":", 1)[0])
        if not me.bot or me.id != expected_bot_id:
            raise RuntimeError("Bot session differs from BOT_TOKEN; stop and remove checker_bot.session first.")
        store = Store(data_dir / "progress.sqlite3")
        store.recover()
        web_checker = WebChecker(web_delay)

        async def load_account_checker():
            nonlocal user
            try:
                if user is None:
                    user = TelegramClient(str(data_dir / "checker_user"), api_id, api_hash,
                                          flood_sleep_threshold=0, request_retries=0,
                                          raise_last_call_error=True, receive_updates=False)
                await asyncio.wait_for(user.connect(), timeout=30)
                authorized = await asyncio.wait_for(user.is_user_authorized(), timeout=30)
                if not authorized or await asyncio.wait_for(user.is_bot(), timeout=30):
                    await user.disconnect()
                    raise RuntimeError("No authorized USER session. Run python bot.py --login in your terminal, "
                                       "or use /botonly without logging into a user account.")
                return Checker(user, delay)
            except (errors.RPCError, OSError, asyncio.TimeoutError) as exc:
                raise RuntimeError("Account checker could not connect (" + type(exc).__name__ + "). "
                                   "Use /botonly, or log in with python bot.py --login.") from None

        service = BotService(bot, None, store, allowed, data_dir, web_checker=web_checker,
                             account_loader=load_account_checker, default_mode=default_mode)

        async def guarded_message(event):
            try:
                await service.handle(event)
            except Exception as exc:
                LOG.error("Message handler failed: %s", type(exc).__name__)
                if event.is_private and event.sender_id in allowed:
                    await service.send(event, "Operation failed. Use /last to recover saved results; check server logs.")

        async def guarded_callback(event):
            try:
                await service.callback(event)
            except Exception as exc:
                LOG.error("Button handler failed: %s", type(exc).__name__)
                if event.is_private and event.sender_id in allowed:
                    await service.send(event, "Operation failed. Use /last to recover saved results.")

        bot.add_event_handler(guarded_message, events.NewMessage(incoming=True))
        bot.add_event_handler(guarded_callback, events.CallbackQuery())
        print(f"@{me.username} is running. Send /start in its private chat.")
        await bot.run_until_disconnected()
    finally:
        if web_checker:
            await web_checker.close()
        if bot:
            await bot.disconnect()
        if user:
            await user.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    login_group = parser.add_mutually_exclusive_group()
    login_group.add_argument("--login", "--login-qr", dest="login", action="store_true",
                             help="One-time user login using a QR code (no login code needed)")
    login_group.add_argument("--login-code", action="store_true",
                             help="Phone/code login with delivery diagnostics and QR fallback")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main(args.login or args.login_code, "code" if args.login_code else "qr"))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Do not print credential-bearing request objects or session data.
        if isinstance(exc, (RuntimeError, ValueError)):
            print(f"Startup error: {exc}")
        elif isinstance(exc, errors.RPCError):
            print(f"Startup error: {login_error(exc)}")
        else:
            print(f"Startup error: {type(exc).__name__}; verify credentials/network and try again.")
        raise SystemExit(1)


