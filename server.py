import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import jwt
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient, Button, functions
from telethon.sessions import StringSession
from perplexity_marker import PX_BUTTON_LABEL, TOC_BUTTON_LABEL, toc_page_url
from telethon.tl.types import Channel, Chat, MessageMediaPhoto, MessageMediaDocument, PeerChannel, DocumentAttributeVideo
from google import genai
from google.genai import types

from database import Database
from web_search import WebSearch

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

JWT_SECRET = secrets.token_hex(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 30

# Today's-headlines page (GET /digest/today)
try:
    _TOC_TZ = ZoneInfo(os.environ.get("DIGEST_TODAY_TZ", "Asia/Jerusalem"))
except Exception:
    _TOC_TZ = timezone.utc
_TOC_CACHE_TTL = 120
_toc_cache: dict[str, tuple[float, str]] = {}


async def require_auth(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth.removeprefix("Bearer ")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = await Database.create()
    app.state.db = db

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    session_str = os.environ.get("TELEGRAM_SESSION")
    app.state.telegram = None

    if api_id and api_hash and session_str:
        try:
            tg = TelegramClient(StringSession(session_str), int(api_id), api_hash)
            await tg.connect()
            if await tg.is_user_authorized():
                app.state.telegram = tg
                logger.info("Telegram client connected for channel search")
            else:
                logger.warning("Telegram session is not authorized; channel search disabled")
                await tg.disconnect()
        except Exception as e:
            logger.warning("Failed to initialize Telegram client: %s", e)
    else:
        logger.info("Telegram API credentials not configured; channel search disabled")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    app.state.bot = None
    if bot_token and api_id and api_hash:
        try:
            bot = TelegramClient(StringSession(), int(api_id), api_hash)
            await bot.start(bot_token=bot_token)
            app.state.bot = bot
            logger.info("Telegram bot client connected")
        except Exception as e:
            logger.warning("Telegram bot client failed to initialize: %s", e)

    yield

    if app.state.telegram:
        await app.state.telegram.disconnect()
        logger.info("Telegram client disconnected")
    if app.state.bot:
        await app.state.bot.disconnect()
        logger.info("Telegram bot client disconnected")


app = FastAPI(lifespan=lifespan)

CONFIG_PATH = Path(__file__).parent / "config.json"
TOP10_PROMPT_PATH = Path(__file__).parent / "top10_prompt.md"
CONTEXT_SUMMARY_PROMPT_PATH = Path(__file__).parent / "context_summary_prompt.md"
GROQ_API_KEY = os.environ.get("GROK_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
AVATAR_CACHE_DIR = Path(__file__).parent / "avatar_cache"
AVATAR_CACHE_DIR.mkdir(exist_ok=True)
THUMB_CACHE_DIR = Path(__file__).parent / "thumb_cache"
THUMB_CACHE_DIR.mkdir(exist_ok=True)
AVATAR_TTL_SECONDS = 1_209_600  # 2 weeks
THUMB_TTL_SECONDS = 1_209_600   # 2 weeks
PENDING_THUMBS_TTL = 600        # 10 minutes

_pending_thumbs: dict[str, tuple[float, object]] = {}
_video_refs: dict[str, tuple[object, int]] = {}
_video_cache: dict[str, tuple[float, bytes]] = {}
_video_locks: dict[str, asyncio.Lock] = {}
_video_download_tasks: dict[str, tuple[asyncio.Task, list[bytes]]] = {}
_video_cdn_cache: dict[str, tuple[float, str]] = {}
_telegram_download_semaphore = asyncio.Semaphore(5)
VIDEO_MEMORY_TTL = 300
CDN_URL_TTL = 3600
_telegram_reconnect_lock = asyncio.Lock()


async def _ensure_telegram_connected(tg: TelegramClient) -> bool:
    """Reconnect a dropped Telethon client. Concurrent callers share one reconnect attempt."""
    if tg.is_connected():
        return True
    async with _telegram_reconnect_lock:
        if tg.is_connected():
            return True
        try:
            await tg.connect()
            logger.info("Telegram client reconnected")
        except Exception as e:
            logger.error("Telegram client reconnect failed: %s", e)
        return tg.is_connected()


def _cleanup_video_cache():
    cutoff = time.time() - VIDEO_MEMORY_TTL
    stale = [k for k, (ts, _) in _video_cache.items() if ts < cutoff]
    for k in stale:
        del _video_cache[k]
        _video_locks.pop(k, None)


async def scrape_telegram_video_url(channel_username: str, message_id: int) -> str | None:
    """Scrape Telegram's embed page to extract the direct CDN video URL."""
    cache_key = f"{channel_username}_{message_id}"
    
    cached = _video_cdn_cache.get(cache_key)
    if cached:
        ts, url = cached
        if time.time() - ts < CDN_URL_TTL:
            return url
    
    try:
        embed_url = f"https://t.me/{channel_username}/{message_id}?embed=1"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(embed_url)
            if response.status_code != 200:
                return None
            
            soup = BeautifulSoup(response.text, 'html.parser')
            video_tag = soup.find('video')
            if video_tag:
                src = video_tag.get('src')
                if src:
                    _video_cdn_cache[cache_key] = (time.time(), src)
                    logging.info(f"Scraped CDN URL for {cache_key}: {src[:80]}...")
                    return src
    except Exception as e:
        logging.warning(f"Failed to scrape video URL for {cache_key}: {e}")
    
    return None


def _cleanup_pending_thumbs():
    """Remove stashed media refs older than PENDING_THUMBS_TTL."""
    cutoff = time.time() - PENDING_THUMBS_TTL
    stale = [k for k, (ts, _) in _pending_thumbs.items() if ts < cutoff]
    for k in stale:
        del _pending_thumbs[k]


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def normalize_channel(raw: str) -> str:
    """Extract the channel name from a full URL, @handle, or plain name."""
    raw = raw.strip().rstrip("/")
    if raw.startswith("https://t.me/"):
        raw = raw.removeprefix("https://t.me/")
    elif raw.startswith("http://t.me/"):
        raw = raw.removeprefix("http://t.me/")
    elif raw.startswith("t.me/"):
        raw = raw.removeprefix("t.me/")
    if raw.startswith("s/"):
        raw = raw.removeprefix("s/")
    if raw.startswith("@"):
        raw = raw[1:]
    return raw.split("/")[0]


async def fetch_channel_html(client: httpx.AsyncClient, channel: str) -> str | None:
    url = f"https://t.me/s/{channel}"
    try:
        logger.info("Fetching %s", url)
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        logger.info("Fetched %s: %d bytes", url, len(resp.text))
        return resp.text
    except Exception as e:
        logger.error("Failed to fetch %s: %s", url, e)
        return None


def extract_image_url(style: str) -> str | None:
    match = re.search(r"url\('([^']+)'\)", style)
    return match.group(1) if match else None


def _merge_grouped_posts(posts: list[dict]) -> list[dict]:
    """Merge consecutive posts with sequential IDs that form a media group.

    Telegram renders grouped media (albums) as separate messages where only
    the last item carries the text.  We detect runs of consecutive numeric
    post_ids, collect the shared text/link-preview from whichever item has it,
    and emit a single merged post per group (keeping the first media).
    """
    if not posts:
        return posts

    groups: list[list[dict]] = [[posts[0]]]
    for prev, cur in zip(posts, posts[1:]):
        try:
            if int(cur["post_id"]) == int(prev["post_id"]) + 1:
                groups[-1].append(cur)
                continue
        except (ValueError, TypeError):
            pass
        groups.append([cur])

    merged: list[dict] = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue

        has_empty = any(not p["text_html"] for p in group)
        if not has_empty:
            merged.extend(group)
            continue

        text_post = next((p for p in group if p["text_html"]), None)
        if text_post is None:
            merged.extend(group)
            continue

        base = group[0].copy()
        base["text_html"] = text_post["text_html"]
        base["text_plain"] = text_post["text_plain"]
        if not base["views"] and text_post["views"]:
            base["views"] = text_post["views"]
        if not base["datetime"] and text_post["datetime"]:
            base["datetime"] = text_post["datetime"]
        if text_post["link_preview"] and not base["link_preview"]:
            base["link_preview"] = text_post["link_preview"]
        merged.append(base)

    return merged


def _merge_telethon_grouped(posts: list[dict]) -> list[dict]:
    """Merge Telethon messages that share a grouped_id (media albums).

    Keeps the first post's media and copies text from whichever item has it.
    Strips the internal _grouped_id key from the output.
    """
    from collections import OrderedDict
    groups: OrderedDict[int, list[dict]] = OrderedDict()
    ungrouped: list[tuple[int, dict]] = []

    for i, p in enumerate(posts):
        gid = p.get("_grouped_id")
        if gid is not None:
            groups.setdefault(gid, []).append((i, p))
        else:
            ungrouped.append((i, p))

    merged_indexed: list[tuple[int, dict]] = []
    for gid, items in groups.items():
        text_post = next((p for _, p in items if p["text_html"]), None)
        base = items[0][1].copy()
        if text_post:
            base["text_html"] = text_post["text_html"]
            base["text_plain"] = text_post["text_plain"]
        if not base["views"]:
            donor = next((p for _, p in items if p["views"]), None)
            if donor:
                base["views"] = donor["views"]
        base.pop("_grouped_id", None)
        merged_indexed.append((items[0][0], base))

    for i, p in ungrouped:
        p.pop("_grouped_id", None)
        merged_indexed.append((i, p))

    merged_indexed.sort(key=lambda x: x[0])
    return [p for _, p in merged_indexed]


def parse_channel_posts(html: str, channel: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    channel_title = channel
    channel_photo = None
    title_el = soup.select_one(".tgme_channel_info_header_title span")
    if title_el:
        channel_title = title_el.get_text(strip=True)
    photo_el = soup.select_one(".tgme_channel_info_header .tgme_page_photo_image img")
    if photo_el:
        channel_photo = photo_el.get("src")

    for msg in soup.select(".tgme_widget_message"):
        data_post = msg.get("data-post", "")
        post_id = data_post.split("/")[-1] if "/" in data_post else data_post

        text_el = msg.select_one(".tgme_widget_message_text")
        text_html = str(text_el) if text_el else ""
        text_plain = text_el.get_text(separator=" ", strip=True) if text_el else ""

        views_el = msg.select_one(".tgme_widget_message_views")
        views = views_el.get_text(strip=True) if views_el else ""

        time_el = msg.select_one("time[datetime]")
        datetime_str = time_el.get("datetime", "") if time_el else ""

        photo_el = msg.select_one(".tgme_widget_message_photo_wrap")
        photo_url = None
        if photo_el and photo_el.get("style"):
            photo_url = extract_image_url(photo_el["style"])

        video_thumb_el = msg.select_one(".tgme_widget_message_video_thumb")
        video_thumb = None
        if video_thumb_el and video_thumb_el.get("style"):
            video_thumb = extract_image_url(video_thumb_el["style"])

        # When the message has a video, the photo wrap (if any) is just the
        # video's still preview. Drop it so downstream consumers don't attach
        # the still as a separate image alongside the video.
        if video_thumb is not None:
            photo_url = None

        link_preview = None
        lp_el = msg.select_one(".tgme_widget_message_link_preview")
        if lp_el:
            lp_title = lp_el.select_one(".link_preview_title")
            lp_desc = lp_el.select_one(".link_preview_description")
            lp_img = lp_el.select_one(".link_preview_image")
            link_preview = {
                "url": lp_el.get("href", ""),
                "title": lp_title.get_text(strip=True) if lp_title else "",
                "description": lp_desc.get_text(strip=True) if lp_desc else "",
                "image": extract_image_url(lp_img["style"]) if lp_img and lp_img.get("style") else None,
            }

        posts.append({
            "channel": channel,
            "channel_title": channel_title,
            "channel_photo": channel_photo,
            "post_id": post_id,
            "post_url": f"https://t.me/{data_post}",
            "text_html": text_html,
            "text_plain": text_plain,
            "views": views,
            "datetime": datetime_str,
            "photo_url": photo_url,
            "video_thumb": video_thumb,
            "video_url": None,
            "has_video": video_thumb is not None,
            "link_preview": link_preview,
            "channel_subscribers": None,
        })

    return _merge_grouped_posts(posts)


async def _download_thumb(
    tg: TelegramClient, media, sem: asyncio.Semaphore, cache_key: str,
) -> str | None:
    """Download a photo thumbnail, caching to disk. Returns a URL path."""
    cache_path = THUMB_CACHE_DIR / f"{cache_key}.jpg"

    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < THUMB_TTL_SECONDS:
        return f"/api/thumb/{cache_key}"

    async with sem:
        try:
            buf = io.BytesIO()
            await tg.download_media(media, file=buf, thumb=-1)
            buf.seek(0)
            data = buf.read()
            if data:
                cache_path.write_bytes(data)
                return f"/api/thumb/{cache_key}"
            return None
        except Exception:
            return None


async def fetch_channel_posts_telethon(
    tg: TelegramClient, channel_ref: int | str, limit: int = 30,
    media_sem: asyncio.Semaphore | None = None,
) -> list[dict]:
    """Fetch recent posts from a channel via Telethon API.

    channel_ref: username (str) for public channels, numeric ID (int) for private.
    media_sem: shared semaphore that caps concurrent media downloads.
    Retries once (after a shared reconnect) if the client had dropped its connection.
    """
    if media_sem is None:
        media_sem = asyncio.Semaphore(5)

    try:
        return await _fetch_channel_posts_telethon_once(tg, channel_ref, limit, media_sem)
    except ConnectionError as e:
        logger.warning("Telethon disconnected while fetching %s, reconnecting: %s", channel_ref, e)
        if not await _ensure_telegram_connected(tg):
            logger.error("Failed to fetch channel %s via Telethon: reconnect failed", channel_ref)
            return []
        try:
            return await _fetch_channel_posts_telethon_once(tg, channel_ref, limit, media_sem)
        except Exception as e2:
            logger.error("Failed to fetch channel %s via Telethon after reconnect: %s", channel_ref, e2)
            return []
    except Exception as e:
        logger.error("Failed to fetch channel %s via Telethon: %s", channel_ref, e)
        return []


async def _fetch_channel_posts_telethon_once(
    tg: TelegramClient, channel_ref: int | str, limit: int,
    media_sem: asyncio.Semaphore,
) -> list[dict]:
    entity = await tg.get_entity(channel_ref)
    channel_title = getattr(entity, "title", str(channel_ref))
    username = getattr(entity, "username", None)

    cache_key = str(username or getattr(entity, "id", channel_ref)).replace("/", "_")
    cache_path = AVATAR_CACHE_DIR / f"{cache_key}.jpg"
    channel_photo = None

    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < AVATAR_TTL_SECONDS:
        channel_photo = f"/api/avatar/{cache_key}"
    else:
        try:
            photo_file = await tg.download_profile_photo(entity, file=bytes)
            if photo_file:
                cache_path.write_bytes(photo_file)
                channel_photo = f"/api/avatar/{cache_key}"
        except Exception:
            pass

    messages = await tg.get_messages(entity, limit=limit)

    filtered = [m for m in messages if m.text is not None or m.media is not None]

    if username:
        channel_name = username
    else:
        real_id = getattr(entity, "id", channel_ref)
        channel_name = str(real_id)

    _cleanup_pending_thumbs()

    raw_posts = []
    for msg in filtered:
        text_plain = msg.text or ""
        text_html = f"<div>{text_plain.replace(chr(10), '<br>')}</div>" if text_plain else ""
        views = str(msg.views) if msg.views else ""
        datetime_str = msg.date.isoformat() if msg.date else ""

        if username:
            post_url = f"https://t.me/{username}/{msg.id}"
        else:
            post_url = f"https://t.me/c/{real_id}/{msg.id}"

        photo_url = None
        has_video = False
        video_thumb = None
        video_url = None

        if isinstance(msg.media, MessageMediaPhoto):
            thumb_key = f"{channel_name}_{msg.id}"
            cache_path = THUMB_CACHE_DIR / f"{thumb_key}.jpg"
            if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < THUMB_TTL_SECONDS:
                photo_url = f"/api/thumb/{thumb_key}"
            else:
                _pending_thumbs[thumb_key] = (time.time(), msg.media)
                photo_url = f"/api/thumb/{thumb_key}"
        elif isinstance(msg.media, MessageMediaDocument) and username:
            doc = msg.media.document
            if doc and any(isinstance(a, DocumentAttributeVideo) for a in (doc.attributes or [])):
                has_video = True
                video_key = f"{channel_name}_{msg.id}"
                _video_refs[video_key] = (msg.media, doc.size or 0)
                video_url = None

                thumb_key = f"{channel_name}_{msg.id}"
                _pending_thumbs[thumb_key] = (time.time(), msg.media)
                video_thumb = f"/api/thumb/{thumb_key}"

        raw_posts.append({
            "channel": channel_name,
            "channel_title": channel_title,
            "channel_photo": channel_photo,
            "post_id": str(msg.id),
            "post_url": post_url,
            "reply_to_msg_id": (
                str(msg.reply_to_msg_id) if getattr(msg, "reply_to_msg_id", None) else None
            ),
            "text_html": text_html,
            "text_plain": text_plain,
            "views": views,
            "datetime": datetime_str,
            "photo_url": photo_url,
            "video_thumb": video_thumb,
            "video_url": video_url,
            "has_video": has_video,
            "link_preview": None,
            "channel_subscribers": getattr(entity, "participants_count", None),
            "_grouped_id": getattr(msg, "grouped_id", None),
        })

    posts = _merge_telethon_grouped(raw_posts)
    return posts


@app.get("/api/avatar/{channel_key}")
async def get_avatar(channel_key: str):
    path = AVATAR_CACHE_DIR / f"{channel_key}.jpg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/thumb/{thumb_key}")
async def get_thumb(thumb_key: str, request: Request):
    path = THUMB_CACHE_DIR / f"{thumb_key}.jpg"
    if path.exists():
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=604800"})

    entry = _pending_thumbs.pop(thumb_key, None)
    tg: TelegramClient | None = request.app.state.telegram
    if entry and tg:
        _, media = entry
        try:
            buf = io.BytesIO()
            await tg.download_media(media, file=buf, thumb=-1)
            buf.seek(0)
            data = buf.read()
            if data:
                path.write_bytes(data)
                return FileResponse(path, media_type="image/jpeg",
                                    headers={"Cache-Control": "public, max-age=604800"})
        except Exception:
            logger.debug("On-demand thumb download failed for %s", thumb_key)

    raise HTTPException(status_code=404, detail="Thumbnail not found")


def _parse_range(range_header: str, file_size: int) -> tuple[int, int]:
    """Parse an HTTP Range header, returning (start, end) inclusive."""
    spec = range_header.replace("bytes=", "").strip()
    parts = spec.split("-", 1)
    start = int(parts[0]) if parts[0] else 0
    end = int(parts[1]) if parts[1] else file_size - 1
    end = min(end, file_size - 1)
    return start, end


@app.get("/api/video/{video_key}")
async def get_video(video_key: str, request: Request):
    _cleanup_video_cache()
    
    ref = _video_refs.get(video_key)
    tg: TelegramClient | None = request.app.state.telegram
    if not ref or not tg:
        raise HTTPException(status_code=404, detail="Video not found")
    
    media, file_size = ref
    range_header = request.headers.get("range")
    
    cached = _video_cache.get(video_key)
    if cached:
        _, data = cached
        logging.info(f"Video {video_key}: Serving from cache ({len(data)} bytes)")
        
        if range_header:
            start, end = _parse_range(range_header, len(data))
            return Response(
                content=data[start:end + 1],
                status_code=206,
                media_type="video/mp4",
                headers={
                    "Content-Range": f"bytes {start}-{end}/{len(data)}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(end - start + 1),
                },
            )
        return Response(
            content=data,
            media_type="video/mp4",
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(data)),
            },
        )
    
    if range_header:
        start, end = _parse_range(range_header, file_size)
        length = end - start + 1
        
        async def stream_range():
            sent = 0
            async with _telegram_download_semaphore:
                logging.info(f"Video {video_key}: Streaming range {start}-{end} from Telegram...")
                async for chunk in tg.iter_download(media, offset=start, request_size=65536):
                    if isinstance(chunk, bytes):
                        remaining = length - sent
                        if remaining <= 0:
                            break
                        to_send = chunk[:remaining]
                        yield to_send
                        sent += len(to_send)
        
        return StreamingResponse(
            stream_range(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
            },
        )
    
    async def stream_and_cache_full():
        chunks = []
        
        async with _telegram_download_semaphore:
            logging.info(f"Video {video_key}: Streaming full video from Telegram...")
            async for chunk in tg.iter_download(media, request_size=65536):
                if isinstance(chunk, bytes):
                    chunks.append(chunk)
                    yield chunk
        
        if video_key not in _video_locks:
            _video_locks[video_key] = asyncio.Lock()
        async with _video_locks[video_key]:
            if video_key not in _video_cache:
                full_data = b''.join(chunks)
                _video_cache[video_key] = (time.time(), full_data)
                logging.info(f"Video {video_key}: Cached {len(full_data)} bytes after streaming")
    
    return StreamingResponse(
        stream_and_cache_full(),
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
        },
    )


