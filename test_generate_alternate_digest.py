#!/usr/bin/env python3
"""Fetch alternate-feed posts, process through Mistral AI, generate HTML digest,
and optionally post new stories to a Telegram channel.

Usage:
    python generate_alternate_digest.py

Environment variables (from .env or shell):
    DIGEST_SERVER_URL       – base URL of the TelegramUpdates server
    DIGEST_USERNAME         – admin username for login
    DIGEST_PASSWORD         – admin password for login
    MISTRAL_API_KEY         – Mistral API key
    DIGEST_TELEGRAM_CHANNEL – target Telegram channel (@username or numeric ID)
    TELEGRAM_API_ID         – Telegram API ID (for posting)
    TELEGRAM_API_HASH       – Telegram API hash (for posting)
    TELEGRAM_SESSION        – Telethon StringSession (for posting)
"""

import argparse
import asyncio
import hashlib
import html
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telethon import TelegramClient, Button
from telethon.sessions import StringSession
from perplexity_marker import (
    PX_BUTTON_LABEL,
    TOC_BUTTON_LABEL,
    toc_page_url,
    toc_miniapp_url,
)

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
load_dotenv()

_LOG_BUFFER: list[str] = []


def _log_err(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    _LOG_BUFFER.append(line)


class ProviderRequestError(Exception):
    """Raised when an AI provider request fails unrecoverably (bad status, exhausted retries, missing key)."""


class AIProvider:
    """Base class for OpenAI-compatible chat-completion providers returning JSON."""
    BASE_URL = ""
    SUPPORTS_JSON_RESPONSE_FORMAT = True
    # Max output tokens to request. None => don't send the field (use provider default).
    DEFAULT_MAX_TOKENS: int | None = None
    # Extra provider-specific fields merged into every request body.
    EXTRA_BODY: dict = {}

    def __init__(self, api_key: str, model: str, max_tokens: int | None = None):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens if max_tokens is not None else self.DEFAULT_MAX_TOKENS

    def chat_json(self, system_prompt: str, user_content: str,
                  temperature: float = 0.2, timeout: float = 300.0,
                  max_tokens: int | None = None) -> str:
        if not self.api_key:
            msg = f"API key for {type(self).__name__} is empty."
            _log_err(f"Error: {msg}")
            raise ProviderRequestError(msg)
        body = {
            "model": self.model,
            "temperature": temperature,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        if self.SUPPORTS_JSON_RESPONSE_FORMAT:
            body["response_format"] = {"type": "json_object"}
        if self.EXTRA_BODY:
            body.update(self.EXTRA_BODY)
        effective_max_tokens = max_tokens if max_tokens is not None else self.max_tokens
        if effective_max_tokens:
            body["max_tokens"] = effective_max_tokens

        timeout_obj = httpx.Timeout(timeout, connect=30.0, read=timeout, write=60.0, pool=30.0)
        transient_status = {429, 500, 502, 503, 504}
        transient_excs = (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError, httpx.ReadError)
        backoff = [30, 30, 30]
        last_error = ""
        provider_name = type(self).__name__

        for attempt in range(len(backoff) + 1):
            try:
                chunks: list[str] = []
                with httpx.stream(
                    "POST",
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    json=body,
                    timeout=timeout_obj,
                ) as resp:
                    if resp.status_code in transient_status:
                        err_body = resp.read().decode(errors="replace")
                        last_error = f"HTTP {resp.status_code}: {err_body[:300]}"
                        raise httpx.RemoteProtocolError(last_error)
                    if resp.status_code != 200:
                        err_body = resp.read().decode(errors="replace")
                        msg = f"{provider_name} API error (HTTP {resp.status_code}): {err_body[:500]}"
                        _log_err(msg)
                        raise ProviderRequestError(msg)
                    finish_reason = None
                    got_done = False
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            got_done = True
                            break
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            chunks.append(piece)
                content = "".join(chunks).strip()
                # A truncated response (hit the token cap) or a stream that ended
                # without a proper completion signal yields incomplete JSON. Treat
                # both as transient so the retry loop below re-requests instead of
                # handing garbage to json.loads().
                if finish_reason == "length":
                    raise httpx.RemoteProtocolError(
                        f"output truncated by max_tokens (finish_reason=length, {len(content)} chars)"
                    )
                if not got_done and finish_reason is None:
                    raise httpx.RemoteProtocolError(
                        f"stream ended prematurely without completion (got {len(content)} chars)"
                    )
                return content
            except transient_excs as e:
                last_error = last_error or f"{type(e).__name__}: {e}"
                if attempt >= len(backoff):
                    _log_err(f"{provider_name} transient failure after {attempt + 1} attempts: {last_error}")
                    raise ProviderRequestError(last_error)
                wait = backoff[attempt]
                _log_err(f"{provider_name} transient failure ({last_error}); retrying in {wait}s (attempt {attempt + 2}/{len(backoff) + 1})...")
                time.sleep(wait)
                last_error = ""
        return ""


class MistralProvider(AIProvider):
    BASE_URL = "https://api.mistral.ai/v1"


class NIMProvider(AIProvider):
    BASE_URL = "https://integrate.api.nvidia.com/v1"
    # GLM on NIM honors OpenAI-style JSON mode; enforce valid JSON output.
    SUPPORTS_JSON_RESPONSE_FORMAT = True


class GroqProvider(AIProvider):
    BASE_URL = "https://api.groq.com/openai/v1"
    # Qwen3 and other Groq reasoning models emit <think>...</think> in the content
    # by default, which breaks json.loads(); "hidden" strips it from the response.
    EXTRA_BODY = {"reasoning_format": "hidden"}
    # GLM-5.x has "thinking" enabled by default, so reasoning + output can exceed
    # NIM's implicit output cap and get truncated. Request a generous budget.
    DEFAULT_MAX_TOKENS = 32768


class OpenRouterProvider(AIProvider):
    BASE_URL = "https://openrouter.ai/api/v1"
    # OpenRouter passes response_format through to the upstream model; reasoning
    # tokens are returned in a separate field, not in `content`, so the streamed
    # content stays parseable as JSON.


_NIM_KEY_CACHE: tuple[str, str] | None = None


def _next_nim_api_key() -> tuple[str, str]:
    """Return (api_key, env_name), picking one key per run (rotated across configured keys).

    The chosen key is cached for the lifetime of the process, so every provider built
    during a run (story generation, grammar check, ...) shares the same NIM key and the
    rotation index only advances once.
    """
    global _NIM_KEY_CACHE
    if _NIM_KEY_CACHE is not None:
        return _NIM_KEY_CACHE

    keys = [(n, os.environ.get(n, "").strip()) for n in NIM_KEY_ENV_NAMES]
    keys = [(n, v) for n, v in keys if v]
    if not keys:
        _NIM_KEY_CACHE = ("", NIM_KEY_ENV_NAMES[0])
        return _NIM_KEY_CACHE
    if len(keys) == 1:
        log(f"[NIM] Only one key set ({keys[0][0]}); rotation disabled.")
        _NIM_KEY_CACHE = (keys[0][1], keys[0][0])
        return _NIM_KEY_CACHE
    try:
        last = int(json.loads(NIM_KEY_STATE_PATH.read_text()).get("last_index", -1))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        last = -1
    idx = (last + 1) % len(keys)
    try:
        NIM_KEY_STATE_PATH.write_text(json.dumps({"last_index": idx}))
    except OSError as e:
        log(f"[NIM] Could not persist key rotation state: {e}", error=True)
    name, value = keys[idx]
    log(f"[NIM] Using API key: {name} (rotation index {idx}/{len(keys)})")
    _NIM_KEY_CACHE = (value, name)
    return _NIM_KEY_CACHE


def build_provider(config: dict) -> AIProvider:
    name = (config.get("channel_ai_provider") or "mistral").lower()
    model = config.get("channel_ai_model")
    max_tokens = config.get("channel_max_tokens")
    max_tokens = int(max_tokens) if max_tokens else None
    if name == "nim":
        api_key, _ = _next_nim_api_key()
        return NIMProvider(api_key, model or "z-ai/glm-5.1", max_tokens=max_tokens)
    if name == "mistral":
        return MistralProvider(os.environ.get("MISTRAL_API_KEY", ""),
                               model or "mistral-large-latest", max_tokens=max_tokens)
    if name == "openrouter":
        if not model:
            raise ValueError("channel_ai_model must be set when channel_ai_provider is 'openrouter'")
        return OpenRouterProvider(os.environ.get("OPENROUTER_API_KEY", ""),
                                  model, max_tokens=max_tokens)
    raise ValueError(f"Unknown channel_ai_provider: {name!r}")


def build_grammar_provider(config: dict) -> AIProvider | None:
    """Build the provider for the Hebrew grammar/language-correction pass.

    Reads the grammar_checker_* config keys (providers: nim, mistral, groq). Never
    raises — returns None on an unknown provider so the digest run still proceeds.
    """
    name = (config.get("grammar_checker_provider") or "nim").lower()
    model = config.get("grammar_checker_model")
    if name == "nim":
        api_key, _ = _next_nim_api_key()  # memoized — same key as story generation
        return NIMProvider(api_key, model or "meta/llama-3.3-70b-instruct")
    if name == "mistral":
        return MistralProvider(os.environ.get("MISTRAL_API_KEY", ""),
                               model or "mistral-large-latest")
    if name == "groq":
        return GroqProvider(os.environ.get("GROK_API_KEY", ""),
                            model or "qwen/qwen3.8-27b")
    if name == "openrouter":
        if not model:
            log("grammar_checker_model must be set for openrouter; skipping grammar check.", error=True)
            return None
        return OpenRouterProvider(os.environ.get("OPENROUTER_API_KEY", ""), model)
    log(f"Unknown grammar_checker_provider {name!r}; skipping grammar check.", error=True)
    return None


PROMPT_PATH = Path(__file__).parent / "alternate_feed_prompt.md"
CONFIG_PATH = Path(__file__).parent / "config.json"
HISTORY_PATH = Path(__file__).parent / "digest_history.json"
NIM_KEY_STATE_PATH = Path(__file__).parent / "test_nim_key_state.json"
DEBUG_LOG_PATH = Path(__file__).parent / "test_debug.log"
NIM_KEY_ENV_NAMES = ["NVIDIA_API_KEY", "NIM_UNIFEED", "NVIDIA_API_KEY_2"]
GRAMMAR_PROMPT_PATH = Path(__file__).parent / "grammar_checker.md"
FIRST_RUN_STORY_COUNT = 10
VALIDATE_CALL_PACING_SECONDS = 5
VALIDATE_MAX_TOKENS = 6144
# A grammar response is just corrected_text (~= input length) + a short corrections
# list. Groq's free tier counts max_tokens against an 8000 TPM limit, so a large
# reservation here starves the prompt and every call 413s. Keep this tight.
GRAMMAR_CHECK_MAX_TOKENS = 3000
GRAMMAR_CHECK_RETRIES = 3         # attempts per story before giving up and keeping the original
GRAMMAR_RETRY_DELAY_SECONDS = 5   # pause between grammar-check retries for one story
GRAMMAR_SOURCE_POSTS_MAX = 6      # source posts sent to the grammar model per story
GRAMMAR_SOURCE_POST_CHARS = 600  # per source-post truncation for the grammar model
RUN_LOG_CHANNEL = os.environ.get("RUN_LOG_TELEGRAM_CHANNEL", "@testzionism20")

# --- Script Configuration ---
POST_TO_CHANNEL = True   # Post stories to the Telegram channel (requires DIGEST_TELEGRAM_CHANNEL env var)
POST_VIDEOS = True      # Download and post videos from source posts
def wrap_with_divider(text: str) -> str:
    return text


def _md_to_html(text: str) -> str:
    """Convert a small Markdown subset to Telegram HTML.

    Supports **bold** and "> " line-prefixed quote blocks. Consecutive "> " lines
    become a single <blockquote>. The quote block is used for claims relayed from
    other (often hostile) media — see alternate_feed_prompt.md section 1A.
    """
    out: list[str] = []
    quote: list[str] = []

    def _flush_quote() -> None:
        if quote:
            out.append("<blockquote>" + "\n".join(html.escape(q) for q in quote) + "</blockquote>")
            quote.clear()

    for line in (text or "").split("\n"):
        m = re.match(r'\s*>\s?(.*)$', line)
        if m:
            quote.append(m.group(1))
        else:
            _flush_quote()
            out.append(html.escape(line))
    _flush_quote()

    return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', "\n".join(out), flags=re.DOTALL)


def log(msg: str, error: bool = False) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr if error else sys.stdout)
    _LOG_BUFFER.append(line)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _story_counts(configured_max_stories: int, has_history: bool) -> tuple[int, int]:
    """Return the effective new-story target and model candidate count."""
    if not has_history:
        return FIRST_RUN_STORY_COUNT, FIRST_RUN_STORY_COUNT
    return configured_max_stories, configured_max_stories + 2


def load_history() -> tuple[list[dict], set[str], set[str], dict[str, str]]:
    """Load previously generated stories, processed post keys, posted media hashes, and post texts."""
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        stories = data.get("stories", [])
        if not isinstance(stories, list):
            stories = []
        keys = data.get("processed_post_keys", [])
        media_hashes = data.get("posted_media_hashes", [])
        post_texts = data.get("processed_post_texts", {})
        return stories, set(keys), set(media_hashes), post_texts
    except (FileNotFoundError, json.JSONDecodeError):
        return [], set(), set(), {}


def _story_preview(story: dict, max_len: int = 80) -> str:
    """Return a short text preview for logging."""
    text = story.get("text", "")
    text = text.replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _story_importance_rank(story: dict) -> int:
    """Return a sortable importance rank, placing invalid values last."""
    try:
        return int(story.get("importance"))
    except (TypeError, ValueError):
        return sys.maxsize


def _post_key(post: dict) -> str:
    """Build a unique key for a raw Telegram post."""
    return f"{post.get('channel', '')}_{post.get('post_id', '')}"


def save_history(
    new_stories: list[dict],
    full_history: list[dict],
    offset: int,
    filtered_posts: list[dict],
    processed_keys: set[str],
    posted_media_hashes: set[str] | None = None,
    processed_post_texts: dict[str, str] | None = None,
) -> None:
    """Apply successful story posts/updates and track processed raw post keys."""
    now = datetime.now().isoformat()
    added, updates = 0, 0

    for story in new_stories:
        hi = story.get("history_index")
        if hi is not None:
            if 0 <= hi < len(full_history):
                existing = full_history[hi]
                additional_info = story.get("text", "").strip()
                if not additional_info:
                    continue
                existing_text = existing.get("text", "").rstrip()
                existing["text"] = f"{existing_text}\n\n**עדכון:** {additional_info}"
                existing["updated_at"] = now
                update_record = {
                    "text": additional_info,
                    "created_at": now,
                    "telegram_message_id": story.get("telegram_message_id"),
                    "validation_reason": story.get("validation_reason", ""),
                }
                existing.setdefault("updates", []).append(update_record)
                if story.get("telegram_message_id") is not None:
                    existing.setdefault("telegram_update_message_ids", []).append(
                        story["telegram_message_id"]
                    )
                updates += 1
                log(
                    f"  [HISTORY-UPDATE #{hi}] added={_story_preview(story)} "
                    f"reply_message_id={story.get('telegram_message_id')}"
                )
            else:
                log(f"  [HISTORY-SKIP] Invalid matched history index #{hi}.", error=True)
        else:
            stored_story = {
                key: value
                for key, value in story.items()
                if not key.startswith("_")
                and key not in {"reply_to_message_id", "validation_reason"}
            }
            stored_story["created_at"] = now
            stored_story["updated_at"] = now
            full_history.append(stored_story)
            added += 1
            log(
                f"  [HISTORY-NEW] {_story_preview(story)} "
                f"telegram_message_id={story.get('telegram_message_id')}"
            )

    if processed_post_texts is None:
        processed_post_texts = {}

    for post in filtered_posts:
        key = _post_key(post)
        processed_keys.add(key)
        text = _get_post_text(post)
        if text:
            processed_post_texts[key] = text[:300]

    payload = {
        "stories": full_history,
        "processed_post_keys": sorted(processed_keys),
        "processed_post_texts": processed_post_texts,
        "posted_media_hashes": sorted(posted_media_hashes) if posted_media_hashes else [],
        "last_updated": now,
    }
    HISTORY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"History saved to {HISTORY_PATH} ({len(full_history)} total stories: {added} new, {updates} updates, {len(processed_keys)} tracked post keys)")


