"""Local-only user login: QR by default, explicit code delivery diagnostics."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from getpass import getpass
from pathlib import Path
import re
import time

from telethon import errors, functions


async def ask(prompt: str, *, secret: bool = False) -> str:
    # QR wait has already finished before a password prompt is shown. Read on
    # the main thread so Ctrl+C cannot leave an input worker blocking shutdown.
    return (getpass if secret else input)(prompt).strip()


def login_error(exc: Exception) -> str:
    """Explain failures without printing requests, hashes, tokens or passwords."""
    if isinstance(exc, errors.FloodWaitError):
        return f"Telegram rate limit: wait {exc.seconds} seconds before trying again."
    messages = {
        "ApiIdInvalidError": "API_ID/API_HASH was rejected. Check your own credentials from my.telegram.org/apps.",
        "PhoneNumberInvalidError": "Invalid phone number. Use the international format beginning with +.",
        "PhoneNumberBannedError": "Telegram rejected this phone number as banned.",
        "PhoneNumberUnoccupiedError": "This number has no Telegram account. Register in the official app first.",
        "AuthRestartError": "Telegram restarted authorization. Run the login command again.",
        "PasswordHashInvalidError": "Incorrect Telegram two-step verification password.",
    }
    if type(exc).__name__ in messages:
        return messages[type(exc).__name__]
    if isinstance(exc, errors.RPCError):
        return f"Telegram rejected login: {type(exc).__name__} (RPC {exc.code})."
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return "Telegram connection failed or timed out. Check the server connection and try again."
    return f"Login failed: {type(exc).__name__}."


async def enter_password(client):
    for _ in range(3):
        password = await ask("Telegram two-step verification password: ", secret=True)
        if not password:
            print("The two-step verification password cannot be empty.")
            continue
        try:
            return await client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            print("Incorrect two-step verification password. Try again.")
    raise RuntimeError("Password attempts exhausted. Login was not completed.")


def render_qr(url: str, path: Path):
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError as exc:
        raise RuntimeError("QR dependency missing. Run: python -m pip install -r requirements.txt") from exc
    qr = qrcode.QRCode(border=4, box_size=8)
    qr.add_data(url)
    qr.make(fit=True)
    # The token stays on this machine; no external QR-generating service.
    qr.make_image(image_factory=SvgPathImage).save(str(path))
    qr.print_ascii(invert=True)
    print(f"QR image (local, temporary): {path}")


async def qr_login(client, data_dir: Path, *, max_refreshes: int = 10):
    print("Open Telegram on your phone: Settings > Devices > Link Desktop Device.")
    print("Scan the QR on this terminal from another screen and approve the login.")
    print("A Telegram login code is not required. Two-step verification may still be required.")
    print("If the terminal QR looks distorted, open the local login-qr.svg image.")
    path = data_dir / "login-qr.svg"
    try:
        qr = await client.qr_login()
        for index in range(max_refreshes):
            # Telethon requires wait() to be running BEFORE the QR is scanned.
            waiter = asyncio.create_task(qr.wait())
            try:
                await asyncio.sleep(0)
                render_qr(qr.url, path)
                try:
                    return await waiter
                except errors.SessionPasswordNeededError:
                    return await enter_password(client)
                except asyncio.TimeoutError:
                    if index + 1 == max_refreshes:
                        raise RuntimeError("QR login timed out. Run: python bot.py --login")
                    print("QR expired; generating a new one. Scan only the newest QR below.")
                    await qr.recreate()
            finally:
                if not waiter.done():
                    waiter.cancel()
                    with suppress(asyncio.CancelledError):
                        await waiter
    finally:
        path.unlink(missing_ok=True)


def describe_delivery(sent) -> bool:
    """Return whether the response supports entering a code in this client."""
    kind = type(getattr(sent, "type", None)).__name__
    descriptions = {
        "SentCodeTypeApp": "Telegram says it sent the code to the Telegram service chat on your other logged-in devices.",
        "SentCodeTypeSms": "Telegram says it sent the code by SMS to your phone.",
        "SentCodeTypeCall": "Telegram says it will deliver the code by a phone call.",
        "SentCodeTypeFlashCall": "Telegram selected a flash call. Use QR login if this is not supported on your phone.",
        "SentCodeTypeMissedCall": "Telegram selected a missed call. Follow the instructions in your Telegram app or use QR login.",
        "SentCodeTypeEmailCode": "Telegram says it sent the code to your configured login email. Check that inbox and spam.",
        "SentCodeTypeFragmentSms": "Telegram selected Fragment code delivery. Check your Fragment number/account, or use QR login.",
        "SentCodeTypeSmsWord": "Telegram selected an SMS word code.",
        "SentCodeTypeSmsPhrase": "Telegram selected an SMS phrase code.",
    }
    if kind in descriptions:
        print(descriptions[kind])
        return True
    if kind == "SentCodeTypeFirebaseSms":
        print("Telegram requires official-app SMS verification for this attempt. Switching to QR login.")
    elif kind == "SentCodeTypeSetUpEmailRequired":
        print("Telegram requires login-email setup in its official app. Switching to QR login.")
    else:
        print(f"Telegram returned an unsupported code response ({kind}). Switching to QR login.")
    return False


async def code_login(client, data_dir: Path):
    phone = await ask("Your Telegram USER phone number, including country code (+...): ")
    phone = re.sub(r"[\s()-]", "", phone)
    if not re.fullmatch(r"\+[0-9]{6,15}", phone):
        raise RuntimeError("Enter a user phone number in international +countrycode format, not a bot token.")
    try:
        sent = await client.send_code_request(phone)
    except errors.SessionPasswordNeededError:
        return await enter_password(client)
    # Future auth tokens can authorize immediately without any code.
    if await client.is_user_authorized():
        return await client.get_me()
    if not describe_delivery(sent):
        return await qr_login(client, data_dir)
    resend_at = time.monotonic() + max(0, getattr(sent, "timeout", None) or 0)
    attempts, resends = 0, 0
    while attempts < 3:
        code = await ask("Enter code, 'qr' to switch to QR login, 'resend', or 'cancel': ", secret=True)
        if code.lower() == "cancel":
            raise RuntimeError("Login cancelled. No authorized user session was created.")
        if code.lower() == "qr":
            return await qr_login(client, data_dir)
        if code.lower() == "resend":
            if not getattr(sent, "next_type", None):
                print("Telegram offered no resend method. Enter 'qr' to use QR login.")
                continue
            remaining = resend_at - time.monotonic()
            if remaining > 0:
                print(f"Telegram requires waiting {int(remaining) + 1} seconds before resending; or enter 'qr'.")
                continue
            if resends >= 1:
                print("One resend was already requested. Enter 'qr', a received code, or 'cancel'.")
                continue
            sent = await client(functions.auth.ResendCodeRequest(phone, sent.phone_code_hash))
            resends += 1
            if not describe_delivery(sent):
                return await qr_login(client, data_dir)
            resend_at = time.monotonic() + max(0, getattr(sent, "timeout", None) or 0)
            continue
        if not code:
            print("No code entered. Enter 'qr' if the code did not arrive.")
            continue
        attempts += 1
        try:
            return await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except errors.SessionPasswordNeededError:
            return await enter_password(client)
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
            print("Telegram rejected that code. Use the newest code or enter 'qr'.")
        except errors.PhoneCodeExpiredError:
            print("Telegram says the code expired. Switching to QR login.")
            return await qr_login(client, data_dir)
    raise RuntimeError("Code attempts exhausted. Run: python bot.py --login for QR login.")


async def login_user(client, data_dir: Path, *, method: str = "qr"):
    await client.connect()
    try:
        if not await client.is_user_authorized():
            if method == "code":
                await code_login(client, data_dir)
            else:
                await qr_login(client, data_dir)
        me = await client.get_me()
        if not me or getattr(me, "bot", False):
            raise RuntimeError("Login did not authorize a USER account. Checker sessions cannot be bot accounts.")
        print(f"User session verified and saved (user ID {me.id}). Now run: python bot.py")
    except errors.RPCError as exc:
        raise RuntimeError(login_error(exc)) from None