@app.get("/api/video/cdn/{channel_username}/{message_id}")
async def get_video_cdn_url(channel_username: str, message_id: int):
    cdn_url = await scrape_telegram_video_url(channel_username, message_id)
    if cdn_url:
        return {"cdn_url": cdn_url}
    raise HTTPException(status_code=404, detail="Could not extract video CDN URL")


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    user_name = body.get("user_name", "")
    password = body.get("password", "")
    db: Database = request.app.state.db
    user = await db.authenticate_user(user_name, password)
    if not user:
        return JSONResponse(
            status_code=401,
            content={"detail": "Login failed, incorrect credentials"},
        )
    token = jwt.encode(
        {
            "user_name": user["user_name"],
            "user_id": user["id"],
            "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    logger.info("User '%s' (id=%s) logged in successfully", user["user_name"], user["id"])
    return {"token": token, "user_name": user["user_name"], "is_admin": user["id"] == 1}


@app.get("/api/posts")
async def get_posts(request: Request, user: dict = Depends(require_auth)):
    db: Database = request.app.state.db
    feeds = await db.get_feeds_for_user(user["user_id"])
    feeds = [f for f in feeds if not f.get("is_alternate")]
    config = load_config()
    max_posts = config.get("max_posts", 100)
    fetch_limit = config.get("fetch_per_channel", 50)

    public_feeds = [f for f in feeds if not f.get("is_private") and f.get("feed_url")]
    private_feeds = [f for f in feeds if f.get("is_private") and f.get("feed_url")]

    tg: TelegramClient | None = request.app.state.telegram
    all_posts = []

    if tg:
        media_sem = asyncio.Semaphore(config.get("media_concurrency", 5))
        telethon_tasks = []
        for f in public_feeds:
            username = normalize_channel(f["feed_url"])
            telethon_tasks.append(fetch_channel_posts_telethon(tg, username, limit=fetch_limit, media_sem=media_sem))
        for f in private_feeds:
            telethon_tasks.append(fetch_channel_posts_telethon(tg, PeerChannel(int(f["feed_url"])), limit=fetch_limit, media_sem=media_sem))
        if telethon_tasks:
            results = await asyncio.gather(*telethon_tasks)
            for posts in results:
                all_posts.extend(posts)
    else:
        public_channels = [normalize_channel(f["feed_url"]) for f in public_feeds]
        async with httpx.AsyncClient(timeout=15.0) as client:
            results = await asyncio.gather(
                *[fetch_channel_html(client, ch) for ch in public_channels]
            )
        for ch, html in zip(public_channels, results):
            if html:
                all_posts.extend(parse_channel_posts(html, ch))

    all_posts.sort(key=lambda p: p["datetime"], reverse=True)
    return all_posts[:max_posts]


@app.get("/api/alternate-posts")
async def get_alternate_posts(request: Request, user: dict = Depends(require_auth)):
    """Return posts from alternate-feed channels (admin only)."""
    if user.get("user_id") != 1:
        raise HTTPException(status_code=403, detail="Admin access required")

    db: Database = request.app.state.db
    feeds = await db.get_alternate_feeds()
    config = load_config()
    max_posts = config.get("alternate_feed_max_posts", 100)
    fetch_limit = config.get("fetch_per_channel", 50)

    public_feeds = [f for f in feeds if not f.get("is_private") and f.get("feed_url")]
    private_feeds = [f for f in feeds if f.get("is_private") and f.get("feed_url")]

    tg: TelegramClient | None = request.app.state.telegram
    all_posts = []

    if tg:
        media_sem = asyncio.Semaphore(config.get("media_concurrency", 5))
        telethon_tasks = []
        for f in public_feeds:
            username = normalize_channel(f["feed_url"])
            telethon_tasks.append(fetch_channel_posts_telethon(tg, username, limit=fetch_limit, media_sem=media_sem))
        for f in private_feeds:
            telethon_tasks.append(fetch_channel_posts_telethon(tg, PeerChannel(int(f["feed_url"])), limit=fetch_limit, media_sem=media_sem))
        if telethon_tasks:
            results = await asyncio.gather(*telethon_tasks)
            for posts in results:
                all_posts.extend(posts)
    else:
        public_channels = [normalize_channel(f["feed_url"]) for f in public_feeds]
        async with httpx.AsyncClient(timeout=15.0) as client:
            results = await asyncio.gather(
                *[fetch_channel_html(client, ch) for ch in public_channels]
            )
        for ch, html in zip(public_channels, results):
            if html:
                all_posts.extend(parse_channel_posts(html, ch))

    all_posts.sort(key=lambda p: p["datetime"], reverse=True)
    logger.info("Admin requested alternate feed (%d posts)", len(all_posts[:max_posts]))
    return all_posts[:max_posts]


def _digest_channel_ref() -> int | str:
    channel = os.environ.get("DIGEST_TELEGRAM_CHANNEL")
    if not channel:
        raise HTTPException(status_code=503, detail="DIGEST_TELEGRAM_CHANNEL not configured")
    return int(channel) if channel.lstrip("-").isdigit() else channel


def _toc_channel_ref(ch: str) -> int | str | None:
    """Resolve the ?ch= param ('test'/'prod') to a Telethon entity ref."""
    raw = os.environ.get(
        "TEST_DIGEST_TELEGRAM_CHANNEL" if ch == "test" else "DIGEST_TELEGRAM_CHANNEL"
    )
    if not raw:
        return None
    return int(raw) if raw.lstrip("-").isdigit() else raw


def _toc_button_rows(bot: TelegramClient | None, target: str,
                     include_px: bool = True) -> list | None:
    """Inline-keyboard rows for a digest channel post: Perplexity + today's headlines.

    Returns None when no bot client is available (a user account cannot attach
    inline keyboards). The headlines row is added only when DIGEST_SERVER_URL and
    DIGEST_TOC_KEY are both set.
    """
    if bot is None:
        return None
    row: list = []
    if include_px:
        row.append(Button.inline(PX_BUTTON_LABEL, b"px"))
    base = os.environ.get("DIGEST_SERVER_URL", "")
    key = os.environ.get("DIGEST_TOC_KEY", "")
    if base and key:
        ch = "test" if target == "test" else "prod"
        row.append(Button.url(TOC_BUTTON_LABEL, toc_page_url(base, ch, key)))
    return [row] if row else None


_TOC_UPDATE_PREFIX = "עדכון"


def _toc_header(text_plain: str) -> tuple[str, bool]:
    """Extract a clean one-line headline from a post -> (headline, is_update).

    Digest updates lead with a bare ``**עדכון**`` line followed by the real
    headline; a marker-only line is skipped in favour of the next line.
    """
    is_update = False
    header = ""
    for raw in text_plain.splitlines():
        cleaned = raw.strip().lstrip("#> \t").replace("*", "").replace("__", "").strip("_\"' \t")
        if not cleaned:
            continue
        if not is_update and cleaned.startswith(_TOC_UPDATE_PREFIX):
            is_update = True
            rest = cleaned[len(_TOC_UPDATE_PREFIX):].lstrip(":-–— \t").strip()
            if rest:
                header = rest
                break
            continue  # marker-only line; use the next one
        header = cleaned
        break
    header = " ".join(header.split())
    if len(header) > 100:
        header = header[:100].rstrip() + "…"
    return header, is_update


_TOC_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_TOC_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def _toc_local_dt(post: dict) -> datetime | None:
    """Parse a post's ISO datetime into an _TOC_TZ-local aware datetime, or None."""
    try:
        dt = datetime.fromisoformat(post.get("datetime") or "")
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_TOC_TZ)


def _toc_bubble_parts(text_plain: str) -> tuple[bool, str]:
    """Render a channel message for a thread bubble -> (is_update, safe_html_body).

    A leading bare ``**עדכון**`` marker line is stripped and reported via the flag.
    Only <b> and http(s) <a> tags survive; everything else is HTML-escaped.
    """
    lines = (text_plain or "").splitlines()
    is_update = False
    start = 0
    for i, raw in enumerate(lines):
        cleaned = raw.strip().lstrip("#> \t").replace("*", "").replace("__", "").strip("_\"' \t")
        if not cleaned:
            start = i + 1
            continue
        marker = cleaned.startswith(_TOC_UPDATE_PREFIX) and not cleaned[
            len(_TOC_UPDATE_PREFIX):
        ].lstrip(":-–— \t").strip()
        if marker:
            is_update = True
            start = i + 1
            continue
        break
    body = "\n".join(lines[start:]).strip()

    tokens: list[str] = []

    def _stash(htmls: str) -> str:
        tokens.append(htmls)
        return f"\x00{len(tokens) - 1}\x00"

    body = _TOC_LINK_RE.sub(
        lambda m: _stash(
            f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'
        ),
        body,
    )
    body = _TOC_BOLD_RE.sub(lambda m: _stash(f"<b>{html.escape(m.group(1))}</b>"), body)
    body = html.escape(body).replace("\n", "<br>")
    body = re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], body)
    return is_update, body


