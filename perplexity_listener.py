"""
Long-running Telegram bot listener for the Perplexity-per-post button.

When a viewer taps the inline "🔍 מידע נוסף מ-Perplexity" button on a
bot-sent channel post, this listener runs PerplexitySearch fresh and edits
the channel message to append (or replace) an expandable blockquote with
the latest answer + sources.

Run via systemd / Task Scheduler / NSSM:
    python perplexity_listener.py
"""

import argparse
import asyncio
import ctypes
import logging
import os
import re
import time

_ES_CONTINUOUS        = 0x80000000
_ES_SYSTEM_REQUIRED   = 0x00000001
_ES_DISPLAY_REQUIRED  = 0x00000002

from dotenv import load_dotenv
from telethon import TelegramClient, events

from perplexity_browser import PerplexityBrowser
from perplexity_search import PerplexitySearchError
from perplexity_marker import PX_MARKER

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

PX_MAX_CONCURRENCY = int(os.environ.get("PX_MAX_CONCURRENCY", "3"))
PX_COOLDOWN_S = int(os.environ.get("PX_COOLDOWN_S", "60"))

_sem = asyncio.Semaphore(PX_MAX_CONCURRENCY)
_last_run_per_msg: dict[tuple[int, int], float] = {}
_headless = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = TelegramClient("perplexity_bot.session", API_ID, API_HASH)


def _split_caption(full: str) -> str:
    """Return the part of `full` before the PX_MARKER (i.e. the original story)."""
    idx = full.find(PX_MARKER)
    return full[:idx].rstrip() if idx >= 0 else full


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


_STREAM_WAIT_S = 20  # must match time.sleep() in perplexity_browser._search_sync


async def _run_progress_bar(bot, peer_id, msg_id, original: str, total: int = _STREAM_WAIT_S) -> None:
    for step in range(1, total + 1):
        bar = "|" + "." * step + " " * (total - step) + "|"
        text = original + "\n\n" + PX_MARKER + f"\n<blockquote expandable><code>{bar}</code></blockquote>"
        try:
            await bot.edit_message(peer_id, msg_id, text, parse_mode="html")
        except Exception:
            pass
        await asyncio.sleep(1)


def _build_blockquote_html(summary: str) -> str:
    body = _html_escape(summary.strip())
    body = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', body)
    return f"{PX_MARKER}\n<blockquote expandable>{body}</blockquote>"


def _chat_key(msg) -> int:
    peer = msg.peer_id
    if hasattr(peer, "channel_id"):
        return peer.channel_id
    if hasattr(peer, "chat_id"):
        return peer.chat_id
    if hasattr(peer, "user_id"):
        return peer.user_id
    return msg.chat_id


@bot.on(events.CallbackQuery(pattern=b"^px$"))
async def on_perplexity_click(event):
    try:
        msg = await event.get_message()
        if msg is None:
            await event.answer("הודעה לא נמצאה", alert=True)
            return

        key = (_chat_key(msg), msg.id)
        now = time.monotonic()
        last = _last_run_per_msg.get(key, 0.0)
        if now - last < PX_COOLDOWN_S:
            wait = int(PX_COOLDOWN_S - (now - last))
            await event.answer(
                f"רענון אחרון בוצע. נסה שוב בעוד {wait} שניות.", alert=False
            )
            return

        full_text = msg.text or msg.message or ""
        original = _split_caption(full_text)
        if not original.strip():
            await event.answer("הפוסט ריק", alert=True)
            return

        _last_run_per_msg[key] = now
        await event.answer("⏳ מריץ Perplexity...", cache_time=0)

        char_limit = 1024 if msg.media else 4096
        formatting_overhead = len("\n\n") + len(PX_MARKER) + len("\n<blockquote expandable></blockquote>") + 50
        max_summary_chars = max(0, char_limit - len(original) - formatting_overhead)

        bar_task = asyncio.create_task(
            _run_progress_bar(bot, msg.peer_id, msg.id, original)
        )

        async with _sem:
            try:
                result = await PerplexityBrowser(
                    prompt_file="perplexity_prompt_enricher.md",
                ).search(original, max_chars=max_summary_chars)
            except PerplexitySearchError as e:
                bar_task.cancel()
                logger.error("Perplexity failed: %s", e)
                await event.answer(f"Perplexity נכשל: {e}", alert=True)
                await bot.edit_message(msg.peer_id, msg.id, original, parse_mode="html")
                return

        bar_task.cancel()

        new_text = original + "\n\n" + _build_blockquote_html(result["summary"])

        if len(new_text) > char_limit:
            truncated_summary = result["summary"][:max_summary_chars] + "…"
            new_text = original + "\n\n" + _build_blockquote_html(truncated_summary)

        await bot.edit_message(msg.peer_id, msg.id, new_text, parse_mode="html")
        logger.info("Edited message %s in chat %s", msg.id, _chat_key(msg))
    except Exception:
        logger.exception("Unhandled error in callback handler")
        try:
            await event.answer("שגיאה פנימית", alert=True)
        except Exception:
            pass


async def main():
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    logger.info("Listener started as @%s", me.username)
    await bot.run_until_disconnected()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--head",
        action="store_true",
        help="Run the Playwright browser in headed (non-headless) mode.",
    )
    args = parser.parse_args()
    _headless = not args.head
    ctypes.windll.kernel32.SetThreadExecutionState(
        _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
    )
    try:
        asyncio.run(main())
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