def write_debug_log(stories: list[dict], original_posts: list[dict]) -> None:
    """Append a trace of each posted story and the source posts it was built from.

    For every story that was actually posted to the channel this run, records the
    Telegram message id + story text and, for each source index, the post key
    (`channel_postid`) and the full original post text. Appends to test_debug.log so
    the file accumulates across runs. A write failure is logged but never fatal.
    """
    posted = [s for s in stories if s.get("_post_success")]
    if not posted:
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []
    for story in posted:
        kind = "UPDATE" if story.get("history_index") is not None else "NEW"
        lines.append("=" * 70)
        lines.append(
            f"[{ts}] story_id={story.get('telegram_message_id')} kind={kind} "
            f"importance={story.get('importance')}"
        )
        lines.append("STORY CONTENT:")
        lines.append((story.get("text") or "").strip())

        source_indices = story.get("source_indices") or []
        lines.append(f"SOURCE POSTS ({len(source_indices)}):")
        for idx in source_indices:
            if not (isinstance(idx, int) and 0 <= idx < len(original_posts)):
                lines.append(f"  - source_index={idx} (out of range)")
                continue
            p = original_posts[idx]
            lines.append(
                f"  - post_id={_post_key(p)} channel={p.get('channel', '')} "
                f"url={p.get('post_url', '')}"
            )
            body = _get_post_text(p).strip()
            for body_line in (body.splitlines() or [""]):
                lines.append(f"      {body_line}")
        lines.append("")

    try:
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        log(
            f"Debug trace for {len(posted)} posted "
            f"stor{'y' if len(posted) == 1 else 'ies'} appended to {DEBUG_LOG_PATH}"
        )
    except OSError as e:
        log(f"Could not write debug log: {e}", error=True)