def _toc_render_thread(modal_id: str, channel_title: str, initial: str,
                       original: dict, updates: list[dict]) -> str:
    """Hidden popup markup: the original post + its updates as Telegram-style bubbles."""
    def _bubble(post: dict) -> str:
        is_upd, body = _toc_bubble_parts(post.get("text_plain") or "")
        label = '<div class="bubble-label">עדכון</div>' if is_upd else ""
        local = _toc_local_dt(post)
        time_html = (
            f'<div class="bubble-time">{local.strftime("%H:%M")}</div>' if local else ""
        )
        url = post.get("post_url") or ""
        if url:
            return (
                f'<a class="bubble" href="{html.escape(url, quote=True)}">'
                f'{label}<div class="bubble-body">{body}</div>{time_html}</a>'
            )
        return (
            f'<div class="bubble">{label}<div class="bubble-body">{body}</div>'
            f'{time_html}</div>'
        )

    bubbles = _bubble(original) + "".join(_bubble(u) for u in updates)
    ct = html.escape((channel_title or "").strip()) or "הכותרות של היום"
    return (
        f'<div class="modal" id="{modal_id}" hidden>'
        f'<div class="modal-bg" data-close></div>'
        f'<div class="modal-card" role="dialog" aria-modal="true">'
        f'<div class="modal-head">'
        f'<div class="avatar">{html.escape(initial)}</div>'
        f'<div class="modal-title">{ct}</div>'
        f'<button type="button" class="modal-x" data-close aria-label="סגור">&times;</button>'
        f'</div>'
        f'<div class="thread">{bubbles}</div>'
        f'</div></div>'
    )


