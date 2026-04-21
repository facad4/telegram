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
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

PROMPT_PATH = Path(__file__).parent / "alternate_feed_prompt.md"
CONFIG_PATH = Path(__file__).parent / "config.json"
HISTORY_PATH = Path(__file__).parent / "digest_history.json"

# --- Script Configuration ---
POST_TO_CHANNEL = True   # Post stories to the Telegram channel (requires DIGEST_TELEGRAM_CHANNEL env var)
POST_VIDEOS = False      # Download and post videos from source posts


def log(msg: str, error: bool = False) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr if error else sys.stdout)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def load_history() -> tuple[list[dict], set[str], set[str]]:
    """Load previously generated stories, processed post keys, and posted media hashes."""
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        stories = data.get("stories", [])
        if not isinstance(stories, list):
            stories = []
        keys = data.get("processed_post_keys", [])
        media_hashes = data.get("posted_media_hashes", [])
        return stories, set(keys), set(media_hashes)
    except (FileNotFoundError, json.JSONDecodeError):
        return [], set(), set()


def _story_preview(story: dict, max_len: int = 80) -> str:
    """Return a short text preview for logging."""
    text = story.get("text", "")
    text = text.replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


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
) -> None:
    """Append new stories/updates to history and track processed raw post keys."""
    now = datetime.now().isoformat()
    added, updates = 0, 0

    for story in new_stories:
        hi = story.get("history_index")
        story["created_at"] = now
        story["updated_at"] = now

        if hi is not None:
            real_index = offset + hi
            if 0 <= real_index < len(full_history):
                story["parent_index"] = real_index
                full_history.append(story)
                updates += 1
                log(f"  [UPDATE for #{real_index}] {_story_preview(story)}")
            else:
                story.pop("history_index", None)
                full_history.append(story)
                added += 1
                log(f"  [NEW] {_story_preview(story)}")
        else:
            full_history.append(story)
            added += 1
            log(f"  [NEW] {_story_preview(story)}")

    for post in filtered_posts:
        processed_keys.add(_post_key(post))

    payload = {
        "stories": full_history,
        "processed_post_keys": sorted(processed_keys),
        "posted_media_hashes": sorted(posted_media_hashes) if posted_media_hashes else [],
        "last_updated": now,
    }
    HISTORY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"History saved to {HISTORY_PATH} ({len(full_history)} total stories: {added} new, {updates} updates, {len(processed_keys)} tracked post keys)")


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


def prepare_slim_posts(posts: list[dict]) -> list[dict]:
    """Strip heavy fields and compute engagement ratios for LLM input."""
    slim = []
    for i, p in enumerate(posts[:100]):
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


def call_mistral(api_key: str, model: str, prompt: str, user_content: str) -> dict:
    """Send posts to Mistral and parse the structured JSON response."""
    resp = httpx.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
        },
        timeout=300.0,
    )
    if resp.status_code != 200:
        log(f"Mistral API error (HTTP {resp.status_code}): {resp.text[:500]}", error=True)
        sys.exit(1)

    data = resp.json()
    content = data["choices"][0]["message"]["content"].strip()
    content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        result = json.loads(content)
    except json.JSONDecodeError as e:
        log(f"Failed to parse Mistral response: {e}", error=True)
        log(f"Raw content: {content[:1000]}", error=True)
        sys.exit(1)

    stories = result.get("stories", result if isinstance(result, list) else [])
    if not isinstance(stories, list):
        log("Mistral returned invalid stories format.", error=True)
        sys.exit(1)

    return stories


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
    return parsed.path.rstrip("/").rsplit("/", 1)[-1]


def resolve_media_urls(stories: list[dict], original_posts: list[dict], base_url: str) -> list[dict]:
    """Ensure media_urls in stories contain absolute, deduplicated URLs.

    Layer 1 dedup: URL-path fingerprinting catches CDN variants of the same image.
    """
    for story in stories:
        resolved = []
        seen_fingerprints: set[str] = set()

        def _try_add(url: str | None) -> None:
            if not url:
                return
            abs = _abs_url(url, base_url)
            if not abs:
                return
            fp = _media_url_fingerprint(abs)
            if fp not in seen_fingerprints:
                seen_fingerprints.add(fp)
                resolved.append(abs)

        for url in story.get("media_urls", []):
            _try_add(url)
        for idx in story.get("source_indices", []):
            if 0 <= idx < len(original_posts):
                p = original_posts[idx]
                for field in ("photo_url", "video_thumb"):
                    _try_add(p.get(field) or "")
                lp = p.get("link_preview")
                if lp and lp.get("image"):
                    _try_add(lp["image"])
        story["media_urls"] = resolved
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
    for story in stories:
        video_urls: list[str] = []
        seen: set[str] = set()
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
                video_urls.append(cdn_url)
        story["video_urls"] = video_urls
    return stories


async def connect_telegram() -> TelegramClient | None:
    """Create and connect a Telethon client. Returns None if credentials are missing."""
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
    return client


