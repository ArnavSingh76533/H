"""Conservative public t.me + Fragment checks, without a user-account session.

The two public pages can suggest availability; they cannot confirm assignment.
HTTP errors, access blocks and unknown page layouts never mean available.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import re
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import httpx

LOG = logging.getLogger("username_checker.web")
VALID = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z", re.ASCII)
MAX_HTML_BYTES = 2 * 1024 * 1024


class WebRateLimited(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds


def clean_text(text: str) -> str:
    text = " ".join(text.split()).casefold()
    # HTML inline tags can introduce a separator immediately before a comma.
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def soup_for(html: str):
    soup = BeautifulSoup(html, "html.parser")
    # Inspect visible page text, not scripts or JSON that may contain examples.
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    return soup


def blocked(soup) -> bool:
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if any(x in title for x in ("site unavailable", "access denied", "just a moment",
                                "security verification", "attention required")):
        return True
    return bool(soup.select("#challenge-form, #cf-challenge-running, .g-recaptcha, .h-captcha"))


def telegram_state(html: str, username: str) -> str:
    """Return contact / occupied / unknown from a successful t.me HTML page."""
    name = username.casefold()
    soup = soup_for(html)
    if blocked(soup) or not soup.select_one(".tgme_page_wrap, .tgme_body_wrap"):
        return "unknown"
    titles = [clean_text(m.get("content", "")) for m in soup.select(
        'meta[property="og:title"], meta[property="twitter:title"], meta[name="twitter:title"]')]
    if soup.title:
        titles.append(clean_text(soup.title.get_text(" ", strip=True)))
    expected_title = f"telegram: contact @{name}"
    description = soup.select_one(".tgme_page_description")
    text = clean_text(description.get_text(" ", strip=True)) if description else ""
    expected = f"if you have telegram, you can contact @{name} right away."
    # Exact full sentence + matching metadata prevents prefix matches and a
    # username being found in a different account's profile description.
    contact = expected_title in titles and text == expected
    profile_title = soup.select_one(".tgme_page_title")
    if contact and not profile_title:
        return "contact"
    if profile_title and profile_title.get_text(strip=True) and titles:
        # Require a Telegram action for this requested name, not arbitrary HTML.
        for link in soup.select("a[href]"):
            href = link.get("href", "").casefold()
            if re.search(r"(?:[?&]domain=|https://t\.me/)" + re.escape(name) + r"(?:[&#/?]|$)", href):
                return "occupied"
    return "unknown"


def fragment_state(html: str, username: str) -> str:
    """Interpret only the asset status, not generic occurrences of a keyword."""
    name = username.casefold()
    soup = soup_for(html)
    if blocked(soup):
        return "unknown"
    status_nodes = soup.select(".tm-section-header-status")
    if not status_nodes:
        return "unknown"
    # The asset's identity must be present in its title/header/table. A generic
    # Fragment homepage or an unrelated auction does not count as a match.
    identity_nodes = soup.select("title, h1, h2, .tm-section-table, .tm-section-header, .tm-username")
    identity = " ".join(node.get_text(" ", strip=True) for node in identity_nodes)
    identity += " " + " ".join(m.get("content", "") for m in soup.select('meta[property="og:title"]'))
    if not re.search(r"(?<![a-z0-9_])@?" + re.escape(name) + r"(?![a-z0-9_])", identity, re.I):
        return "unknown"
    statuses = {clean_text(node.get_text(" ", strip=True)) for node in status_nodes}
    if len(statuses) != 1:
        return "unknown"
    if statuses & {"available", "on auction", "for sale", "on sale", "auction"}:
        return "purchase"
    if statuses & {"taken", "sold", "purchased", "not for sale"}:
        return "fragment_taken"
    # "Unavailable" on Fragment means no current purchase offer. It does NOT
    # itself say Telegram will assign the username for free.
    if statuses == {"unavailable"}:
        return "not_listed"
    return "unknown"


def retry_seconds(value: str | None) -> int:
    try:
        if value and value.strip().isdigit():
            return min(86400, max(1, int(value)))
        if value:
            due = parsedate_to_datetime(value)
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            return min(86400, max(1, int((due - datetime.now(timezone.utc)).total_seconds()) + 1))
    except (ValueError, TypeError, OverflowError):
        pass
    return 60


class WebChecker:
    def __init__(self, delay: float = 1.0, *, client: httpx.AsyncClient | None = None):
        self.delay = delay
        self.next_request_at = 0.0
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            headers={"User-Agent": "UsernameChecker/1.0", "Accept": "text/html",
                     "Accept-Language": "en-US,en;q=0.9"})

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def _page(self, url: str) -> str | None:
        await asyncio.sleep(max(0, self.next_request_at - time.time()))
        self.next_request_at = time.time() + self.delay

        async def read_page():
            async with self.client.stream("GET", url) as response:
                if response.status_code == 429:
                    seconds = retry_seconds(response.headers.get("Retry-After"))
                    self.next_request_at = time.time() + seconds
                    raise WebRateLimited(seconds)
                if response.status_code != 200:
                    LOG.warning("%s returned HTTP %s", urlparse(url).hostname, response.status_code)
                    return None
                if "text/html" not in response.headers.get("Content-Type", "").casefold():
                    return None
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_HTML_BYTES:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")

        try:
            # Bound decompressed response bytes as well as elapsed time.
            return await asyncio.wait_for(read_page(), timeout=25)
        except (httpx.HTTPError, OSError, asyncio.TimeoutError):
            LOG.warning("%s public-page request failed", urlparse(url).hostname)
            return None

    async def check(self, username: str) -> str:
        if not VALID.fullmatch(username):
            return "invalid"
        username = username.lower()
        html = await self._page(f"https://t.me/{username}")
        if html is None:
            return "unknown"
        state = telegram_state(html, username)
        if state == "occupied":
            return "occupied"
        if state != "contact":
            return "unknown"
        html = await self._page(f"https://fragment.com/username/{username}")
        if html is None:
            return "unknown"
        state = fragment_state(html, username)
        if state == "not_listed":
            return "likely_available"
        return state if state in {"purchase", "fragment_taken"} else "unknown"