def _toc_feed_html(posts: list[dict], today, channel_title: str) -> tuple[str, str, int]:
    """Build ``(rows_html, modals_html, item_count)`` for the today's-headlines feed.

    Digest updates (posted as Telegram replies to their original story) are folded
    into the original's row with a "הצג עדכונים" link that opens a thread popup.
    """
    initial = ((channel_title or "").strip()[:1]) or "•"
    by_id = {p.get("post_id"): p for p in posts if p.get("post_id")}

    updates_of: dict[str, list[dict]] = {}
    consumed: set[str] = set()
    for p in posts:
        rid = p.get("reply_to_msg_id")
        if rid and rid in by_id and _toc_header(p.get("text_plain") or "")[1]:
            updates_of.setdefault(rid, []).append(p)
            consumed.add(p.get("post_id"))
    _floor = datetime.min.replace(tzinfo=_TOC_TZ)
    for lst in updates_of.values():  # get_messages is newest-first; thread wants oldest-first
        lst.sort(key=lambda u: (_toc_local_dt(u) or _floor))

    items: list[dict] = []
    modals: list[str] = []
    for p in posts:
        pid = p.get("post_id")
        if pid in consumed:
            continue
        txt = (p.get("text_plain") or "").strip()
        if not txt or txt.startswith("Run log — "):
            continue
        header, is_update = _toc_header(txt)
        if not header:
            continue
        local = _toc_local_dt(p)
        if local is None:
            continue

        ups = updates_of.get(pid)
        if ups:
            up_locals = [d for d in (_toc_local_dt(u) for u in ups) if d]
            if not up_locals:
                continue
            latest = max(up_locals)
            if local.date() != today and latest.date() != today:
                continue
            mid = f"u{len(modals) + 1}"
            modals.append(_toc_render_thread(mid, channel_title, initial, p, ups))
            disp = latest if latest.date() == today else local
            items.append({
                "kind": "group", "header": header, "url": p.get("post_url") or "",
                "time": disp.strftime("%H:%M"), "count": len(ups), "modal_id": mid,
                "sort": max([local, *up_locals]),
            })
        else:
            if local.date() != today:
                continue
            items.append({
                "kind": "row", "header": header, "url": p.get("post_url") or "",
                "time": local.strftime("%H:%M"), "is_update": is_update, "sort": local,
            })

    items.sort(key=lambda x: x["sort"], reverse=True)

    rows_html = ""
    for it in items:
        if it["kind"] == "group":
            n = it["count"]
            btn = "הצג עדכון" if n == 1 else f"הצג עדכונים ({n})"
            head = (
                f'<a class="h" href="{html.escape(it["url"], quote=True)}">'
                f'{html.escape(it["header"])}</a>'
                if it["url"] else f'<span class="h">{html.escape(it["header"])}</span>'
            )
            rows_html += (
                f'<div class="row">{head}<span class="m">'
                f'<button type="button" class="upd" data-modal="{it["modal_id"]}">{btn}</button>'
                f'{it["time"]}</span></div>\n'
            )
        else:
            tag = '<span class="tag">עדכון</span>' if it["is_update"] else ""
            inner = (
                f'<span class="h">{html.escape(it["header"])}</span>'
                f'<span class="m">{tag}{it["time"]}</span>'
            )
            if it["url"]:
                rows_html += (
                    f'<a class="row" href="{html.escape(it["url"], quote=True)}">{inner}</a>\n'
                )
            else:
                rows_html += f'<div class="row">{inner}</div>\n'

    return rows_html, "".join(modals), len(items)


