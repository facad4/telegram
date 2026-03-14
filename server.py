import asyncio
import base64
import hashlib
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

import httpx
import jwt
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, MessageMediaPhoto, MessageMediaDocument, PeerChannel

from database import Database

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

JWT_SECRET = secrets.token_hex(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 30


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

    yield

    if app.state.telegram:
        await app.state.telegram.disconnect()
        logger.info("Telegram client disconnected")


app = FastAPI(lifespan=lifespan)

CONFIG_PATH = Path(__file__).parent / "config.json"
TOP10_PROMPT_PATH = Path(__file__).parent / "top10_prompt.md"
GROQ_API_KEY = os.environ.get("GROK_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
AVATAR_CACHE_DIR = Path(__file__).parent / "avatar_cache"
AVATAR_CACHE_DIR.mkdir(exist_ok=True)
THUMB_CACHE_DIR = Path(__file__).parent / "thumb_cache"
THUMB_CACHE_DIR.mkdir(exist_ok=True)
AVATAR_TTL_SECONDS = 1_209_600  # 2 weeks
THUMB_TTL_SECONDS = 1_209_600   # 2 weeks


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
    """
    if media_sem is None:
        media_sem = asyncio.Semaphore(5)

    try:
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

        download_tasks = [
            _download_thumb(tg, msg.media, media_sem, f"{channel_name}_{msg.id}")
            if isinstance(msg.media, MessageMediaPhoto) else asyncio.sleep(0, result=None)
            for msg in filtered
        ]
        photo_results = await asyncio.gather(*download_tasks)

        raw_posts = []
        for msg, photo_url in zip(filtered, photo_results):
            text_plain = msg.text or ""
            text_html = f"<div>{text_plain.replace(chr(10), '<br>')}</div>" if text_plain else ""
            views = str(msg.views) if msg.views else ""
            datetime_str = msg.date.isoformat() if msg.date else ""

            if username:
                post_url = f"https://t.me/{username}/{msg.id}"
            else:
                post_url = f"https://t.me/c/{real_id}/{msg.id}"

            raw_posts.append({
                "channel": channel_name,
                "channel_title": channel_title,
                "channel_photo": channel_photo,
                "post_id": str(msg.id),
                "post_url": post_url,
                "text_html": text_html,
                "text_plain": text_plain,
                "views": views,
                "datetime": datetime_str,
                "photo_url": photo_url,
                "video_thumb": None,
                "link_preview": None,
                "channel_subscribers": getattr(entity, "participants_count", None),
                "_grouped_id": getattr(msg, "grouped_id", None),
            })

        posts = _merge_telethon_grouped(raw_posts)
        return posts
    except Exception as e:
        logger.error("Failed to fetch channel %s via Telethon: %s", channel_ref, e)
        return []


@app.get("/api/avatar/{channel_key}")
async def get_avatar(channel_key: str):
    path = AVATAR_CACHE_DIR / f"{channel_key}.jpg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/thumb/{thumb_key}")
async def get_thumb(thumb_key: str):
    path = THUMB_CACHE_DIR / f"{thumb_key}.jpg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


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
    config = load_config()
    max_posts = config.get("max_posts", 20)

    public_feeds = [f for f in feeds if not f.get("is_private") and f.get("feed_url")]
    private_feeds = [f for f in feeds if f.get("is_private") and f.get("feed_url")]

    tg: TelegramClient | None = request.app.state.telegram
    all_posts = []

    if tg:
        media_sem = asyncio.Semaphore(config.get("media_concurrency", 5))
        telethon_tasks = []
        for f in public_feeds:
            username = normalize_channel(f["feed_url"])
            telethon_tasks.append(fetch_channel_posts_telethon(tg, username, limit=max_posts, media_sem=media_sem))
        for f in private_feeds:
            telethon_tasks.append(fetch_channel_posts_telethon(tg, PeerChannel(int(f["feed_url"])), limit=max_posts, media_sem=media_sem))
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


@app.post("/api/top-posts")
async def get_top_posts(request: Request, user: dict = Depends(require_auth)):
    config = load_config()
    ai_provider = config.get("ai_provider", "gemini")
    ai_model = config.get("ai_model", "gemini-2.0-flash-lite")

    if ai_provider == "gemini" and not GOOGLE_API_KEY:
        return JSONResponse(status_code=503, content={"detail": "AI ranking not configured (GOOGLE_API_KEY missing)"})
    if ai_provider == "groq" and not GROQ_API_KEY:
        return JSONResponse(status_code=503, content={"detail": "AI ranking not configured (GROQ_API_KEY missing)"})

    body = await request.json()
    posts = body.get("posts", [])
    if len(posts) < 10:
        return JSONResponse(status_code=400, content={"detail": "Need at least 10 posts to rank"})

    try:
        prompt_text = TOP10_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return JSONResponse(status_code=500, content={"detail": "top10_prompt.md not found"})

    posts = posts[:50]

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
                return JSONResponse(status_code=502, content={"detail": detail})
        except Exception as e:
            logger.error("AI API request failed (%s/%s): %s", ai_provider, ai_model, e)
            return JSONResponse(status_code=502, content={"detail": "AI service request failed"})

    try:
        data = resp.json()
        if ai_provider == "gemini":
            content = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        else:
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

    db: Database = request.app.state.db

    if user.get("user_id") != 1:
        if admin_only:
            raise HTTPException(status_code=403, detail="Only admin can create admin-only feeds")
        if await db.is_feed_admin_only(feed_url):
            raise HTTPException(status_code=403, detail="This channel is restricted to admin")

    result = await db.add_feed(user["user_id"], feed_url, is_private=is_private, admin_only=admin_only)
    if result is None:
        return JSONResponse(status_code=409, content={"detail": "Feed already exists"})
    return {"status": "success", "feed_url": feed_url}


@app.delete("/api/feeds")
async def delete_feed(request: Request, user: dict = Depends(require_auth)):
    body = await request.json()
    feed_url = body.get("feed_url", "").strip()
    if not feed_url:
        return JSONResponse(status_code=400, content={"detail": "feed_url is required"})
    db: Database = request.app.state.db
    removed = await db.remove_feed(user["user_id"], feed_url)
    if not removed:
        return JSONResponse(status_code=404, content={"detail": "Feed not found"})
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
    return posts


@app.post("/api/saved")
async def save_post(request: Request, user: dict = Depends(require_auth)):
    post = await request.json()
    db: Database = request.app.state.db
    result = await db.save_post(user["user_id"], post)
    if result is None:
        return {"status": "exists", "message": "Post already saved"}
    return {"status": "success"}


@app.delete("/api/saved/{channel}/{post_id}")
async def unsave_post(channel: str, post_id: str, request: Request, user: dict = Depends(require_auth)):
    db: Database = request.app.state.db
    removed = await db.unsave_post(user["user_id"], channel, post_id)
    if not removed:
        return JSONResponse(status_code=404, content={"detail": "Saved post not found"})
    return {"status": "success"}


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
async def root():
    return FileResponse(
        Path(__file__).parent / "static" / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
