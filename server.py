import asyncio
import json
import logging
import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

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


@app.get("/api/posts")
async def get_posts():
    config = load_config()
    channels = [normalize_channel(ch) for ch in config.get("channels", [])]
    max_posts = config.get("max_posts", 20)

    async with httpx.AsyncClient(timeout=15.0) as client:
        results = await asyncio.gather(
            *[fetch_channel_html(client, ch) for ch in channels]
        )

    all_posts = []
    for ch, html in zip(channels, results):
        if html:
            all_posts.extend(parse_channel_posts(html, ch))

    all_posts.sort(key=lambda p: p["datetime"], reverse=True)
    return all_posts[:max_posts]


@app.get("/api/config")
async def get_config():
    config = load_config()
    return {
        "refresh_interval_minutes": config.get("refresh_interval_minutes", 5),
        "scroll_speed": config.get("scroll_speed", 50),
    }


@app.get("/api/paz/posts")
async def get_paz_posts():
    config = load_config()
    channels = [normalize_channel(ch) for ch in config.get("paz_channels", [])]
    max_posts = config.get("max_posts", 20)

    async with httpx.AsyncClient(timeout=15.0) as client:
        results = await asyncio.gather(
            *[fetch_channel_html(client, ch) for ch in channels]
        )

    all_posts = []
    for ch, html in zip(channels, results):
        if html:
            all_posts.extend(parse_channel_posts(html, ch))

    all_posts.sort(key=lambda p: p["datetime"], reverse=True)
    return all_posts[:max_posts]


@app.get("/api/admin/config")
async def get_full_config():
    return load_config()


@app.post("/api/admin/config")
async def update_config(request: Request):
    try:
        config_data = await request.json()
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config_data, f, indent=2)
        return {"status": "success", "message": "Configuration updated"}
    except Exception as e:
        logger.error("Failed to update config: %s", e)
        return {"status": "error", "message": str(e)}


@app.get("/api/admin/config/download")
async def download_config():
    return FileResponse(
        CONFIG_PATH,
        media_type="application/json",
        filename="config.json"
    )


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/")
async def root():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
