# Telegram Username Checker Bot

Standalone Python bot based on the username-checking idea credited to Charlie.
Uses Telegram's official `account.checkUsername` method through Telethon rather
than treating a missing t.me page or a word on Fragment as proof of availability.
It checks basic **personal-account** usernames. It does not claim usernames,
check private invite hashes, or guarantee availability for channels or bots.

## Features

- Upload a UTF-8 `.txt` file: automatically checks 50 random usernames from anywhere in the list.
- Press **Next 50**, or send `/next`, for another random batch of 50.
- Each file entry is consumed once per allowed user, even across restarts and repeated uploads.
- Case-insensitive deduplication: `Hello_AI`, `hello_ai`, and `@hello_ai` are one name.
- Supports bare usernames, `@username`, and `https://t.me/username`, separated by lines, spaces, commas, or semicolons.
- `/check hello_ai` checks one name; up to 50 names work in one command.
- Reply `/check` to a text message or `.txt` document.
- Plain `@username` or `t.me/username` also works in private chat.
- `/status` shows queue counts; `/last` retrieves the most recent batch if delivery failed.
- Separates available, taken, purchasable, invalid, unavailable-for-account, and unknown results.
- Handles Telegram FloodWait without bypassing it; cooldown survives restart.
- Private chat only, restricted to numeric IDs you configure.

## Setup (Python 3.10+)

Unzip the project, enter its directory, then:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows, activate with `.venv\Scripts\activate` and copy `.env.example` to
`.env` using Explorer or `copy .env.example .env` in Command Prompt.

Edit `.env`:

```dotenv
API_ID=your_numeric_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_botfather_token
ALLOWED_USER_IDS=your_numeric_telegram_user_id
CHECK_DELAY=2.0
```

Get API credentials from <https://my.telegram.org/apps> and the bot token from
[@BotFather](https://t.me/BotFather). The allowed ID is your user ID, not the
bot's ID. Comma-separated IDs allow multiple people, each with their own queue.
If you need to find your ID, start the configured bot with any numeric allowed
ID and message it: the unauthorized response displays the sender's ID.

Perform the one-time checker-account login **in your own terminal**. QR login
is the default, so you do not have to wait for a Telegram/SMS code:

```bash
python bot.py --login
```

On your already logged-in phone, open **Telegram > Settings > Devices > Link
Desktop Device**, scan the terminal QR from another screen, and approve it.
If the terminal QR is distorted, open the temporary local `data/login-qr.svg`
image. This image refreshes when the token expires and is deleted when login
finishes or is cancelled. A two-step verification password is requested only
if your account requires one. QR login needs an existing logged-in Telegram
app; it does not create a new Telegram account.

For phone/code login instead:

```bash
python bot.py --login-code
```

The program displays the delivery method Telegram actually returned (Telegram
service chat, SMS, call, email, or Fragment). If nothing arrives, enter `qr` at
the code prompt to switch to QR login. Enter `resend` to request the next method
**only if Telegram offered one and its timer has elapsed**; the script permits
one explicit resend and never forces SMS or repeatedly requests codes.
Unsupported official-app-only delivery and expired codes switch to QR login.
`cancel` exits without completing login. FloodWait displays Telegram's required
wait in seconds instead of hiding it behind a generic startup error.

Telegram chooses delivery; a script cannot guarantee SMS. Only user accounts
can call `account.checkUsername`; a bot token alone cannot provide this official
check. The bot token provides the chat interface. The user session remains in
the same `data/checker_user.session` file for both login methods and is reused
when you restart. Never send your login code or 2FA password to another person.

Then run:

```bash
python bot.py
```

Open your bot's private chat, send `/start`, and upload your `.txt` file.

Example `names.txt`:

```text
abcai
xyzai
hello_ai
@sample_name
https://t.me/another_name
```

## How random selection and no repeats work

Each newly imported username receives a random priority in an indexed SQLite
queue. Reading the next 50 priorities samples throughout the file, without
rescanning or shuffling a huge file on every click. Random order is fixed for
that queue; it is not sequential file order. New uploads add previously unseen
names to the queue; pending names from earlier uploads remain there.

`data/progress.sqlite3` stores queue and history. **Preserve the entire `data/`
directory** across restarts/deployments. `DATA_DIR` can point to a persistent
volume. Run only one bot process against this directory.

Manual `/check` explicitly rechecks a requested name if you send it again.
Such checks also prevent that name from appearing in future automatic batches.
There is no automatic history reset.

An interrupted/network-failed attempt is saved as unknown and consumed instead
of being silently repeated. A process crash between sending a Telegram request
and saving its result cannot guarantee exactly-once network execution; the bot
keeps the entry consumed and labels it interrupted. `/last` recovers saved
results. You may explicitly `/check` an unknown name again.

FloodWait means Telegram rejected the check: that entry returns to the queue,
the current batch stops, and you can request `/next` after the cooldown.
Unattempted entries stay pending. This can produce a batch smaller than 50.
The final batch is also smaller when fewer than 50 names remain. All-taken or
all-available files still print every result, not only available names.

## Limits and interpretation

- Files: maximum 20 MiB and 1,000,000 tokens. One entry per line is recommended.
- Accepted candidate format: 5–32 ASCII letters/digits/underscores, beginning
  with a letter. Telegram makes the final validity decision; some syntactically
  valid candidates may be reserved or otherwise unassignable.
- Invalid file entries are counted and skipped. Manual invalid names receive
  an invalid result.
- Default request spacing: 2 seconds, so 50 checks take about 100 seconds plus
  network time. Telegram can still impose a longer wait; no rate is guaranteed.
- One batch/import runs at a time across the bot because all users share the
  same checker account. `/status` and `/last` still work while checking.
- A true API response means available for the checker account **at that moment**.
  A false response means unavailable for that account; it is not proof of a
  publicly visible owner. Purchasable names are not free registrations.
- Availability can change after checking. The bot never changes your username.
- Protect `.env` and all `.session` files in `data/`: sessions provide account
  access. Never upload these to a repository or send them to others.

## Verification

```bash
python -m unittest -v
```

The included tests use local Telegram simulations and temporary databases: random
batches, no repeats, re-upload deduplication, restart recovery, UTF-8 rollback,
status mapping, authorization, FloodWait recovery, cooldown persistence, and
the 50-result button flow, QR expiry/refresh, QR two-step verification, delivery
diagnostics, resend timing and hashes, code expiry, and reuse of existing
sessions. A local integration test also exercises Telethon's real QRLogin.wait
with a simulated scan update, checking that the listener is running before the
QR is displayed. No test sends a real code or uses a real account. A real
Telegram login and live availability checks must be tested on your deployment
with your own credentials.

## Updating an existing checkout

Stop the old process with Ctrl+C, then run these from the repository directory
with your virtual environment activated:

```bash
git pull --ff-only
python -m pip install -r requirements.txt
python bot.py --login
python bot.py
```

Keep your existing `.env` and `data/` directory. Login reuses an already
authorized session; do not delete session files or the username database to
fix a missing code.

Official reference: <https://core.telegram.org/method/account.checkUsername>.
Login references: <https://core.telegram.org/api/auth> and
<https://core.telegram.org/api/qr-login>.