# --- Hostile-media-quote screening (see alternate_feed_prompt.md section 1A) ---
# Signals that a *source post* is not first-hand reporting but a relay/translation
# of what other (often hostile) media published.
_HOSTILE_SOURCE_SIGNALS = (
    "ערוצים פלסטינים", "ערוצים פלסטיניים", "תקשורת פלסטינית", "התקשורת הפלסטינית",
    "מקורות פלסטינים", "מקורות פלסטיניים", "דיווחים פלסטינים",
    "בתקשורת הערבית", "התקשורת הערבית", "תעמולה פלסטינית", "לפתוח רמקולים",
)
_ARABIC_CHAR = re.compile(r"[؀-ۿﭐ-﷿ﹰ-ﻼ]")
# Phrases in the *story text* that mark a claim as attributed rather than stated as fact.
_ATTRIBUTION_MARKERS = (
    "תקשורת פלסטינית", "התקשורת הפלסטינית", "ערוצים פלסטינים", "ערוצים פלסטיניים",
    "מקורות פלסטינים", "מקורות פלסטיניים", "לפי התקשורת", "לפי דיווחים", "על פי דיווחים",
    "לפי גורמים פלסטינים", "בתקשורת הערבית", "התקשורת הערבית", "נטען", "לטענת",
    "מציגה כ", "מציגים כ", "לפי הדיווח", "דווח כי",
)
# Framing that must never stand in the digest's own voice (only inside an attributed quote).
_UNATTRIBUTED_RED_FLAGS = ("אלבראק", "אל-בוראק", "אל בוראק", "אל־בוראק")


def _is_hostile_media_quote(post_text: str) -> bool:
    """True if a raw source post merely relays/translates another outlet's report."""
    if any(sig in post_text for sig in _HOSTILE_SOURCE_SIGNALS):
        return True
    has_translation_label = "תרגום" in post_text or "תירגום" in post_text
    return has_translation_label and len(_ARABIC_CHAR.findall(post_text)) >= 15


def _first_line(text: str) -> str:
    stripped = (text or "").strip()
    return stripped.splitlines()[0] if stripped else ""


def screen_hostile_framing(stories: list[dict], original_posts: list[dict]) -> list[dict]:
    """Hold stories that reproduce hostile-media framing without attribution.

    Enforces alternate_feed_prompt.md section 1A: when a story is built from a post
    that only quotes/translates other media, the story's bold subject line MUST
    attribute the claim. Stories that fail are dropped from this run (not posted,
    not saved to history) and recorded in test_debug.log for manual review.
    Returns the filtered list of stories cleared to proceed.
    """
    cleared: list[dict] = []
    held: list[tuple[dict, str]] = []
    for story in stories:
        text = story.get("text") or ""
        bold = _first_line(text)
        attributed_in_bold = any(m in bold for m in _ATTRIBUTION_MARKERS)
        attributed_anywhere = any(m in text for m in _ATTRIBUTION_MARKERS)

        src_texts = [
            _get_post_text(original_posts[i])
            for i in (story.get("source_indices") or [])
            if isinstance(i, int) and 0 <= i < len(original_posts)
        ]
        from_hostile_quote = any(_is_hostile_media_quote(t) for t in src_texts)

        reason = ""
        if from_hostile_quote and not attributed_in_bold:
            reason = ("source post relays hostile media, but the story's bold line "
                      "does not attribute the claim (section 1A step 1)")
        elif any(flag in text for flag in _UNATTRIBUTED_RED_FLAGS) and not attributed_anywhere:
            reason = "story uses hostile framing (\"אלבראק\") with no attribution anywhere in the text"

        if reason:
            held.append((story, reason))
        else:
            cleared.append(story)

    if held:
        for story, reason in held:
            log(f"  [HOSTILE-FRAMING HOLD] {reason}: {_story_preview(story)!r}", error=True)
        _write_hostile_framing_review(held, original_posts)
        log(
            f"Held {len(held)} stor{'y' if len(held) == 1 else 'ies'} from posting for "
            f"hostile-framing review (see {DEBUG_LOG_PATH}); {len(cleared)} cleared."
        )
    return cleared


def _write_hostile_framing_review(held: list[tuple[dict, str]], original_posts: list[dict]) -> None:
    """Append held stories + their source posts to test_debug.log for manual review."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = ["", "#" * 70,
             f"[{ts}] HOSTILE-FRAMING REVIEW — {len(held)} story(ies) held from posting"]
    for story, reason in held:
        lines.append("-" * 70)
        lines.append(f"REASON: {reason}")
        lines.append("STORY CONTENT:")
        lines.append((story.get("text") or "").strip())
        src = story.get("source_indices") or []
        lines.append(f"SOURCE POSTS ({len(src)}):")
        for idx in src:
            if not (isinstance(idx, int) and 0 <= idx < len(original_posts)):
                lines.append(f"  - source_index={idx} (out of range)")
                continue
            p = original_posts[idx]
            lines.append(
                f"  - post_id={_post_key(p)} channel={p.get('channel', '')} "
                f"url={p.get('post_url', '')}"
            )
            for body_line in (_get_post_text(p).strip().splitlines() or [""]):
                lines.append(f"      {body_line}")
        lines.append("")
    try:
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as e:
        log(f"Could not write hostile-framing review log: {e}", error=True)


def login(base_url: str, username: str, password: str) -> str:
    """Authenticate and return a JWT token."""
    resp = httpx.post(
        f"{base_url}/api/login",
        json={"user_name": username, "password": password},
        timeout=15.0,
    )
    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text)
        log(f"Login failed: {detail}", error=True)
        sys.exit(1)
    data = resp.json()
    if not data.get("is_admin"):
        log("Error: user is not admin — alternate feed requires admin access.", error=True)
        sys.exit(1)
    return data["token"]


def fetch_alternate_posts(base_url: str, token: str) -> list[dict]:
    """Fetch posts from the alternate feed endpoint."""
    resp = httpx.get(
        f"{base_url}/api/alternate-posts",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text)
        log(f"Failed to fetch alternate posts: {detail}", error=True)
        sys.exit(1)
    posts = resp.json()
    log(f"Fetched {len(posts)} alternate-feed posts.")
    return posts


def prepare_slim_posts(posts: list[dict], limit: int = 100) -> list[dict]:
    """Strip heavy fields and compute engagement ratios for LLM input."""
    slim = []
    for i, p in enumerate(posts[:limit]):
        views_raw = re.sub(r"[^\d]", "", p.get("views", "0")) or "0"
        views_num = int(views_raw)
        subs = p.get("channel_subscribers") or 0
        engagement = round(views_num / subs * 100, 1) if subs > 0 else 0

        media_url = p.get("photo_url") or p.get("video_thumb") or None
        if media_url and media_url.startswith("data:"):
            media_url = None

        entry = {
            "i": i,
            "ch": p.get("channel", ""),
            "t": (p.get("text_plain") or "")[:300],
            "dt": p.get("datetime", ""),
            "v": p.get("views", ""),
            "e": engagement,
            "m": bool(p.get("photo_url") or p.get("video_thumb")),
            "media_url": media_url,
        }
        lp = p.get("link_preview")
        if lp and lp.get("title"):
            entry["lp"] = lp["title"][:80]
        slim.append(entry)
    return slim


def _get_post_text(post: dict) -> str:
    """Extract plain text from a post for comparison, stripping HTML if needed."""
    text = post.get("text_plain", "")
    if not text:
        text_html = post.get("text_html", "")
        if text_html:
            text = re.sub(r"<[^>]+>", "", text_html)
    return text


def _text_similarity(text1: str, text2: str) -> float:
    """Compute similarity between two texts (0.0 to 1.0)."""
    import difflib
    return difflib.SequenceMatcher(None, text1, text2).ratio()


def deduplicate_posts(posts: list[dict], similarity_threshold: float = 0.85) -> list[dict]:
    """Remove near-duplicate posts within a batch, keeping the most complete version.

    Uses substring containment + fuzzy matching to detect duplicates.
    Returns deduplicated list and logs removed duplicates.
    """
    if not posts:
        return posts

    deduplicated = []
    skip_indices: set[int] = set()

    _get_text = _get_post_text

    for i, post_i in enumerate(posts):
        if i in skip_indices:
            continue

        text_i = _get_text(post_i)
        kept_post = post_i
        duplicates = [i]

        for j, post_j in enumerate(posts):
            if i >= j or j in skip_indices:
                continue

            text_j = _get_text(post_j)

            # Check substring containment (one is mostly contained in the other)
            if len(text_i) > 0 and len(text_j) > 0:
                min_len = min(len(text_i), len(text_j))
                max_len = max(len(text_i), len(text_j))
                ratio = min_len / max_len

                # If one is >80% contained in the other, they're likely duplicates
                if ratio > 0.80:
                    # Also check fuzzy similarity
                    similarity = _text_similarity(text_i, text_j)
                    if similarity > similarity_threshold:
                        log(f"    → MATCH! Marking post {j} as duplicate")
                        duplicates.append(j)
                        skip_indices.add(j)
                        # Keep the longer post as the "source"
                        if len(text_j) > len(text_i):
                            kept_post = post_j
                            text_i = text_j

        if len(duplicates) > 1:
            log(f"  Deduplicated {len(duplicates)} posts: kept post from channel '{kept_post.get('channel')}' (merged {len(duplicates)-1} near-duplicates)")

        deduplicated.append(kept_post)

    if len(skip_indices) > 0:
        log(f"Post deduplication: {len(posts)} posts → {len(deduplicated)} after removing {len(skip_indices)} near-duplicates")

    return deduplicated


def filter_covered_by_history(
    posts: list[dict],
    processed_post_texts: dict[str, str],
    similarity_threshold: float = 0.85,
) -> tuple[list[dict], list[dict]]:
    """Split posts into (to_send, covered) based on similarity to previously-processed post texts.

    A post is 'covered' if its text closely matches any stored processed post text,
    meaning the same event was already sent to Mistral in a prior batch.
    """
    if not processed_post_texts or not posts:
        return posts, []

    to_send: list[dict] = []
    covered: list[dict] = []

    for post in posts:
        full_text = _get_post_text(post)
        if not full_text:
            to_send.append(post)
            continue

        matched = False
        for stored_key, stored_text in processed_post_texts.items():
            if not stored_text:
                continue
            # Compare equal-length prefixes: stored_text is already ≤300 chars,
            # so truncate the new post to the same length for a fair comparison.
            compare_len = min(len(full_text), len(stored_text), 300)
            if compare_len < 50:
                continue
            text = full_text[:compare_len]
            stored = stored_text[:compare_len]
            sim = _text_similarity(text, stored)
            if sim >= similarity_threshold:
                log(f"  [HISTORY-DEDUP] Post {_post_key(post)} matches {stored_key} (sim={sim:.3f}) — skipping")
                matched = True
                break

        if matched:
            covered.append(post)
        else:
            to_send.append(post)

    if covered:
        log(f"History dedup: {len(posts)} posts → {len(to_send)} to send ({len(covered)} already covered by history)")

    return to_send, covered


def call_provider(provider: AIProvider, prompt: str, user_content: str) -> list:
    """Send posts to the AI provider and parse the structured JSON response."""
    name = type(provider).__name__
    max_parse_attempts = 3
    for attempt in range(max_parse_attempts):
        content = provider.chat_json(prompt, user_content, temperature=0.2, timeout=900.0)
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            result = json.loads(content)
        except json.JSONDecodeError as e:
            log(f"Failed to parse {name} response: {e}", error=True)
            log(f"Raw content: {content[:1000]}", error=True)
            if attempt < max_parse_attempts - 1:
                log(f"Retrying {name} request (parse attempt {attempt + 2}/{max_parse_attempts})...", error=True)
                continue
            sys.exit(1)

        stories = result.get("stories", result if isinstance(result, list) else [])
        if not isinstance(stories, list):
            log(f"{name} returned invalid stories format.", error=True)
            sys.exit(1)

        return stories

    return []


HISTORY_VALIDATION_PROMPT = """
You validate one newly generated Hebrew news story against stories that were
previously posted to a Telegram channel.