async def post_stories_to_telegram(
    stories: list[dict],
    channel: str | int,
    posted_media_hashes: set[str],
) -> set[str]:
    """Post stories to a Telegram channel with three-layer media dedup.

    Returns the updated posted_media_hashes set (includes newly posted hashes).
    """
    client = await connect_telegram()
    if client is None:
        log("Telegram credentials missing; skipping channel posting.", error=True)
        return posted_media_hashes

    try:
        entity = await client.get_entity(channel)
        log(f"Posting {len(stories)} items to Telegram channel: {getattr(entity, 'title', channel)}")
    except Exception as e:
        log(f"Failed to resolve Telegram channel '{channel}': {e}", error=True)
        await client.disconnect()
        return posted_media_hashes

    run_hashes: set[str] = set()

    for i, story in enumerate(stories):
        try:
            is_update = story.get("history_index") is not None
            text = story.get("text", "")
            if is_update:
                text = "**עדכון**\n\n" + text
            caption = text[:1024]

            image_buffers = []
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
                for url in story.get("media_urls", [])[:10]:
                    try:
                        resp = await http.get(url)
                        if resp.status_code != 200:
                            continue
                        data = resp.content
                        h = hashlib.md5(data).hexdigest()
                        if h in posted_media_hashes or h in run_hashes:
                            continue
                        run_hashes.add(h)
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
                for url in story.get("video_urls", [])[:5]:
                    try:
                        resp = await http.get(url)
                        if resp.status_code != 200:
                            continue
                        data = resp.content
                        h = hashlib.md5(data).hexdigest()
                        if h in posted_media_hashes or h in run_hashes:
                            continue
                        run_hashes.add(h)
                        buf = io.BytesIO(data)
                        buf.name = f"video_{i}_{len(video_buffers)}.mp4"
                        video_buffers.append(buf)
                    except Exception as e:
                        log(f"  Failed to download video {url}: {e}", error=True)

            caption_used = False

            if image_buffers:
                await client.send_file(
                    entity,
                    file=image_buffers,
                    caption=caption,
                    parse_mode="md",
                    force_document=False,
                )
                caption_used = True

            for vi, vbuf in enumerate(video_buffers):
                await client.send_file(
                    entity,
                    file=vbuf,
                    caption=caption if not caption_used else "",
                    parse_mode="md",
                    supports_streaming=True,
                )
                caption_used = True
                if vi < len(video_buffers) - 1:
                    await asyncio.sleep(2)

            if not image_buffers and not video_buffers:
                await client.send_message(entity, text, parse_mode="md")

            label = "UPDATE" if is_update else "NEW"
            media_summary = f"{len(image_buffers)} img, {len(video_buffers)} vid"
            log(f"  [{label}] Posted to Telegram ({media_summary}): {_story_preview(story)}")
            await asyncio.sleep(1.5)

        except Exception as e:
            log(f"  Failed to post story {i}: {e}", error=True)

    posted_media_hashes.update(run_hashes)
    await client.disconnect()
    log(f"Telegram posting complete. {len(run_hashes)} new media hashes tracked.")
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


def main():
    parser = argparse.ArgumentParser(description="Generate an alternate-feed digest via Mistral AI.")
    parser.add_argument("--server", default=os.environ.get("DIGEST_SERVER_URL", ""), help="Server base URL")
    parser.add_argument("--username", default=os.environ.get("DIGEST_USERNAME", ""), help="Admin username")
    parser.add_argument("--password", default=os.environ.get("DIGEST_PASSWORD", ""), help="Admin password")
    parser.add_argument("--output", default="alternate_digest.html", help="Output HTML file path")
    args = parser.parse_args()

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

    mistral_key = os.environ.get("MISTRAL_API_KEY", "")
    if not mistral_key:
        log("Error: MISTRAL_API_KEY environment variable required.", error=True)
        sys.exit(1)

    config = load_config()
    model = config.get("context_mistral_model", "mistral-large-latest")

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

    full_history, processed_keys, posted_media_hashes = load_history()
    full_history.sort(key=lambda s: s.get("updated_at") or s.get("created_at") or "")
    recent_history = full_history[-100:]
    offset = len(full_history) - len(recent_history)
    if full_history:
        log(f"Loaded {len(full_history)} stories from history (sending last {len(recent_history)} to Mistral, sorted by recency).")
    else:
        log("No history found — first run.")

    new_posts = [p for p in all_posts if _post_key(p) not in processed_keys]
    log(f"Filtered posts: {len(new_posts)} new out of {len(all_posts)} total ({len(all_posts) - len(new_posts)} already processed).")

    if not new_posts:
        log("No new posts to process. Regenerating HTML from existing history.")
        generate_html(full_history, args.output)
        return

    log(f"Preparing {len(new_posts)} new posts for Mistral ({model})...")
    slim = prepare_slim_posts(new_posts)

    user_content = json.dumps(
        {"new_posts": slim, "previously_generated": recent_history},
        ensure_ascii=False,
    )

    log("Sending to Mistral AI for analysis...")
    stories = call_mistral(mistral_key, model, prompt, user_content)
    stories = resolve_media_urls(stories, new_posts, base_url)

    save_history(stories, full_history, offset, new_posts, processed_keys, posted_media_hashes)

    tg_channel = os.environ.get("DIGEST_TELEGRAM_CHANNEL", "")
    if POST_TO_CHANNEL and tg_channel and stories:
        if tg_channel.lstrip("-").isdigit():
            tg_channel = int(tg_channel)
        if POST_VIDEOS:
            log("Resolving video URLs from source posts...")
            stories = asyncio.run(resolve_video_urls(stories, new_posts))
            video_count = sum(len(s.get("video_urls", [])) for s in stories)
            log(f"Found {video_count} video(s) across {len(stories)} stories.")
        else:
            log("Video support disabled; skipping video resolution.")
        log(f"Posting {len(stories)} stories to Telegram channel {tg_channel}...")
        posted_media_hashes = asyncio.run(
            post_stories_to_telegram(stories, tg_channel, posted_media_hashes)
        )
        save_history([], full_history, offset, [], processed_keys, posted_media_hashes)
    elif not POST_TO_CHANNEL:
        log("POST_TO_CHANNEL is disabled; skipping Telegram posting.")
    elif not tg_channel:
        log("DIGEST_TELEGRAM_CHANNEL not set; skipping Telegram posting.")

    log(f"Received {len(stories)} new items. Generating HTML from full history ({len(full_history)} stories)...")
    generate_html(full_history, args.output)


if __name__ == "__main__":
    main()
