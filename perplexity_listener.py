"""
Long-running Telegram bot listener for the per-post enrichment button.

When a viewer taps the inline "ספר לי עוד על זה" button on a bot-sent
channel post, this listener runs Mistral + Tavily web search fresh (via
WebSearch, using the enricher prompt) and edits the channel message to
append an expandable blockquote with the enriched answer.

Run via systemd / Task Scheduler / NSSM:
    python perplexity_listener.py
"""

import argparse
import asyncio
import ctypes
import json
import logging
import os
import re
import time
from pathlib import Path

_ES_CONTINUOUS        = 0x80000000
_ES_SYSTEM_REQUIRED   = 0x00000001
_ES_DISPLAY_REQUIRED  = 0x00000002

from dotenv import load_dotenv
from telethon import TelegramClient, events

from perplexity_marker import PX_MARKER
from web_search import WebSearch

load_dotenv()


def _load_config() -> dict:
    """Read config.json (Mistral/Tavily settings for the enricher)."""
    try:
        path = Path(__file__).parent / "config.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not read config.json (%s); using defaults.", e)
        return {}

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


_STREAM_WAIT_S = 40  # nominal loader duration (the loop is cancelled when the answer is ready)
_PHASE_POLL_S = 2.0  # how often to check for a phase change (edits happen only on change)

# Friendly narration of the enrichment stages, keyed by elapsed seconds. Loosely
# tracks the real Mistral + Tavily steps (load, search, filter, summarize, verify).
_PHASES = [
    (0, "🔍", "טוען את הפוסט"),
    (5, "🌐", "מחפש ברשת"),
    (10, "🌐", "מסנן תוצאות"),
    (15, "🧠", "מנתח ומסכם"),
    (20, "🧠", "מוודא סיכום"),
    (25, "✍️", "מכין את הפוסט"),
    (30, "✍️", "מסיים"),
    (35, "📌", "ממש תיכף מוכן"),
]


def _phase_for(elapsed: float) -> tuple[str, str]:
    """Return the (emoji, label) for the latest stage reached at `elapsed`."""
    emoji, label = _PHASES[0][1], _PHASES[0][2]
    for start, e, lbl in _PHASES:
        if elapsed >= start:
            emoji, label = e, lbl
    return emoji, label


async def _run_progress_bar(bot, peer_id, msg_id, original: str, total: int = _STREAM_WAIT_S) -> None:
    """Narrate the current pipeline stage. Edits the message ONLY when the phase
    changes (~4 edits over the whole run), which avoids the Telegram edit flood
    waits the earlier per-frame scanner hit (29s EditMessageRequest waits)."""
    last_label = None
    elapsed = 0.0
    while True:
        emoji, label = _phase_for(elapsed)
        if label != last_label:
            body = f"{emoji} {label}…"
            text = original + "\n\n" + PX_MARKER + f"\n<blockquote expandable>{body}</blockquote>"
            try:
                await bot.edit_message(peer_id, msg_id, text, parse_mode="html")
            except Exception:
                pass
            last_label = label
        await asyncio.sleep(_PHASE_POLL_S)
        elapsed += _PHASE_POLL_S


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
        await event.answer("⏳ מעשיר את הפוסט...", cache_time=0)

        char_limit = 1024 if msg.media else 4096
        formatting_overhead = len("\n\n") + len(PX_MARKER) + len("\n<blockquote expandable></blockquote>") + 50
        max_summary_chars = max(0, char_limit - len(original) - formatting_overhead)

        bar_task = asyncio.create_task(
            _run_progress_bar(bot, msg.peer_id, msg.id, original)
        )

        config = _load_config()
        async with _sem:
            try:
                result = await WebSearch(
                    mistral_model=config.get("context_mistral_model", "mistral-large-latest"),
                    search_depth=config.get("context_tavily_depth", "advanced"),
                    max_results=config.get("context_tavily_max_results", 5),
                    summarize_prompt_file="perplexity_prompt_enricher.md",
                ).search(original, max_chars=max_summary_chars)
            except Exception as e:
                bar_task.cancel()
                logger.error("Enrichment failed: %s", e)
                await event.answer(f"העשרה נכשלה: {e}", alert=True)
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