Compare the candidate semantically against EVERY item in posted_stories. Do not
compare wording alone; identify whether the same real-world event is already
covered.

Return exactly one JSON object:
{
  "classification": "new" | "update" | "duplicate",
  "matched_history_index": <integer or null>,
  "additional_info": "<only genuinely new facts, in concise Hebrew, or empty>",
  "reason": "<clear concise explanation>"
}

Rules:
- "new": no posted story covers the same event. matched_history_index must be null.
- "update": a posted story covers the event, but the candidate contains material
  new facts. additional_info must contain only those new facts.
- "duplicate": the event and all material facts are already covered.
- Rewording, commentary, emphasis, or a new source without new facts is duplicate.
- Never invent facts. Use only the candidate and posted_stories.
"""


def _posted_history_for_validation(history: list[dict]) -> list[dict]:
    """Return generated history stories that were posted (legacy entries are assumed posted)."""
    return [
        {
            "history_index": idx,
            "text": story.get("text", ""),
            "telegram_message_id": story.get("telegram_message_id"),
        }
        for idx, story in enumerate(history)
        if story.get("text") and story.get("telegram_posted") is not False
    ]


def validate_story_candidate(
    story: dict,
    rank: int,
    posted_history: list[dict],
    provider: AIProvider,
) -> dict:
    """Validate one candidate with the model against all posted generated stories."""
    log(f"  [VALIDATE rank={rank}] candidate={_story_preview(story)}")
    if not posted_history:
        reason = "No previously posted stories exist in digest history."
        log(f"  [NEW rank={rank}] {reason}")
        return {
            "classification": "new",
            "matched_history_index": None,
            "additional_info": "",
            "reason": reason,
        }

    content = provider.chat_json(
        HISTORY_VALIDATION_PROMPT,
        json.dumps(
            {"candidate": story.get("text", ""), "posted_stories": posted_history},
            ensure_ascii=False,
        ),
        temperature=0.0,
        timeout=900.0,
        max_tokens=VALIDATE_MAX_TOKENS,
    )
    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        reason = f"Validation response was invalid JSON: {exc}"
        log(f"  [REJECT rank={rank}] {reason}", error=True)
        return {
            "classification": "duplicate",
            "matched_history_index": None,
            "additional_info": "",
            "reason": reason,
        }

    classification = str(result.get("classification", "")).strip().lower()
    reason = str(result.get("reason", "")).strip() or "Model supplied no reason."
    additional_info = str(result.get("additional_info", "")).strip()
    matched_index = result.get("matched_history_index")
    try:
        matched_index = int(matched_index) if matched_index is not None else None
    except (TypeError, ValueError):
        matched_index = None

    history_by_index = {item["history_index"]: item for item in posted_history}
    matched = history_by_index.get(matched_index)
    if classification not in {"new", "update", "duplicate"}:
        classification = "duplicate"
        reason = f"Invalid model classification; candidate rejected. Model reason: {reason}"
    if classification in {"update", "duplicate"} and matched is None:
        classification = "duplicate"
        reason = f"No valid matched history entry was supplied. Model reason: {reason}"
    if classification == "update" and not additional_info:
        classification = "duplicate"
        reason = f"Rejected update because it contains no genuinely new information. {reason}"

    if classification == "new":
        log(f"  [NEW rank={rank}] reason={reason}")
    elif classification == "update":
        log(
            f"  [UPDATE rank={rank}] reason={reason} matched_history=#{matched_index} "
            f"telegram_message_id={matched.get('telegram_message_id')} "
            f"existing={matched.get('text', '')[:160]!r} additional={additional_info[:160]!r}"
        )
    else:
        matched_details = (
            f" matched_history=#{matched_index} "
            f"telegram_message_id={matched.get('telegram_message_id')} "
            f"existing={matched.get('text', '')[:160]!r}"
            if matched
            else ""
        )
        log(
            f"  [REJECT-DUPLICATE rank={rank}] reason={reason}{matched_details} "
            f"candidate={_story_preview(story)!r}"
        )

    return {
        "classification": classification,
        "matched_history_index": matched_index,
        "additional_info": additional_info,
        "reason": reason,
    }


def _grammar_source_refs(story: dict, source_posts: list[dict] | None) -> list[str]:
    """Collect read-only raw source-post texts for one story's grammar check.

    Uses ``story["source_indices"]`` to index into ``source_posts`` (the same list
    order ``resolve_media_urls`` relies on). Returns an ordered, de-duplicated,
    truncated list; empty when there is nothing usable.
    """
    if not source_posts:
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for idx in story.get("source_indices", []) or []:
        if not isinstance(idx, int) or not (0 <= idx < len(source_posts)):
            continue
        text = (source_posts[idx].get("text_plain") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        refs.append(text[:GRAMMAR_SOURCE_POST_CHARS])
        if len(refs) >= GRAMMAR_SOURCE_POSTS_MAX:
            break
    return refs


def _grammar_call_once(provider: "AIProvider", prompt: str, payload: dict) -> dict:
    """Run one grammar-model call and return the parsed result dict.

    Raises ``ProviderRequestError`` (provider failure) or ``ValueError`` (missing /
    unparseable / empty response). The caller treats both as retryable.
    """
    content = provider.chat_json(
        prompt,
        json.dumps(payload, ensure_ascii=False),
        temperature=0.0,
        timeout=900.0,
        max_tokens=GRAMMAR_CHECK_MAX_TOKENS,
    )
    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response was not valid JSON ({exc})") from exc
    if not isinstance(result, dict) or not str(result.get("corrected_text", "")).strip():
        raise ValueError("no corrected_text in response")
    return result


def grammar_check_stories(
    stories: list[dict], config: dict, source_posts: list[dict] | None = None
) -> None:
    """Run a Hebrew grammar/language-correction model pass over each story's text.

    For every story about to be posted (new stories and update fragments alike) the
    configured grammar model:
      1. keeps the text Hebrew-only (foreign proper nouns excepted) and translates any
         other non-Hebrew wording,
      2. fixes gender agreement (זכר/נקבה),
      3. fixes Hebrew grammar, punctuation and awkward phrasing,
      4. fixes missing/garbled verbs and bold-line/body mismatches, using the raw
         source posts (when ``source_posts`` is given) as read-only reference.

    Every applied correction is logged (original -> corrected). The pass is best
    effort: a missing prompt file or an unknown provider skips the whole pass. A
    per-story failure (provider error, unparseable or empty response) is retried up
    to ``GRAMMAR_CHECK_RETRIES`` times; if every attempt fails the original text is
    kept and the run moves on to the next story. Mutates ``story["text"]`` in place.
    ``source_posts`` is optional; when omitted the model receives only
    ``{"text": ...}`` exactly as before.
    """
    if not stories:
        return

    provider = build_grammar_provider(config)
    if provider is None:
        return

    try:
        prompt = GRAMMAR_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log(f"Grammar check skipped: prompt file not found at {GRAMMAR_PROMPT_PATH}", error=True)
        return

    log(
        f"Running grammar/language check on {len(stories)} "
        f"stor{'y' if len(stories) == 1 else 'ies'} via "
        f"{type(provider).__name__} ({provider.model})..."
    )

    total_corrections = 0
    checked = 0
    for rank, story in enumerate(stories, start=1):
        original = story.get("text", "")
        if not original.strip():
            continue
        if checked > 0:
            time.sleep(VALIDATE_CALL_PACING_SECONDS)
        checked += 1

        refs = _grammar_source_refs(story, source_posts)
        payload: dict = {"text": original}
        if refs:
            payload["source_posts"] = refs

        result: dict | None = None
        for attempt in range(1, GRAMMAR_CHECK_RETRIES + 1):
            try:
                result = _grammar_call_once(provider, prompt, payload)
                break
            except (ProviderRequestError, ValueError) as exc:
                if attempt < GRAMMAR_CHECK_RETRIES:
                    log(
                        f"  [GRAMMAR rank={rank}] attempt {attempt}/{GRAMMAR_CHECK_RETRIES} "
                        f"failed: {exc}. Retrying in {GRAMMAR_RETRY_DELAY_SECONDS}s...",
                        error=True,
                    )
                    time.sleep(GRAMMAR_RETRY_DELAY_SECONDS)
                else:
                    log(
                        f"  [GRAMMAR rank={rank}] all {GRAMMAR_CHECK_RETRIES} attempts "
                        f"failed: {exc}. Keeping original text, moving to next story.",
                        error=True,
                    )
        if result is None:
            continue

        corrected = str(result.get("corrected_text", "")).strip()
        corrections = result.get("corrections") or []
        if corrected == original:
            log(f"  [GRAMMAR rank={rank}] no changes needed.")
            continue

        applied = 0
        for item in corrections:
            if not isinstance(item, dict):
                continue
            applied += 1
            log(
                f"  [GRAMMAR-FIX rank={rank}] ({item.get('issue', '')}) "
                f"{item.get('original', '')!r} -> {item.get('corrected', '')!r}"
            )
        total_corrections += applied
        log(
            f"  [GRAMMAR rank={rank}] text updated ({applied} correction(s)).\n"
            f"    BEFORE: {original[:200]!r}\n"
            f"    AFTER:  {corrected[:200]!r}"
        )
        story["text"] = corrected

    log(
        f"Grammar/language check complete: {total_corrections} correction(s) applied "
        f"across {checked} stor{'y' if checked == 1 else 'ies'}."
    )


def _abs_url(url: str, base_url: str) -> str | None:
    """Convert a media URL to an absolute URL, or return None if unusable."""
    if not url or url.startswith("data:"):
        return None
    if url.startswith("/api/"):
        return base_url + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return None


def _media_url_fingerprint(url: str) -> str:
    """Extract a stable fingerprint by stripping query params and isolating the path tail."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    # Extract filename or last 2 path segments for better matching
    segments = path.split("/")
    if len(segments) > 1:
        return "/".join(segments[-2:])
    return segments[-1] if segments else ""