def _render_toc_page(channel_title: str, rows_html: str, message: str = "",
                     modals_html: str = "") -> str:
    now = datetime.now(_TOC_TZ).strftime("%d/%m/%Y")
    name = (channel_title or "").strip()
    title = html.escape(name) if name else "הכותרות של היום"
    initial = html.escape(name[:1]) if name else "•"
    body_list = f'<div class="feed">{rows_html}</div>' if rows_html else ""
    note = f'<p class="note">{html.escape(message)}</p>' if message else ""
    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" id="mtc" content="#FBFAF4">
<title>הכותרות של היום</title>
<script>
(function () {{
  try {{
    if (localStorage.getItem('digest_toc_theme') === 'dark')
      document.documentElement.setAttribute('data-theme', 'dark');
  }} catch (e) {{}}
}})();
function toggleTheme() {{
  var el = document.documentElement;
  var dark = el.getAttribute('data-theme') !== 'dark';
  if (dark) el.setAttribute('data-theme', 'dark');
  else el.removeAttribute('data-theme');
  try {{ localStorage.setItem('digest_toc_theme', dark ? 'dark' : 'light'); }} catch (e) {{}}
  var m = document.getElementById('mtc');
  if (m) m.content = dark ? '#0e1117' : '#FBFAF4';
}}
function closeModal(m) {{ if (m) {{ m.hidden = true; document.body.style.overflow = ''; }} }}
document.addEventListener('click', function (e) {{
  var opener = e.target.closest('[data-modal]');
  if (opener) {{
    e.preventDefault();
    var m = document.getElementById(opener.getAttribute('data-modal'));
    if (m) {{ m.hidden = false; document.body.style.overflow = 'hidden'; }}
    return;
  }}
  if (e.target.closest('[data-close]')) closeModal(e.target.closest('.modal'));
}});
document.addEventListener('keydown', function (e) {{
  if (e.key === 'Escape') closeModal(document.querySelector('.modal:not([hidden])'));
}});
</script>
<style>
  :root {{
    color-scheme: light;
    --bg: #FBFAF4; --card-bg: #F1EFE8; --text: #091717; --text-secondary: #5C6B6B;
    --accent: #20808D; --border: #E0DED6;
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --bg: #0e1117; --card-bg: #1a1d27; --text: #e8eaed; --text-secondary: #9aa0a6;
    --accent: #2196f3; --border: #2d3240;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, sans-serif;
         line-height: 1.6; }}
  .top {{ display: flex; align-items: center; gap: 12px;
          max-width: 800px; margin: 0 auto; padding: 12px 16px;
          border-bottom: 1px solid var(--border); }}
  .avatar {{ width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0;
             background: var(--accent); color: #fff; font-weight: 700; font-size: 1.2rem;
             display: flex; align-items: center; justify-content: center; }}
  .top .id {{ min-width: 0; }}
  h1 {{ font-size: 1.15rem; margin: 0;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .date {{ color: var(--text-secondary); font-size: .82rem; }}
  .theme-toggle {{ margin-inline-start: auto; flex-shrink: 0;
                   width: 38px; height: 38px; padding: 0;
                   display: flex; align-items: center; justify-content: center;
                   background: var(--card-bg); border: 1px solid var(--border);
                   border-radius: 10px; color: var(--text-secondary); cursor: pointer; }}
  .theme-toggle:hover {{ border-color: var(--accent); color: var(--accent); }}
  .theme-toggle svg {{ width: 18px; height: 18px; fill: currentColor; }}
  .theme-toggle .sun {{ display: none; }}
  :root[data-theme="dark"] .theme-toggle .moon {{ display: none; }}
  :root[data-theme="dark"] .theme-toggle .sun {{ display: block; }}
  .feed {{ max-width: 720px; margin: 0 auto; padding: 8px 18px 56px; }}
  .row {{ display: flex; gap: 16px; align-items: baseline;
          justify-content: space-between; padding: 16px 0;
          border-bottom: 1px solid var(--border);
          border-inline-start: 2px solid transparent; text-decoration: none;
          transition: border-color .15s ease, padding .15s ease; }}
  .feed > :last-child {{ border-bottom-color: transparent; }}
  .row:hover {{ border-inline-start-color: var(--accent); padding-inline-start: 12px; }}
  .h {{ color: var(--text); font-size: 1.05rem; line-height: 1.55;
        transition: color .15s ease; }}
  .row:hover .h {{ color: var(--accent); }}
  .m {{ flex-shrink: 0; font-size: .74rem; color: var(--text-secondary);
        white-space: nowrap; letter-spacing: .03em; font-variant-numeric: tabular-nums; }}
  .tag {{ color: var(--accent); font-weight: 600;
          letter-spacing: 0; margin-inline-end: 6px; }}
  .upd {{ font: inherit; font-size: .74rem; font-weight: 600; color: var(--accent);
          background: none; border: 0; padding: 0; cursor: pointer;
          text-decoration: underline; margin-inline-end: 8px; }}
  .note {{ text-align: center; color: var(--text-secondary); padding: 40px 16px; }}
  .modal[hidden] {{ display: none; }}
  .modal {{ position: fixed; inset: 0; z-index: 50; display: flex;
            align-items: flex-end; justify-content: center; }}
  .modal-bg {{ position: absolute; inset: 0; background: rgba(0, 0, 0, .55); }}
  .modal-card {{ position: relative; width: 100%; max-width: 640px; max-height: 85vh;
                 display: flex; flex-direction: column; background: var(--bg);
                 border: 1px solid var(--border); border-radius: 16px 16px 0 0;
                 overflow: hidden; }}
  .modal-head {{ display: flex; align-items: center; gap: 10px; flex-shrink: 0;
                 padding: 12px 14px; border-bottom: 1px solid var(--border); }}
  .modal-head .avatar {{ width: 32px; height: 32px; font-size: .95rem; }}
  .modal-title {{ flex: 1; min-width: 0; font-weight: 600; font-size: .98rem;
                  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .modal-x {{ background: none; border: 0; color: var(--text-secondary);
              font-size: 1.6rem; line-height: 1; cursor: pointer; padding: 0 4px; }}
  .thread {{ padding: 14px; overflow-y: auto; display: flex;
             flex-direction: column; gap: 8px; }}
  .bubble {{ display: block; align-self: flex-start; max-width: 92%;
             background: var(--card-bg); border: 1px solid var(--border);
             border-radius: 14px; border-start-start-radius: 4px;
             padding: 8px 12px 6px; color: var(--text); text-decoration: none; }}
  .bubble:hover {{ border-color: var(--accent); }}
  .bubble-label {{ color: var(--accent); font-weight: 700; font-size: .72rem;
                   margin-bottom: 3px; }}
  .bubble-body {{ font-size: .95rem; line-height: 1.5; }}
  .bubble-body b {{ font-weight: 700; }}
  .bubble-time {{ margin-top: 4px; font-size: .68rem; color: var(--text-secondary); }}
  @media (min-width: 560px) {{
    .modal {{ align-items: center; }}
    .modal-card {{ border-radius: 16px; }}
  }}
</style>
</head>
<body>
<div class="top">
  <div class="avatar">{initial}</div>
  <div class="id">
    <h1>{title}</h1>
    <div class="date">{now}</div>
  </div>
  <button class="theme-toggle" onclick="toggleTheme()" aria-label="החלף ערכת נושא">
    <svg class="moon" viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z"/></svg>
    <svg class="sun" viewBox="0 0 24 24"><path d="M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zM2 13h2c.55 0 1-.45 1-1s-.45-1-1-1H2c-.55 0-1 .45-1 1s.45 1 1 1zm18 0h2c.55 0 1-.45 1-1s-.45-1-1-1h-2c-.55 0-1 .45-1 1s.45 1 1 1zM11 2v2c0 .55.45 1 1 1s1-.45 1-1V2c0-.55-.45-1-1-1s-1 .45-1 1zm0 18v2c0 .55.45 1 1 1s1-.45 1-1v-2c0-.55-.45-1-1-1s-1 .45-1 1zM5.99 4.58a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0s.39-1.03 0-1.41L5.99 4.58zm12.37 12.37a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0a.996.996 0 0 0 0-1.41l-1.06-1.06zm1.06-10.96a.996.996 0 0 0 0-1.41.996.996 0 0 0-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06zM7.05 18.36a.996.996 0 0 0 0-1.41.996.996 0 0 0-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06z"/></svg>
  </button>
</div>
{body_list}
{note}
{modals_html}
</body>
</html>"""


@app.get("/digest/today")
async def digest_today(request: Request):
    """Secret-token-gated page listing today's headlines for a digest channel."""
    key = os.environ.get("DIGEST_TOC_KEY", "")
    supplied = request.query_params.get("k", "")
    if not key or not supplied or not secrets.compare_digest(supplied, key):
        raise HTTPException(status_code=404, detail="Not Found")

    ch = "test" if request.query_params.get("ch") == "test" else "prod"
    resp_headers = {
        "X-Robots-Tag": "noindex, nofollow",
        "Cache-Control": "private, no-store",
    }

    cached = _toc_cache.get(ch)
    if cached and (time.time() - cached[0]) < _TOC_CACHE_TTL:
        return HTMLResponse(cached[1], headers=resp_headers)

    unavailable = _render_toc_page("", "", message="הרשימה אינה זמינה כרגע.")
    channel_ref = _toc_channel_ref(ch)
    tg: TelegramClient | None = request.app.state.telegram
    if channel_ref is None or tg is None:
        return HTMLResponse(unavailable, headers=resp_headers)

    config = load_config()
    fetch_limit = max(config.get("fetch_per_channel", 50), 100)
    media_sem = asyncio.Semaphore(config.get("media_concurrency", 5))
    try:
        posts = await fetch_channel_posts_telethon(
            tg, channel_ref, limit=fetch_limit, media_sem=media_sem
        )
    except Exception as e:
        logger.warning("GET /digest/today: channel fetch failed: %s", e)
        return HTMLResponse(unavailable, headers=resp_headers)

    today = datetime.now(_TOC_TZ).date()
    channel_title = posts[0].get("channel_title") if posts else ""
    rows_html, modals_html, count = _toc_feed_html(posts, today, channel_title or "")

    page = _render_toc_page(
        channel_title or "", rows_html,
        message="" if count else "לא פורסמו כותרות היום.",
        modals_html=modals_html,
    )
    _toc_cache[ch] = (time.time(), page)
    logger.info("GET /digest/today ch=%s -> %d items", ch, count)
    return HTMLResponse(page, headers=resp_headers)


async def _digest_write_clients(request: Request) -> list[TelegramClient]:
    """Prefer bot, then user client, for edit/delete on the digest channel."""
    bot: TelegramClient | None = request.app.state.bot
    tg: TelegramClient | None = request.app.state.telegram
    clients = [c for c in (bot, tg) if c is not None]
    if not clients:
        raise HTTPException(status_code=503, detail="Telegram client not available")
    return clients


@app.get("/api/channel-posts")
async def get_channel_posts(request: Request, user: dict = Depends(require_auth)):
    """Return posts from DIGEST_TELEGRAM_CHANNEL (admin only)."""
    if user.get("user_id") != 1:
        raise HTTPException(status_code=403, detail="Admin access required")

    channel_ref = _digest_channel_ref()
    tg: TelegramClient | None = request.app.state.telegram
    if not tg:
        raise HTTPException(status_code=503, detail="Telegram client not available")

    config = load_config()
    max_posts = config.get("alternate_feed_max_posts", 100)
    fetch_limit = config.get("fetch_per_channel", 50)
    media_sem = asyncio.Semaphore(config.get("media_concurrency", 5))
    posts = await fetch_channel_posts_telethon(
        tg, channel_ref, limit=fetch_limit, media_sem=media_sem
    )
    posts.sort(key=lambda p: p["datetime"], reverse=True)
    logger.info(
        "Admin '%s' requested digest channel posts (%d posts)",
        user["user_name"], len(posts[:max_posts]),
    )
    return posts[:max_posts]


@app.put("/api/admin/channel-posts/{post_id}")
async def edit_channel_post(post_id: int, request: Request, user: dict = Depends(require_auth)):
    """Edit text/caption of a message in DIGEST_TELEGRAM_CHANNEL (admin only)."""
    if user.get("user_id") != 1:
        raise HTTPException(status_code=403, detail="Admin access required")

    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    channel_ref = _digest_channel_ref()
    clients = await _digest_write_clients(request)
    last_err: Exception | None = None
    for client in clients:
        try:
            entity = await client.get_entity(channel_ref)
            await client.edit_message(entity, post_id, text)
            logger.info(
                "User '%s' edited digest channel post %s",
                user["user_name"], post_id,
            )
            return {"status": "success"}
        except Exception as e:
            last_err = e
            logger.warning("Edit digest post %s failed with a client: %s", post_id, e)

    logger.error("Failed to edit digest channel post %s: %s", post_id, last_err)
    raise HTTPException(status_code=500, detail=f"Failed to edit: {last_err}")


@app.delete("/api/admin/channel-posts/{post_id}")
async def delete_channel_post(post_id: int, request: Request, user: dict = Depends(require_auth)):
    """Delete a message from DIGEST_TELEGRAM_CHANNEL (admin only)."""
    if user.get("user_id") != 1:
        raise HTTPException(status_code=403, detail="Admin access required")

    channel_ref = _digest_channel_ref()
    clients = await _digest_write_clients(request)
    last_err: Exception | None = None
    for client in clients:
        try:
            entity = await client.get_entity(channel_ref)
            await client.delete_messages(entity, [post_id])
            logger.info(
                "User '%s' deleted digest channel post %s",
                user["user_name"], post_id,
            )
            return {"status": "success"}
        except Exception as e:
            last_err = e
            logger.warning("Delete digest post %s failed with a client: %s", post_id, e)

    logger.error("Failed to delete digest channel post %s: %s", post_id, last_err)
    raise HTTPException(status_code=500, detail=f"Failed to delete: {last_err}")


@app.post("/api/top-posts")
async def get_top_posts(request: Request, user: dict = Depends(require_auth)):
    body = await request.json()
    posts = body.get("posts", [])
    logger.info("User '%s' requested Top 10 analysis (%d posts)", user["user_name"], len(posts))
    
    config = load_config()
    ai_provider = config.get("ai_provider", "mistral")
    ai_model = config.get("ai_model", "mistral-large-latest")

    if ai_provider == "gemini" and not GOOGLE_API_KEY:
        return JSONResponse(status_code=503, content={"detail": "AI ranking not configured (GOOGLE_API_KEY missing)"})
    if ai_provider == "groq" and not GROQ_API_KEY:
        return JSONResponse(status_code=503, content={"detail": "AI ranking not configured (GROQ_API_KEY missing)"})
    if ai_provider == "mistral" and not MISTRAL_API_KEY:
        return JSONResponse(status_code=503, content={"detail": "AI ranking not configured (MISTRAL_API_KEY missing)"})

    if len(posts) < 10:
        return JSONResponse(status_code=400, content={"detail": "Need at least 10 posts to rank"})

    try:
        prompt_text = TOP10_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return JSONResponse(status_code=500, content={"detail": "top10_prompt.md not found"})

    posts = posts[:100]

    slim_posts = []
    for i, p in enumerate(posts):
        views_raw = re.sub(r"[^\d]", "", p.get("views", "0")) or "0"
        views_num = int(views_raw)
        subs = p.get("channel_subscribers") or 0
        engagement = round(views_num / subs * 100, 1) if subs > 0 else 0

        entry = {
            "i": i,
            "ch": p.get("channel", ""),
            "t": (p.get("text_plain") or "")[:200],
            "dt": p.get("datetime", ""),
            "v": p.get("views", ""),
            "e": engagement,
            "m": bool(p.get("photo_url") or p.get("video_thumb")),
        }
        lp = p.get("link_preview")
        if lp and lp.get("title"):
            entry["lp"] = lp["title"][:80]
        slim_posts.append(entry)

    user_content = json.dumps(slim_posts, ensure_ascii=False)

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            if ai_provider == "gemini":
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{ai_model}:generateContent?key={GOOGLE_API_KEY}",
                    headers={"Content-Type": "application/json"},
                    json={
                        "contents": [
                            {"role": "user", "parts": [{"text": prompt_text + "\n\n" + user_content}]},
                        ],
                        "generationConfig": {
                            "temperature": 0.2,
                            "responseMimeType": "application/json",
                        },
                    },
                )
            elif ai_provider == "mistral":
                resp = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {MISTRAL_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": ai_model,
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": prompt_text},
                            {"role": "user", "content": user_content},
                        ],
                    },
                )
            else:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": ai_model,
                        "temperature": 0.2,
                        "messages": [
                            {"role": "system", "content": prompt_text},
                            {"role": "user", "content": user_content},
                        ],
                    },
                )
            if resp.status_code != 200:
                error_body = resp.text[:1000]
                logger.error("AI API error (%s/%s) HTTP %s: %s", ai_provider, ai_model, resp.status_code, error_body)
                detail = f"AI service returned {resp.status_code}"
                try:
                    err_json = resp.json()
                    detail = err_json.get("error", {}).get("message", detail)
                except Exception:
                    pass
                if resp.status_code == 429 or "quota" in detail.lower() or "rate limit" in detail.lower() or "resource has been exhausted" in detail.lower():
                    detail = "Quota exceeded — please try again later"
                return JSONResponse(status_code=502, content={"detail": detail})
        except Exception as e:
            logger.error("AI API request failed (%s/%s): %s", ai_provider, ai_model, e)
            return JSONResponse(status_code=502, content={"detail": "AI service request failed"})

    try:
        data = resp.json()
        if ai_provider == "gemini":
            content = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        else:  # groq and mistral use OpenAI-compatible format
            content = data["choices"][0]["message"]["content"].strip()
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        indices = json.loads(content)
        if not isinstance(indices, list):
            raise ValueError("Expected a JSON array")
    except Exception as e:
        logger.error("Failed to parse AI response (%s): %s — raw: %s", ai_provider, e, content[:500] if 'content' in dir() else "N/A")
        return JSONResponse(status_code=502, content={"detail": "Failed to parse AI response"})

    top_posts = []
    for idx in indices:
        if isinstance(idx, int) and 0 <= idx < len(posts):
            top_posts.append(posts[idx])
    return top_posts[:10]


