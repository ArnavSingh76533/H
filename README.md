# Telegram Username Checker Bot

Standalone Python bot based on the username-checking idea credited to Charlie.
Both modes run in the same private bot. Neither mode claims usernames.

| Command | Method | Batch size | User-account login |
| --- | --- | --- | --- |
| `/botonly` | Public t.me page, then Fragment | 80 | Not required |
| `/accountmode` or `/usermode` | Official Telegram account.checkUsername | 50 | Required |

The selected mode is saved per allowed user and survives restart. The bot starts
even without a checker user account. New users default to bot-only if no checker
session file exists; existing installations with a session file default to account
mode. Optional `DEFAULT_MODE` overrides the initial default. Explicit chat commands
take precedence over that default.

## Bot-only checks

Send `/botonly`, then upload a .txt file, send `/next`, or `/check username`.
No phone number, user-session file, OTP or QR login is used by this mode.
API_ID, API_HASH and BOT_TOKEN are still needed because the chat interface uses
Telethon authenticated as your **bot**.

The public checker follows this sequence:

1. Fetch https://t.me/username with a bounded HTTP request.
2. Recognize a Telegram profile as taken. Otherwise require the requested
   username in generic contact metadata and the exact visible sentence:
   `If you have Telegram, you can contact @username right away.`
3. Only for that generic contact page, fetch
   https://fragment.com/username/username and inspect the asset status.
4. Fragment **Available / On auction / For sale** means purchasable, not free.
   **Taken / Sold** means collectible or unavailable. An explicit Fragment
   **Unavailable** status plus the generic contact page is reported as
   **likely available (web)**.

Public pages cannot confirm Telegram will let you register a name. Fragment
"Unavailable" only says it is not currently offered there. Reserved or otherwise
unassignable usernames can still lack public profiles. Use account mode for the
official, account-specific result.

Timeouts, HTTP 403/404/5xx, redirects, blocked pages, wrong asset identities and
unrecognized HTML are **unknown**, never available. HTTP 429 honors Retry-After
and saves a separate public-check cooldown. No access challenges are bypassed.
An unknown result or rate limit stops the batch and preserves unattempted entries.
This may produce fewer than 80 results; a short final queue also produces a
smaller batch. Public page layouts can change.

## Commands and files

- Upload a UTF-8 .txt file: checks 80 or 50 random usernames in the active mode.
- **Next 80 / Next 50** or `/next`: another batch without repeats.
- `/check hello_ai`: one name, or up to 80 in bot-only / 50 in account mode.
- Reply `/check` to text or a .txt document. Plain @username or t.me/username also works.
- `/status`: current mode, batch size and queue counts.
- `/last`: recover the latest saved batch, including which mode it used.
- `/help`: current commands and mode.

Entries can be usernames, @usernames or https://t.me/username links, separated
by lines, spaces, commas or semicolons. Invalid entries are counted and skipped.
Limits: 20 MiB and 1,000,000 tokens per file. Candidate syntax is 5-32 ASCII
letters/digits/underscores, starting with a letter. Telegram makes the final
assignability decision. Private invite hashes are not usernames.

## Setup (Python 3.10+)

In the project directory:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows activate with `.venv\Scripts\activate` and copy .env.example to .env.
Fill .env with your own values:

```dotenv
API_ID=your_numeric_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_botfather_token
ALLOWED_USER_IDS=your_numeric_telegram_user_id
CHECK_DELAY=2.0
WEB_CHECK_DELAY=1.0
```

Get API credentials from https://my.telegram.org/apps and the token from
[@BotFather](https://t.me/BotFather). ALLOWED_USER_IDS is your numeric user ID,
not the bot ID; multiple IDs are comma separated. The unauthorized private-chat
response displays the sender's ID if you need to find yours.

Start without a user-account login:

```bash
python bot.py
```

In your bot's private chat send `/botonly`, then a file or `/check hello_ai`.
No user-account client is constructed or connected for bot-only checks.

## Optional account mode

For official checks, log in once **in your own terminal**:

```bash
python bot.py --login
```

On your already logged-in phone open **Telegram > Settings > Devices > Link
Desktop Device**, scan the QR from another screen, and approve it. If the terminal
QR is distorted, open the temporary local data/login-qr.svg image. The image
refreshes on expiry and is removed after the flow exits. Two-step verification
is requested if your account requires it.

For phone/code login use `python bot.py --login-code`. The prompt shows the
delivery method Telegram returned. Enter `qr` if no code arrives, `resend`
only when the offered timer permits it, or `cancel`. Only one explicit resend
is allowed. Telegram controls delivery; the script does not force SMS.

Restart with `python bot.py`, then send `/accountmode`. The official checker
uses data/checker_user.session. Bot-only still works if that session is absent
or expired. A bot account cannot call account.checkUsername. A true API response
means available for the checker user account at that moment; false means
unavailable for that account, not proof of a visible owner. Purchasable usernames
are reported separately. Account checks are for basic personal-account usernames,
not a guarantee of assignability to channels or bots.

## Queue and history

New usernames get random priorities in an indexed SQLite queue. Reading the
next 80 or 50 priorities selects throughout the list without rescanning it on
every click. Uploads add unseen names to the saved queue; older pending entries
remain. Deduplication ignores case.

Both modes share queue/history, so switching modes does not repeat attempted
file entries. Manual `/check` explicitly allows rechecking in either mode,
including verifying a web estimate in account mode. There is no automatic reset.

Results are saved before delivery; `/last` can recover them if delivery fails.
An unknown/network-failed attempt is consumed instead of silently retried.
After a crash an unfinished request becomes interrupted; exactly-once network
execution cannot be guaranteed across a crash. Rate-limited requests that
obtained no result return to the pending queue.

Preserve the entire data/ directory, including progress.sqlite3. DATA_DIR can
point to a persistent volume. Run one process per directory. Existing databases
are migrated without deleting names/results. Each person has their own mode
and queue. One batch/import runs at a time; /status and /last remain available.

Default API spacing is 2 seconds per name; public-page spacing is 1 second per
HTTP request, up to two requests per name. These are not guaranteed safe rates.
Telegram or Fragment may impose longer waits. CHECK_DELAY and WEB_CHECK_DELAY
can increase the spacing (minimum 1 second).

Keep .env and data/*.session private; sessions provide account access. Never
share login codes or passwords. Availability can change after checking.

## Verification

```bash
python -m unittest -v
```

Tests cover official mapping, random batches/no repeats, restart/re-upload
behavior, QR login, HTML parsing, exact username matching, Fragment statuses,
HTTP errors/rate limits, saved modes, old database migration, 80-name files and
buttons, switching back to account mode, and startup without a user client.

HTTP tests use representative HTML and httpx.MockTransport, not real accounts.
Live t.me requests returned access-block pages and Fragment returned HTTP 403 in
the development environment. Live end-to-end public checks still require a
server that can reach both sites.

## Updating

Stop with Ctrl+C, activate your existing virtual environment, then:

```bash
git pull --ff-only
python -m pip install -r requirements.txt
python bot.py
```

Keep .env and data/. Send `/botonly` for batches of 80 or `/accountmode` for
official checks. Do not delete sessions or the database to change modes.

References: https://core.telegram.org/method/account.checkUsername,
https://core.telegram.org/api/auth, https://core.telegram.org/api/qr-login.