def resolve_media_urls(stories: list[dict], original_posts: list[dict], base_url: str) -> list[dict]:
    """Ensure media_urls in stories contain absolute, deduplicated URLs.

    Layer 1 dedup: URL-path fingerprinting catches CDN variants of the same image.
    Layer 1.5 dedup: Intra-story deduplication removes duplicate URLs within a single story.
    Layer 1.75 dedup: Cross-story deduplication removes images that appear in multiple stories.
    """
    global_fingerprints: set[str] = set()

    for story in stories:
        resolved = []
        thumb_resolved = []
        seen_fingerprints: set[str] = set()

        def _try_add(url: str | None, target: list) -> None:
            if not url:
                return
            abs = _abs_url(url, base_url)
            if not abs:
                return
            fp = _media_url_fingerprint(abs)
            if fp in seen_fingerprints:
                return
            if fp in global_fingerprints:
                return
            seen_fingerprints.add(fp)
            global_fingerprints.add(fp)
            target.append(abs)

        for url in story.get("media_urls", []):
            _try_add(url, resolved)
        for idx in story.get("source_indices", []):
            if 0 <= idx < len(original_posts):
                p = original_posts[idx]
                _try_add(p.get("photo_url") or "", resolved)
                _try_add(p.get("video_thumb") or "", thumb_resolved)
                lp = p.get("link_preview")
                if lp and lp.get("image"):
                    _try_add(lp["image"], resolved)
        story["media_urls"] = resolved
        story["video_thumb_urls"] = thumb_resolved
    return stories


async def scrape_video_cdn_url(channel: str, post_id: str) -> str | None:
    """Scrape Telegram's embed page to extract the direct CDN video URL."""
    embed_url = f"https://t.me/{channel}/{post_id}?embed=1"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(embed_url)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "html.parser")
            video_tag = soup.find("video")
            if video_tag:
                src = video_tag.get("src")
                if src:
                    log(f"  Scraped video CDN URL for {channel}/{post_id}")
                    return src
    except Exception as e:
        log(f"  Failed to scrape video URL for {channel}/{post_id}: {e}", error=True)
    return None


async def resolve_video_urls(stories: list[dict], original_posts: list[dict]) -> list[dict]:
    """Collect CDN video URLs for source posts that have video.

    Populates a 'video_urls' list on each story (separate from media_urls).
    Only works for public channels; private channel videos are skipped.
    """
    global_seen_urls: set[str] = set()  # Level 2: cross-story URL dedup
    for story in stories:
        video_urls: list[str] = []
        seen: set[str] = set()
        seen_urls: set[str] = set()  # Level 1: within-story URL dedup
        for idx in story.get("source_indices", []):
            if not (0 <= idx < len(original_posts)):
                continue
            p = original_posts[idx]
            if not p.get("has_video"):
                continue
            post_url = p.get("post_url", "")
            if "t.me/c/" in post_url:
                log(f"  Skipping private channel video: {post_url}")
                continue
            channel = p.get("channel", "")
            pid = p.get("post_id", "")
            if not channel or not pid:
                continue
            dedup_key = f"{channel}_{pid}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            cdn_url = await scrape_video_cdn_url(channel, pid)
            if cdn_url:
                if cdn_url in seen_urls:
                    log(f"  [VIDEO-DEDUP] Duplicate CDN URL within story, skipping: {cdn_url}")
                    continue
                if cdn_url in global_seen_urls:
                    log(f"  [VIDEO-DEDUP] URL already assigned to another story, skipping: {cdn_url}")
                    continue
                seen_urls.add(cdn_url)
                global_seen_urls.add(cdn_url)
                video_urls.append(cdn_url)
        story["video_urls"] = video_urls
    return stories


_shared_user_client: TelegramClient | None = None
_shared_bot_client: TelegramClient | None = None
_bot_client_attempted = False


async def connect_telegram() -> TelegramClient | None:
    """Return the cached Telethon user client for this run, connecting it on first use."""
    global _shared_user_client
    if _shared_user_client is not None:
        return _shared_user_client
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    session_str = os.environ.get("TELEGRAM_SESSION")
    if not (api_id and api_hash and session_str):
        return None
    client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        log("Telegram session is not authorized; posting disabled.", error=True)
        await client.disconnect()
        return None
    _shared_user_client = client
    return client