async def _context_summary_gemini(body: dict) -> dict | JSONResponse:
    """Generate context summary using Gemini with Google Search grounding."""
    if not GOOGLE_API_KEY:
        return JSONResponse(
            status_code=503,
            content={"detail": "Context summary not configured (GOOGLE_API_KEY missing)"}
        )
    
    post_text = body.get("post_text", "").strip()
    
    if not post_text:
        return JSONResponse(
            status_code=400,
            content={"detail": "post_text is required"}
        )
    
    try:
        prompt_text = CONTEXT_SUMMARY_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return JSONResponse(
            status_code=500,
            content={"detail": "context_summary_prompt.md not found"}
        )
    
    try:
        client = genai.Client(api_key=GOOGLE_API_KEY)
        
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash",
            contents=post_text,
            config=types.GenerateContentConfig(
                system_instruction=prompt_text,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.3,
            )
        )
        
        summary = response.text
        
        sources = []
        if response.candidates and len(response.candidates) > 0:
            candidate = response.candidates[0]
            if hasattr(candidate, 'grounding_metadata') and candidate.grounding_metadata:
                if hasattr(candidate.grounding_metadata, 'grounding_chunks'):
                    for chunk in candidate.grounding_metadata.grounding_chunks:
                        if hasattr(chunk, 'web') and chunk.web:
                            sources.append({
                                "title": getattr(chunk.web, 'title', ''),
                                "url": getattr(chunk.web, 'uri', ''),
                            })
        
        return {"summary": summary, "sources": sources}
        
    except Exception as e:
        logger.error("Gemini context summary generation failed: %s", e)
        return JSONResponse(
            status_code=502,
            content={"detail": f"Failed to generate context summary: {str(e)}"}
        )


