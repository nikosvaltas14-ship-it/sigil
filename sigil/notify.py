"""Push notifications for Sigil, sent via a Telegram bot.

Telegram was picked over Pushover/SMS gateways because it's free with no
purchase or carrier gateway involved: create a bot with @BotFather, message
it once, and the chat id from that message is enough to send to it forever.
"""
from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# The Bot API puts the token in the URL path, and httpx logs every request URL
# at INFO: scrub it from any record that passes through the httpx logger.
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


class _RedactBotToken(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _BOT_TOKEN_RE.sub("bot***", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_BOT_TOKEN_RE.sub("bot***", str(a))
                                if isinstance(a, (str, httpx.URL)) else a
                                for a in record.args)
        return True


_httpx_log = logging.getLogger("httpx")
if not any(isinstance(f, _RedactBotToken) for f in _httpx_log.filters):
    _httpx_log.addFilter(_RedactBotToken())

# Telegram's Bot API refuses uploads over 50 MB; stay clear of the edge.
MAX_DOCUMENT_BYTES = 45 * 1024 * 1024
# A multi-MB upload over a phone-grade uplink needs more than a text message.
DOCUMENT_TIMEOUT_SEC = 30
# Telegram's sendMessage limit is 4096 characters; stay under it.
MAX_MESSAGE_CHARS = 4000
# sendDocument's caption limit, in characters.
MAX_CAPTION_CHARS = 1024


def scrub_local_paths(text: str) -> str:
    """Error text can carry local paths, which name the user; show "~" instead."""
    home = str(Path.home())
    for form in {home, home.replace("\\", "/")}:
        text = re.sub(re.escape(form), "~", text, flags=re.I)
    return text


def send_telegram(cfg, text: str) -> bool:
    token = (cfg.get("telegram_bot_token", "") or "").strip()
    chat_id = (cfg.get("telegram_chat_id", "") or "").strip()
    if not token or not chat_id:
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": scrub_local_paths(text)[:MAX_MESSAGE_CHARS]},
            timeout=10,
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def send_telegram_document(cfg, path, caption: str = "") -> bool:
    """Send a file (e.g. the weekly review PDF) to the Telegram chat.

    Multipart `sendDocument`; True only on HTTP 200. Never raises: a missing
    file, a file over MAX_DOCUMENT_BYTES, missing credentials or a network
    error all return False with a log line. The log never includes the URL,
    which carries the bot token.
    """
    token = (cfg.get("telegram_bot_token", "") or "").strip()
    chat_id = (cfg.get("telegram_chat_id", "") or "").strip()
    if not token or not chat_id:
        return False
    doc = Path(path)
    try:
        size = doc.stat().st_size
    except OSError as exc:
        log.warning("telegram document: cannot read %s (%s)", doc.name, type(exc).__name__)
        return False
    if size > MAX_DOCUMENT_BYTES:
        log.warning("telegram document: %s is %.1f MB, over the %d MB limit; not sent",
                    doc.name, size / 1_048_576, MAX_DOCUMENT_BYTES // 1_048_576)
        return False
    mime = mimetypes.guess_type(doc.name)[0] or "application/octet-stream"
    try:
        with doc.open("rb") as fh:
            resp = httpx.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id, "caption": scrub_local_paths(caption or "")[:MAX_CAPTION_CHARS]},
                files={"document": (doc.name, fh, mime)},
                timeout=DOCUMENT_TIMEOUT_SEC,
            )
    except (httpx.HTTPError, OSError, ValueError) as exc:
        log.warning("telegram document: send of %s failed (%s)", doc.name, type(exc).__name__)
        return False
    if resp.status_code != 200:
        log.warning("telegram document: send of %s got HTTP %s", doc.name, resp.status_code)
    return resp.status_code == 200