async def connect_telegram_bot() -> TelegramClient | None:
    """Return the cached Telethon bot client for this run, logging in on first use only."""
    global _shared_bot_client, _bot_client_attempted
    if _bot_client_attempted:
        return _shared_bot_client
    _bot_client_attempted = True
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not (api_id and api_hash and bot_token):
        return None
    # Persistent session file: reuses the existing bot authorization instead of
    # re-running ImportBotAuthorizationRequest every run (which trips Telegram's
    # flood limit -- "A wait of N seconds is required"). Mirrors perplexity_listener.py.
    session_path = str(Path(__file__).parent / "digest_bot.session")
    client = TelegramClient(session_path, int(api_id), api_hash)
    try:
        await client.start(bot_token=bot_token)
        _shared_bot_client = client
        return client
    except Exception as exc:
        log(
            f"Bot client startup failed; falling back to user client: {exc}",
            error=True,
        )
        try:
            await client.disconnect()
        except Exception:
            pass
        return None


async def resolve_toc_url(ch: str, base_url: str) -> str | None:
    """URL for the "today's headlines" inline button.

    Prefers a Direct Link Mini App (``t.me/<bot>/<app>``) that opens natively inside
    Telegram with no browser chrome; falls back to the raw ``/digest/today`` page URL
    (``DIGEST_TOC_PUBLIC_URL`` if set, else ``base_url``) when the bot username or the
    Mini App short name is unknown. ``ch`` is ``"test"`` or ``"prod"``.
    """
    username = os.environ.get("DIGEST_TOC_BOT_USERNAME", "")
    app_short = os.environ.get("DIGEST_TOC_APP", "")
    if not username:
        bot_client = await connect_telegram_bot()
        if bot_client is not None:
            try:
                username = (await bot_client.get_me()).username or ""
            except Exception:
                username = ""
    if username and app_short:
        return toc_miniapp_url(username, app_short, ch)
    key = os.environ.get("DIGEST_TOC_KEY", "")
    page_base = os.environ.get("DIGEST_TOC_PUBLIC_URL") or base_url
    if page_base and key:
        return toc_page_url(page_base, ch, key)
    return None


async def disconnect_telegram_clients() -> None:
    """Disconnect any Telegram clients that were connected/cached during this run."""
    global _shared_user_client, _shared_bot_client, _bot_client_attempted
    if _shared_bot_client is not None:
        await _shared_bot_client.disconnect()
        _shared_bot_client = None
    if _shared_user_client is not None:
        await _shared_user_client.disconnect()
        _shared_user_client = None
    _bot_client_attempted = False


async def _send_run_log_to_channel() -> None:
    """Send the full captured run log to RUN_LOG_CHANNEL as a file attachment."""
    if not _LOG_BUFFER:
        return
    log_text = "\n".join(_LOG_BUFFER)

    channel = RUN_LOG_CHANNEL
    if channel.lstrip("-").isdigit():
        channel = int(channel)

    client = await connect_telegram_bot()
    if client is None:
        client = await connect_telegram()
        if client is None:
            print("[run-log] Telegram credentials missing; cannot send run log.", file=sys.stderr)
            return

    try:
        entity = await client.get_entity(channel)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        file_buf = io.BytesIO(log_text.encode("utf-8"))
        file_buf.name = f"run_log_{ts}.txt"
        await client.send_file(entity, file=file_buf, caption=f"Run log — {ts}")
    except Exception as e:
        print(f"[run-log] Failed to send run log: {e}", file=sys.stderr)


async def post_stories_to_telegram(
    stories: list[dict],
    channel: str | int,
    posted_media_hashes: set[str],
    new_story_target: int | None = None,
    toc_url: str | None = None,
) -> set[str]:
    """Post stories to a Telegram channel with multi-layer media dedup.

    Returns the updated posted_media_hashes set (includes newly posted hashes).
    - Layer 2: Content-based MD5 dedup (catches same image from different URLs)
    - Layer 3: Cross-run dedup (posted_media_hashes persisted from previous runs)
    """
    client = await connect_telegram()
    if client is None:
        log("Telegram credentials missing; skipping channel posting.", error=True)
        for story in stories:
            story["_post_success"] = False
        return posted_media_hashes

    bot_client = await connect_telegram_bot()
    sender = bot_client if bot_client is not None else client
    buttons = None
    if bot_client is not None:
        row = [Button.inline(PX_BUTTON_LABEL, b"px")]
        if toc_url:
            row.append(Button.url(TOC_BUTTON_LABEL, toc_url))
        buttons = [row]
        log("Posting via bot client (Perplexity + headlines buttons enabled).")

    try:
        entity = await sender.get_entity(channel)
        log(f"Posting {len(stories)} items to Telegram channel: {getattr(entity, 'title', channel)}")
    except Exception as e:
        log(f"Failed to resolve Telegram channel '{channel}': {e}", error=True)
        return posted_media_hashes

    run_hashes: set[str] = set()
    run_content_hashes: set[str] = set()  # Track content hashes within this run
    run_video_urls: set[str] = set()  # Level 3: URL dedup safety net across stories
    successful_new_stories = 0

    for i, story in enumerate(stories):
        is_update = story.get("history_index") is not None
        if (
            not is_update
            and new_story_target is not None
            and successful_new_stories >= new_story_target
        ):
            story["_post_success"] = False
            story["_post_skipped_quota"] = True
            log(
                f"  [SKIP-EXTRA rank={story.get('_candidate_rank')}] "
                f"New-story target already reached: {_story_preview(story)}"
            )
            continue

        try:
            story_hashes: set[str] = set()
            story_video_urls: set[str] = set()
            story_text = story.get("text", "")
            if is_update:
                story_text = "**עדכון**\n\n" + story_text
            text = _md_to_html(wrap_with_divider(story_text))

            image_buffers = []
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
                media_urls = [] if is_update else story.get("media_urls", [])[:10]
                for url in media_urls:
                    try:
                        resp = await http.get(url)
                        if resp.status_code != 200:
                            continue
                        data = resp.content
                        h = hashlib.md5(data).hexdigest()
                        # Layer 2 dedup: Skip if already downloaded in this run
                        if h in run_content_hashes or h in story_hashes:
                            log(f"  Skipping duplicate image content in run: {url}")
                            continue
                        # Layer 3 dedup: Skip if posted in previous runs
                        if h in posted_media_hashes:
                            log(f"  Skipping image posted in previous run: {url}")
                            continue
                        story_hashes.add(h)
                        content_type = resp.headers.get("content-type", "")
                        ext_map = {"image/png": "png", "image/gif": "gif", "image/webp": "webp"}
                        ext = ext_map.get(content_type.split(";")[0].strip(), "jpg")
                        buf = io.BytesIO(data)
                        buf.name = f"photo_{i}_{len(image_buffers)}.{ext}"
                        image_buffers.append(buf)
                    except Exception as e:
                        log(f"  Failed to download image {url}: {e}", error=True)

            video_buffers = []
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as http:
                video_urls = [] if is_update else story.get("video_urls", [])[:5]
                for url in video_urls:
                    if url in run_video_urls or url in story_video_urls:
                        log(f"  [VIDEO-DEDUP] Skipping already-downloaded video URL: {url}")
                        continue
                    try:
                        resp = await http.get(url)
                        if resp.status_code != 200:
                            continue
                        data = resp.content
                        h = hashlib.md5(data).hexdigest()
                        if h in posted_media_hashes or h in run_hashes or h in story_hashes:
                            log(f"  [VIDEO-DEDUP] Skipping by content hash: {url}")
                            continue
                        story_hashes.add(h)
                        story_video_urls.add(url)
                        buf = io.BytesIO(data)
                        buf.name = f"video_{i}_{len(video_buffers)}.mp4"
                        video_buffers.append(buf)
                    except Exception as e:
                        log(f"  Failed to download video {url}: {e}", error=True)

            if not video_buffers:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
                    thumb_urls = [] if is_update else story.get("video_thumb_urls", [])[:5]
                    for url in thumb_urls:
                        try:
                            resp = await http.get(url)
                            if resp.status_code != 200:
                                continue
                            data = resp.content
                            h = hashlib.md5(data).hexdigest()
                            if (
                                h in run_content_hashes
                                or h in story_hashes
                                or h in posted_media_hashes
                            ):
                                continue
                            story_hashes.add(h)
                            buf = io.BytesIO(data)
                            buf.name = f"thumb_{i}_{len(image_buffers)}.jpg"
                            image_buffers.append(buf)
                        except Exception as e:
                            log(f"  Failed to download video thumb {url}: {e}", error=True)

            all_media = video_buffers + image_buffers
            if not all_media:
                text = "\n" + text
            if all_media:
                await sender.send_file(entity, file=all_media)
            sent_message = await sender.send_message(
                entity,
                text,
                parse_mode="html",
                link_preview=False,
                buttons=buttons,
                reply_to=story.get("reply_to_message_id") if is_update else None,
            )
            story["telegram_message_id"] = sent_message.id
            story["telegram_posted"] = True
            story["_post_success"] = True
            run_hashes.update(story_hashes)
            run_content_hashes.update(story_hashes)
            run_video_urls.update(story_video_urls)
            if not is_update:
                successful_new_stories += 1

            label = "UPDATE" if is_update else "NEW"
            media_summary = f"{len(image_buffers)} img, {len(video_buffers)} vid"
            reply_summary = (
                f", reply_to={story.get('reply_to_message_id')}" if is_update else ""
            )
            log(
                f"  [{label}] Posted to Telegram (message_id={sent_message.id}, "
                f"{media_summary}{reply_summary}): {_story_preview(story)}"
            )
            await asyncio.sleep(1.5)

        except Exception as e:
            story["_post_success"] = False
            log(
                f"  [POST-FAILED rank={story.get('_candidate_rank')}] "
                f"{_story_preview(story)}: {e}",
                error=True,
            )

    posted_media_hashes.update(run_hashes)
    log(
        f"Telegram posting complete. {successful_new_stories} new stories posted "
        f"(target={new_story_target}); {len(run_hashes)} new media hashes tracked."
    )
    return posted_media_hashes