async def _context_summary_mistral(body: dict) -> dict | JSONResponse:
    """Generate context summary using Mistral AI + Tavily web search."""
    if not MISTRAL_API_KEY:
        return JSONResponse(
            status_code=503,
            content={"detail": "Context summary not configured (MISTRAL_API_KEY missing)"}
        )
    if not TAVILY_API_KEY:
        return JSONResponse(
            status_code=503,
            content={"detail": "Context summary not configured (TAVILY_API_KEY missing)"}
        )
    
    post_text = body.get("post_text", "").strip()
    
    if not post_text:
        return JSONResponse(
            status_code=400,
            content={"detail": "post_text is required"}
        )
    
    try:
        config = load_config()
        mistral_model = config.get("context_mistral_model", "mistral-large-latest")
        tavily_depth = config.get("context_tavily_depth", "basic")
        tavily_max_results = config.get("context_tavily_max_results", 5)
        
        web_search = WebSearch(
            mistral_api_key=MISTRAL_API_KEY,
            tavily_api_key=TAVILY_API_KEY,
            mistral_model=mistral_model,
            search_depth=tavily_depth,
            max_results=tavily_max_results,
        )
        
        result = await web_search.search(post_text)
        
        return {
            "summary": result["summary"],
            "sources": result["sources"]
        }
        
    except Exception as e:
        logger.error("Mistral+Tavily context summary generation failed: %s", e)
        return JSONResponse(
            status_code=502,
            content={"detail": f"Failed to generate context summary: {str(e)}"}
        )


@app.post("/api/context-summary")
async def get_context_summary(request: Request, user: dict = Depends(require_auth)):
    """Generate context summary for a post using configured provider (Gemini or Mistral+Tavily)."""
    body = await request.json()
    post_text = body.get("post_text", "")
    logger.info("User '%s' requested context summary (text_length=%d)", user["user_name"], len(post_text))
    
    config = load_config()
    context_provider = config.get("context_provider", "gemini")
    
    if context_provider == "mistral":
        return await _context_summary_mistral(body)
    else:
        return await _context_summary_gemini(body)


@app.get("/api/config", dependencies=[Depends(require_auth)])
async def get_config():
    config = load_config()
    return {
        "refresh_interval_minutes": config.get("refresh_interval_minutes", 5),
        "scroll_speed": config.get("scroll_speed", 50),
    }


@app.get("/api/feeds")
async def get_feeds(request: Request, user: dict = Depends(require_auth)):
    db: Database = request.app.state.db
    feeds = await db.get_feeds_for_user(user["user_id"])
    return [
        {
            "feed_url": f["feed_url"],
            "is_private": f.get("is_private", False),
            "admin_only": f.get("admin_only", False),
            "is_alternate": f.get("is_alternate", False),
        }
        for f in feeds
    ]


@app.post("/api/feeds")
async def add_feed(request: Request, user: dict = Depends(require_auth)):
    body = await request.json()
    feed_url = body.get("feed_url", "").strip()
    if not feed_url:
        return JSONResponse(status_code=400, content={"detail": "feed_url is required"})

    is_private = body.get("is_private", False)
    admin_only = body.get("admin_only", False)
    is_alternate = body.get("is_alternate", False)

    db: Database = request.app.state.db

    if user.get("user_id") != 1:
        if admin_only:
            raise HTTPException(status_code=403, detail="Only admin can create admin-only feeds")
        if is_alternate:
            raise HTTPException(status_code=403, detail="Only admin can create alternate feed channels")
        if await db.is_feed_admin_only(feed_url):
            raise HTTPException(status_code=403, detail="This channel is restricted to admin")

    result = await db.add_feed(user["user_id"], feed_url, is_private=is_private, admin_only=admin_only, is_alternate=is_alternate)
    if result is None:
        return JSONResponse(status_code=409, content={"detail": "Feed already exists"})
    logger.info("User '%s' added channel '%s' (private=%s, admin_only=%s, alternate=%s)", user["user_name"], feed_url, is_private, admin_only, is_alternate)
    return {"status": "success", "feed_url": feed_url}


@app.delete("/api/feeds")
async def delete_feed(request: Request, user: dict = Depends(require_auth)):
    body = await request.json()
    feed_url = body.get("feed_url", "").strip()
    if not feed_url:
        return JSONResponse(status_code=400, content={"detail": "feed_url is required"})
    is_alternate = body.get("is_alternate", False)
    db: Database = request.app.state.db
    removed = await db.remove_feed(user["user_id"], feed_url, is_alternate=is_alternate)
    if not removed:
        return JSONResponse(status_code=404, content={"detail": "Feed not found"})
    logger.info("User '%s' removed channel '%s' (alternate=%s)", user["user_name"], feed_url, is_alternate)
    return {"status": "success"}


@app.get("/api/search-channels")
async def search_channels(request: Request, q: str = "", user: dict = Depends(require_auth)):
    tg: TelegramClient | None = request.app.state.telegram
    if tg is None:
        return JSONResponse(status_code=503, content={"detail": "Channel search not available"})
    if len(q.strip()) < 2:
        return []
    try:
        result = await tg(functions.contacts.SearchRequest(q=q.strip(), limit=8))
        channels = []
        for chat in result.chats:
            username = getattr(chat, "username", None)
            if not username:
                continue
            channels.append({
                "username": username,
                "title": getattr(chat, "title", username),
                "participants_count": getattr(chat, "participants_count", None),
            })
        return channels
    except Exception as e:
        logger.error("Telegram search failed: %s", e)
        return JSONResponse(status_code=500, content={"detail": "Search failed"})


