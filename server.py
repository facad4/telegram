import asyncio
import base64
import io
import json
import logging
import os
import re
import secrets
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
from telethon.tl.types import Channel, Chat, MessageMediaPhoto, MessageMediaDocument

from database import Database

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    users = await db.get_all_users()
    logger.info("Found %d users in database:", len(users))
    for user in users:
        logger.info("  User: %s (created: %s)", user.get("user_name"), user.get("created_at"))
    feeds = await db.get_all_feeds()
    logger.info("Found %d feeds in database:", len(feeds))
    for feed in feeds:
        logger.info("  Feed: user_id=%s, url=%s", feed.get("user_id"), feed.get("feed_url"))

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
        })

    return posts


async def fetch_private_channel_posts(
    tg: TelegramClient, channel_id: int, limit: int = 30,
) -> list[dict]:
    """Fetch recent posts from a private channel via Telethon API."""
    try:
        entity = await tg.get_entity(channel_id)
        channel_title = getattr(entity, "title", str(channel_id))

        channel_photo = None
        try:
            photo_file = await tg.download_profile_photo(entity, file=bytes)
            if photo_file:
                b64 = base64.b64encode(photo_file).decode("ascii")
                channel_photo = f"data:image/jpeg;base64,{b64}"
        except Exception:
            pass

        messages = await tg.get_messages(entity, limit=limit)
        posts = []
        for msg in messages:
            if msg.text is None and msg.media is None:
                continue

            text_plain = msg.text or ""
            text_html = f"<div>{text_plain}</div>" if text_plain else ""
            views = str(msg.views) if msg.views else ""
            datetime_str = msg.date.isoformat() if msg.date else ""

            photo_url = None
            video_thumb = None
            if isinstance(msg.media, MessageMediaPhoto):
                try:
                    buf = io.BytesIO()
                    await tg.download_media(msg.media, file=buf, thumb=-1)
                    buf.seek(0)
                    b64 = base64.b64encode(buf.read()).decode("ascii")
                    photo_url = f"data:image/jpeg;base64,{b64}"
                except Exception:
                    pass

            real_channel_id = getattr(entity, "id", channel_id)

            posts.append({
                "channel": str(real_channel_id),
                "channel_title": channel_title,
                "channel_photo": channel_photo,
                "post_id": str(msg.id),
                "post_url": f"https://t.me/c/{real_channel_id}/{msg.id}",
                "text_html": text_html,
                "text_plain": text_plain,
                "views": views,
                "datetime": datetime_str,
                "photo_url": photo_url,
                "video_thumb": video_thumb,
                "link_preview": None,
            })

        return posts
    except Exception as e:
        logger.error("Failed to fetch private channel %s: %s", channel_id, e)
        return []


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

    public_channels = [normalize_channel(f["feed_url"]) for f in public_feeds]
    async with httpx.AsyncClient(timeout=15.0) as client:
        results = await asyncio.gather(
            *[fetch_channel_html(client, ch) for ch in public_channels]
        )

    all_posts = []
    for ch, html in zip(public_channels, results):
        if html:
            all_posts.extend(parse_channel_posts(html, ch))

    tg: TelegramClient | None = request.app.state.telegram
    if tg and private_feeds:
        private_results = await asyncio.gather(
            *[
                fetch_private_channel_posts(tg, int(f["feed_url"]), limit=max_posts)
                for f in private_feeds
            ]
        )
        for posts in private_results:
            all_posts.extend(posts)

    all_posts.sort(key=lambda p: p["datetime"], reverse=True)
    return all_posts[:max_posts]


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
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