def generate_html(stories: list[dict], output_path: str) -> None:
    """Write a standalone HTML file presenting the full history archive."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    sorted_stories = sorted(
        stories,
        key=lambda s: s.get("updated_at") or s.get("created_at") or "",
        reverse=True,
    )

    latest_run_ts = max(
        (s.get("created_at", "") for s in stories), default=""
    )

    latest_run_stories = sorted(
        [s for s in stories
         if s.get("created_at", "") == latest_run_ts
         and latest_run_ts
         and s.get("parent_index") is None],
        key=lambda s: s.get("importance", 99),
    )
    latest_run_rank = {id(s): i + 1 for i, s in enumerate(latest_run_stories)}

    cards_html = ""
    for story in sorted_stories:
        text = html.escape(story.get("text", "")).replace("\n", "<br>")
        is_update = story.get("parent_index") is not None
        rank = latest_run_rank.get(id(story))
        timestamp = story.get("updated_at") or story.get("created_at") or ""
        ts_display = ""
        if timestamp:
            try:
                ts_display = datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                ts_display = timestamp

        if is_update:
            badge = '<span class="badge update-badge">UPDATE</span>'
            card_class = "card card-update"
        elif rank is not None:
            badge = f'<span class="badge rank-badge">#{rank}</span>'
            card_class = "card"
        else:
            badge = ""
            card_class = "card"

        media_block = ""
        for url in story.get("media_urls", []):
            safe_url = html.escape(url)
            media_block += f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer"><img src="{safe_url}" alt="Media" loading="lazy"></a>\n'

        cards_html += f"""
    <article class="{card_class}">
      <div class="card-header">{badge}<span class="timestamp">{ts_display}</span></div>
      <div class="text">{text}</div>
      {media_block}
    </article>"""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alternate Feed Digest — {now}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #0e1117;
    color: #e8eaed;
    padding: 20px;
    max-width: 800px;
    margin: 0 auto;
    line-height: 1.6;
  }}
  h1 {{
    font-size: 1.5rem;
    font-weight: 600;
    margin-bottom: 4px;
    color: #e8eaed;
  }}
  .subtitle {{
    font-size: 0.85rem;
    color: #9aa0a6;
    margin-bottom: 24px;
  }}
  .card {{
    background: #1a1d27;
    border: 1px solid #2d3240;
    border-radius: 14px;
    padding: 20px;
    margin-bottom: 16px;
  }}
  .card-update {{
    background: #1a2027;
    border-left: 3px solid #ff9800;
  }}
  .card-header {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
  }}
  .badge {{
    display: inline-block;
    color: #fff;
    font-size: 0.7rem;
    font-weight: 700;
    padding: 2px 10px;
    border-radius: 20px;
  }}
  .rank-badge {{
    background: #2196f3;
  }}
  .update-badge {{
    background: #ff9800;
  }}
  .timestamp {{
    font-size: 0.75rem;
    color: #9aa0a6;
    margin-left: auto;
  }}
  .text {{
    font-size: 1rem;
    line-height: 1.6;
    color: #e8eaed;
  }}
  img {{
    display: block;
    max-width: 100%;
    max-height: 400px;
    object-fit: contain;
    border-radius: 8px;
    margin-top: 10px;
    cursor: pointer;
  }}
</style>
</head>
<body>
  <h1>Alternate Feed Digest</h1>
  <div class="subtitle">Full archive — {len(sorted_stories)} stories — generated {now}</div>
{cards_html}
</body>
</html>"""

    Path(output_path).write_text(html_content, encoding="utf-8")
    log(f"Digest written to {output_path} ({len(sorted_stories)} stories)")