@app.get("/api/admin/channels")
async def get_admin_channels(request: Request, user: dict = Depends(require_auth)):
    """List channels the Telethon session is a member of (admin only)."""
    if user.get("user_id") != 1:
        raise HTTPException(status_code=403, detail="Admin access required")
    tg: TelegramClient | None = request.app.state.telegram
    if tg is None:
        return JSONResponse(status_code=503, content={"detail": "Telegram client not available"})
    try:
        dialogs = await tg.get_dialogs()
        channels = []
        for dialog in dialogs:
            entity = dialog.entity
            if not isinstance(entity, (Channel,)):
                continue
            channels.append({
                "id": entity.id,
                "title": getattr(entity, "title", ""),
                "participants_count": getattr(entity, "participants_count", None),
                "username": getattr(entity, "username", None),
            })
        return channels
    except Exception as e:
        logger.error("Failed to list channels: %s", e)
        return JSONResponse(status_code=500, content={"detail": "Failed to list channels"})


@app.get("/api/saved")
async def get_saved_posts(request: Request, user: dict = Depends(require_auth)):
    db: Database = request.app.state.db
    posts = await db.get_saved_posts(user["user_id"])
    logger.info("User '%s' viewed saved posts (%d posts)", user["user_name"], len(posts))
    return posts


@app.post("/api/saved")
async def save_post(request: Request, user: dict = Depends(require_auth)):
    post = await request.json()
    db: Database = request.app.state.db
    result = await db.save_post(user["user_id"], post)
    if result is None:
        return {"status": "exists", "message": "Post already saved"}
    logger.info("User '%s' saved post from channel '%s' (post_id=%s)", user["user_name"], post.get("channel"), post.get("post_id"))
    return {"status": "success"}


@app.delete("/api/saved/{channel}/{post_id}")
async def unsave_post(channel: str, post_id: str, request: Request, user: dict = Depends(require_auth)):
    db: Database = request.app.state.db
    removed = await db.unsave_post(user["user_id"], channel, post_id)
    if not removed:
        return JSONResponse(status_code=404, content={"detail": "Saved post not found"})
    logger.info("User '%s' unsaved post from channel '%s' (post_id=%s)", user["user_name"], channel, post_id)
    return {"status": "success"}


@app.post("/api/track/share")
async def track_share(request: Request, user: dict = Depends(require_auth)):
    """Track share actions for analytics."""
    body = await request.json()
    shared_to = body.get("shared_to", "unknown")
    post_url = body.get("post_url", "")
    logger.info("User '%s' shared post via '%s' (url=%s)", user["user_name"], shared_to, post_url)
    return {"status": "success"}


@app.post("/api/admin/share-to-channel")
async def share_to_channel(request: Request, user: dict = Depends(require_auth)):
    if user["user_id"] != 1:
        raise HTTPException(status_code=403, detail="Admin access required")
    channel = os.environ.get("DIGEST_TELEGRAM_CHANNEL")
    if not channel:
        raise HTTPException(status_code=503, detail="DIGEST_TELEGRAM_CHANNEL not configured")
    tg: TelegramClient | None = request.app.state.telegram
    if not tg:
        raise HTTPException(status_code=503, detail="Telegram client not available")
    body = await request.json()
    post_channel = body.get("post_channel", "")
    post_id = body.get("post_id", "")
    if not post_channel or not post_id:
        raise HTTPException(status_code=400, detail="post_channel and post_id are required")
    try:
        target = await tg.get_entity(channel)
        source = int(post_channel) if str(post_channel).lstrip("-").isdigit() else post_channel
        await tg.forward_messages(target, messages=[int(post_id)], from_peer=source)
        logger.info(
            "User '%s' forwarded post to Telegram channel '%s' (from=%s/%s)",
            user["user_name"], channel, post_channel, post_id,
        )
        return {"status": "success"}
    except Exception as e:
        logger.error("Failed to forward to Telegram channel: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to forward: {e}")


@app.post("/api/admin/compose-to-channel")
async def compose_to_channel(request: Request, user: dict = Depends(require_auth)):
    if user["user_id"] != 1:
        raise HTTPException(status_code=403, detail="Admin access required")
    body = await request.json()
    text = body.get("text", "").strip()
    target = body.get("target", "")
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if target == "test":
        channel = os.environ.get("TEST_DIGEST_TELEGRAM_CHANNEL")
        if not channel:
            raise HTTPException(status_code=503, detail="TEST_DIGEST_TELEGRAM_CHANNEL not configured")
    elif target == "production":
        channel = os.environ.get("DIGEST_TELEGRAM_CHANNEL")
        if not channel:
            raise HTTPException(status_code=503, detail="DIGEST_TELEGRAM_CHANNEL not configured")
    else:
        raise HTTPException(status_code=400, detail="target must be 'test' or 'production'")
    bot: TelegramClient | None = request.app.state.bot
    tg: TelegramClient | None = request.app.state.telegram
    sender = bot or tg
    if not sender:
        raise HTTPException(status_code=503, detail="Telegram client not available")
    buttons = _toc_button_rows(bot, target)
    try:
        entity = await sender.get_entity(int(channel) if channel.lstrip("-").isdigit() else channel)
        await sender.send_message(entity, text, link_preview=True, buttons=buttons)
        logger.info(
            "User '%s' composed post to %s Telegram channel '%s'",
            user["user_name"], target, channel,
        )
        return {"status": "success"}
    except Exception as e:
        logger.error("Failed to compose post to Telegram channel: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to send: {e}")


@app.post("/api/admin/upload-image-to-channel")
async def upload_image_to_channel(
    request: Request,
    image: UploadFile = File(...),
    caption: str = Form(""),
    target: str = Form("test"),
    user: dict = Depends(require_auth),
):
    if user["user_id"] != 1:
        raise HTTPException(status_code=403, detail="Admin access required")
    if target == "test":
        channel = os.environ.get("TEST_DIGEST_TELEGRAM_CHANNEL")
        if not channel:
            raise HTTPException(status_code=503, detail="TEST_DIGEST_TELEGRAM_CHANNEL not configured")
    elif target == "production":
        channel = os.environ.get("DIGEST_TELEGRAM_CHANNEL")
        if not channel:
            raise HTTPException(status_code=503, detail="DIGEST_TELEGRAM_CHANNEL not configured")
    else:
        raise HTTPException(status_code=400, detail="target must be 'test' or 'production'")
    bot: TelegramClient | None = request.app.state.bot
    tg: TelegramClient | None = request.app.state.telegram
    sender = bot or tg
    if not sender:
        raise HTTPException(status_code=503, detail="Telegram client not available")
    data = await image.read()
    if not data:
        raise HTTPException(status_code=400, detail="image is empty")
    buf = io.BytesIO(data)
    buf.name = image.filename or "upload.jpg"
    has_caption = bool(caption.strip())
    buttons = _toc_button_rows(bot, target, include_px=has_caption)
    try:
        entity = await sender.get_entity(int(channel) if channel.lstrip("-").isdigit() else channel)
        await sender.send_file(
            entity,
            file=buf,
            caption=caption if has_caption else None,
            buttons=buttons,
        )
        logger.info(
            "User '%s' uploaded image (%d bytes) to %s Telegram channel '%s'",
            user["user_name"], len(data), target, channel,
        )
        return {"status": "success", "size": len(data), "target": target}
    except Exception as e:
        logger.error("Failed to upload image to Telegram channel: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to send: {e}")


@app.get("/api/admin/config")
async def get_full_config(user: dict = Depends(require_auth)):
    if user.get("user_id") != 1:
        raise HTTPException(status_code=403, detail="Admin access required")
    return load_config()


@app.post("/api/admin/config")
async def update_config(request: Request, user: dict = Depends(require_auth)):
    if user.get("user_id") != 1:
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        config_data = await request.json()
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config_data, f, indent=2)
        return {"status": "success", "message": "Configuration updated"}
    except Exception as e:
        logger.error("Failed to update config: %s", e)
        return {"status": "error", "message": str(e)}


@app.get("/static/manifest.json")
async def get_manifest():
    manifest_path = Path(__file__).parent / "static" / "manifest.json"
    return FileResponse(
        manifest_path,
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"}
    )


@app.get("/static/sw.js")
async def get_service_worker():
    sw_path = Path(__file__).parent / "static" / "sw.js"
    return FileResponse(
        sw_path,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/"
        }
    )


@app.get("/health")
@app.head("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/")
@app.head("/")
async def root():
    return FileResponse(
        Path(__file__).parent / "static" / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