async def main():
    global HISTORY_PATH

    parser = argparse.ArgumentParser(description="Generate an alternate-feed digest via Mistral AI.")
    parser.add_argument("--server", default=os.environ.get("DIGEST_SERVER_URL", ""), help="Server base URL")
    parser.add_argument("--username", default=os.environ.get("DIGEST_USERNAME", ""), help="Admin username")
    parser.add_argument("--password", default=os.environ.get("DIGEST_PASSWORD", ""), help="Admin password")
    parser.add_argument("--output", default=None, help="Output HTML file path")
    parser.add_argument("--no-telegram", action="store_true", help="Skip posting to Telegram channel")
    parser.add_argument("--test", action="store_true", help="Test mode: use test history file and TEST_DIGEST_TELEGRAM_CHANNEL")
    parser.add_argument("--single", action="store_true", help="Skip AI/dedup; post only the first fetched post to the test channel and exit. Requires --test.")
    args = parser.parse_args()

    if args.test:
        HISTORY_PATH = Path(__file__).parent / "test_digest_history.json"
        log("[TEST MODE] Using test_digest_history.json and TEST_DIGEST_TELEGRAM_CHANNEL")

    if args.output is None:
        args.output = "test_alternate_digest.html" if args.test else "alternate_digest.html"

    base_url = args.server.rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = "http://" + base_url
    username = args.username
    password = args.password

    if not base_url:
        log("Error: server URL required (--server or DIGEST_SERVER_URL env var).", error=True)
        sys.exit(1)
    if not username or not password:
        log("Error: username and password required (--username/--password or DIGEST_USERNAME/DIGEST_PASSWORD env vars).", error=True)
        sys.exit(1)

    config = load_config()
    provider = build_provider(config)
    max_posts_per_call = int(config.get("channel_max_posts_per_call", 100))
    configured_max_stories = int(config.get("channel_max_stories", 3))
    history_validation_enabled = config.get("channel_history_validation_enabled", True)
    log(
        f"History validation is {'enabled' if history_validation_enabled else 'disabled'} "
        f"(config key channel_history_validation_enabled, default=true)."
    )

    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        log(f"Error: prompt file not found at {PROMPT_PATH}", error=True)
        sys.exit(1)

    log(f"Logging in to {base_url} as {username}...")
    token = login(base_url, username, password)

    log("Fetching alternate feed posts...")
    all_posts = fetch_alternate_posts(base_url, token)
    if not all_posts:
        log("No posts in alternate feed. Nothing to process.")
        sys.exit(0)

    if args.single:
        if not args.test:
            log("--single requires --test mode (uses TEST_DIGEST_TELEGRAM_CHANNEL).", error=True)
            sys.exit(1)
        first_post = all_posts[0]
        text = _get_post_text(first_post) or "(empty)"
        log(f"[SINGLE MODE] Posting first fetched post: {text[:80]}...")

        story = {
            "text": text,
            "media_urls": [],
            "video_urls": [],
            "video_thumb_urls": [],
            "source_indices": [0],
            "history_index": None,
        }
        single_stories = [story]
        single_stories = resolve_media_urls(single_stories, all_posts, base_url)
        if POST_VIDEOS:
            single_stories = await resolve_video_urls(single_stories, all_posts)

        # Telegram media albums (multi-file send) silently drop inline buttons.
        # Cap to a single media item so the Perplexity button survives.
        s = single_stories[0]
        if s.get("video_urls"):
            s["video_urls"] = s["video_urls"][:1]
            s["media_urls"] = []
            s["video_thumb_urls"] = []
        elif s.get("media_urls"):
            s["media_urls"] = s["media_urls"][:1]
            s["video_thumb_urls"] = []
        else:
            s["video_thumb_urls"] = s["video_thumb_urls"][:1]

        tg_channel = os.environ.get("TEST_DIGEST_TELEGRAM_CHANNEL", "")
        if not tg_channel:
            log("TEST_DIGEST_TELEGRAM_CHANNEL not set; cannot post.", error=True)
            sys.exit(1)
        if tg_channel.lstrip("-").isdigit():
            tg_channel = int(tg_channel)
        log(f"Posting single story to Telegram channel {tg_channel}...")
        _toc_url = await resolve_toc_url("test", base_url)
        await post_stories_to_telegram(single_stories, tg_channel, set(), toc_url=_toc_url)
        return

    full_history, processed_keys, posted_media_hashes, processed_post_texts = load_history()
    full_history.sort(key=lambda s: s.get("updated_at") or s.get("created_at") or "")
    offset = 0
    max_stories, candidate_count = _story_counts(
        configured_max_stories,
        has_history=bool(full_history),
    )
    if full_history:
        posted_count = len(_posted_history_for_validation(full_history))
        log(
            f"Loaded {len(full_history)} generated stories from history "
            f"({posted_count} treated as previously posted to Telegram)."
        )
    else:
        log(
            f"No history found — first run. Ignoring configured story target "
            f"{configured_max_stories} and requesting exactly {FIRST_RUN_STORY_COUNT} stories."
        )
    prompt = prompt.replace("{max_stories}", str(candidate_count))

    new_posts = [p for p in all_posts if _post_key(p) not in processed_keys]
    log(f"Filtered posts: {len(new_posts)} new out of {len(all_posts)} total ({len(all_posts) - len(new_posts)} already processed).")

    # Deduplicate near-duplicate posts within this batch
    deduplicate_enabled = config.get("deduplicate_posts", True)
    if deduplicate_enabled and len(new_posts) > 1:
        log(f"Running post deduplication on {len(new_posts)} posts...")
        new_posts = deduplicate_posts(new_posts)
    elif not deduplicate_enabled:
        log("Post deduplication is disabled in config.")
    elif len(new_posts) <= 1:
        log(f"Skipping within-batch deduplication (only {len(new_posts)} post(s)).")

    # Filter posts whose content is already covered by previously-processed posts
    if deduplicate_enabled and new_posts and processed_post_texts:
        log(f"Checking {len(new_posts)} posts against {len(processed_post_texts)} stored post texts...")
        new_posts, covered_posts = filter_covered_by_history(new_posts, processed_post_texts)
        if covered_posts:
            for p in covered_posts:
                processed_keys.add(_post_key(p))
                text = _get_post_text(p)
                if text:
                    processed_post_texts[_post_key(p)] = text[:300]

    if not new_posts:
        log("No new posts to process. Regenerating HTML from existing history.")
        # Still save keys for any posts filtered as covered by history
        save_history([], full_history, offset, [], processed_keys, posted_media_hashes, processed_post_texts)
        generate_html(full_history, args.output)
        return

    sent_count = min(len(new_posts), max_posts_per_call)
    log(f"Preparing {sent_count} of {len(new_posts)} new posts for {type(provider).__name__} ({provider.model}) [cap={max_posts_per_call}]...")
    slim = prepare_slim_posts(new_posts, limit=max_posts_per_call)

    user_content = json.dumps({"new_posts": slim}, ensure_ascii=False)

    log(f"Sending to {type(provider).__name__} for analysis...")
    try:
        raw_candidates = call_provider(provider, prompt, user_content)
    except ProviderRequestError as e:
        log(f"Story generation failed: {e}. Nothing to save this run.", error=True)
        sys.exit(1)
    generated_candidates = sorted(
        raw_candidates,
        key=_story_importance_rank,
    )[:candidate_count]
    log(
        f"Model generated {len(generated_candidates)} ranked candidate(s); "
        f"requested={candidate_count}, effective new-story target={max_stories}."
    )

    stories: list[dict] = []
    if history_validation_enabled:
        posted_history = _posted_history_for_validation(full_history)
        accepted_new_count = 0
        for rank, story in enumerate(generated_candidates, start=1):
            if rank > 1:
                time.sleep(VALIDATE_CALL_PACING_SECONDS)
            try:
                decision = validate_story_candidate(story, rank, posted_history, provider)
            except ProviderRequestError as e:
                log(
                    f"Validation stopped early at rank={rank} due to a provider error: {e}. "
                    f"Proceeding with {len(stories)} already-validated stor{'y' if len(stories) == 1 else 'ies'} from this run.",
                    error=True,
                )
                break
            classification = decision["classification"]
            if classification == "duplicate":
                continue

            story["_candidate_rank"] = rank
            story["validation_reason"] = decision["reason"]
            if classification == "update":
                history_index = decision["matched_history_index"]
                matched_story = full_history[history_index]
                original_message_id = matched_story.get("telegram_message_id")
                if original_message_id is None:
                    log(
                        f"  [REJECT-UPDATE rank={rank}] Cannot reply because matched "
                        f"history #{history_index} has no Telegram message ID. "
                        f"existing={_story_preview(matched_story)!r}",
                        error=True,
                    )
                    continue
                story["text"] = decision["additional_info"]
                story["history_index"] = history_index
                story["reply_to_message_id"] = original_message_id
            else:
                story.pop("history_index", None)
                story.pop("reply_to_message_id", None)
            stories.append(story)
            if classification == "new":
                accepted_new_count += 1
                if accepted_new_count >= max_stories:
                    log(
                        f"Reached effective new-story target={max_stories}; "
                        "skipping validation of lower-ranked candidates."
                    )
                    break
    else:
        stories = generated_candidates[:max_stories]
        for rank, story in enumerate(stories, start=1):
            story["_candidate_rank"] = rank
            story.pop("history_index", None)
            story.pop("reply_to_message_id", None)
        log(
            f"History validation disabled; selected the first {len(stories)} "
            "candidate(s) by model importance ranking."
        )

    valid_new_count = sum(s.get("history_index") is None for s in stories)
    update_count = len(stories) - valid_new_count
    log(
        f"Validation complete: {valid_new_count} ranked new candidate(s), "
        f"{update_count} update(s); updates do not count toward target={max_stories}."
    )
    grammar_check_stories(stories, config, new_posts)
    if config.get("hostile_framing_screen_enabled", True):
        stories = screen_hostile_framing(stories, new_posts)
    stories = resolve_media_urls(stories, new_posts, base_url)

    tg_channel = os.environ.get("TEST_DIGEST_TELEGRAM_CHANNEL" if args.test else "DIGEST_TELEGRAM_CHANNEL", "")
    log(f"[DEBUG] args.test={args.test}, tg_channel={repr(tg_channel)}, stories={len(stories)}, POST_TO_CHANNEL={POST_TO_CHANNEL}, no_telegram={args.no_telegram}")
    history_items: list[dict] = []
    if POST_TO_CHANNEL and tg_channel and stories and not args.no_telegram:
        if tg_channel.lstrip("-").isdigit():
            tg_channel = int(tg_channel)
        if POST_VIDEOS:
            log("Resolving video URLs from source posts...")
            stories = await resolve_video_urls(stories, new_posts)
            video_count = sum(len(s.get("video_urls", [])) for s in stories)
            log(f"Found {video_count} video(s) across {len(stories)} stories.")
        else:
            log("Video support disabled; skipping video resolution.")
        log(f"Posting {len(stories)} stories to Telegram channel {tg_channel}...")
        _toc_ch = "test" if args.test else "prod"
        _toc_url = await resolve_toc_url(_toc_ch, base_url)
        posted_media_hashes = await post_stories_to_telegram(
            stories,
            tg_channel,
            posted_media_hashes,
            new_story_target=max_stories,
            toc_url=_toc_url,
        )
        history_items = [story for story in stories if story.get("_post_success")]
    elif args.no_telegram:
        log("--no-telegram flag supplied; skipping Telegram posting.")
    elif not POST_TO_CHANNEL:
        log("POST_TO_CHANNEL is disabled; skipping Telegram posting.")
    elif not tg_channel:
        log("DIGEST_TELEGRAM_CHANNEL not set; skipping Telegram posting.")

    if not (POST_TO_CHANNEL and tg_channel and stories and not args.no_telegram):
        history_items = [
            story
            for story in stories
            if story.get("history_index") is None
        ][:max_stories]
        for story in history_items:
            story["telegram_posted"] = False

    save_history(
        history_items,
        full_history,
        offset,
        new_posts,
        processed_keys,
        posted_media_hashes,
        processed_post_texts,
    )

    write_debug_log(stories, new_posts)

    posted_new_count = sum(
        s.get("history_index") is None and s.get("_post_success") for s in stories
    )
    posted_update_count = sum(
        s.get("history_index") is not None and s.get("_post_success") for s in stories
    )
    log(
        f"Run result: {posted_new_count} new stories posted "
        f"(effective target={max_stories}), {posted_update_count} updates posted. "
        f"Generating HTML from full history ({len(full_history)} stories)..."
    )
    generate_html(full_history, args.output)


async def _run() -> None:
    try:
        await main()
    finally:
        await _send_run_log_to_channel()
        await disconnect_telegram_clients()


if __name__ == "__main__":
    asyncio.run(_run())
