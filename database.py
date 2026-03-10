import json
import os
import logging

from supabase._async.client import AsyncClient, create_client

logger = logging.getLogger(__name__)


class Database:
    client: AsyncClient

    @classmethod
    async def create(cls) -> "Database":
        instance = cls()
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        instance.client = await create_client(url, key)
        logger.info("Supabase async client initialized")
        return instance

    async def get_all_users(self) -> list[dict]:
        response = await self.client.table("users").select("*").execute()
        return response.data

    async def get_all_feeds(self) -> list[dict]:
        response = await self.client.table("feeds").select("*").execute()
        return response.data

    async def authenticate_user(self, user_name: str, password: str) -> dict | None:
        response = (
            await self.client.table("users")
            .select("*")
            .eq("user_name", user_name)
            .eq("User_password", password)
            .execute()
        )
        return response.data[0] if response.data else None

    async def get_feeds_for_user(self, user_id: int) -> list[dict]:
        response = (
            await self.client.table("feeds")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
        return response.data

    async def add_feed(
        self, user_id: int, feed_url: str,
        is_private: bool = False, admin_only: bool = False,
    ) -> dict | None:
        """Insert a feed row. Returns None if (user_id, feed_url) already exists."""
        existing = (
            await self.client.table("feeds")
            .select("*")
            .eq("user_id", user_id)
            .eq("feed_url", feed_url)
            .execute()
        )
        if existing.data:
            return None
        response = (
            await self.client.table("feeds")
            .insert({
                "user_id": user_id,
                "feed_url": feed_url,
                "is_private": is_private,
                "admin_only": admin_only,
            })
            .execute()
        )
        return response.data[0] if response.data else None

    async def is_feed_admin_only(self, feed_url: str) -> bool:
        """Check if any feed row with this URL is marked admin_only."""
        response = (
            await self.client.table("feeds")
            .select("admin_only")
            .eq("feed_url", feed_url)
            .eq("admin_only", True)
            .limit(1)
            .execute()
        )
        return bool(response.data)

    async def remove_feed(self, user_id: int, feed_url: str) -> bool:
        response = (
            await self.client.table("feeds")
            .delete()
            .eq("user_id", user_id)
            .eq("feed_url", feed_url)
            .execute()
        )
        return bool(response.data)

    async def get_saved_posts(self, user_id: int) -> list[dict]:
        response = (
            await self.client.table("save_for_later")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        posts = []
        for row in response.data:
            try:
                post = json.loads(row["saved_post"])
                post["saved_at"] = row.get("created_at", "")
                posts.append(post)
            except (json.JSONDecodeError, KeyError):
                logger.warning("Skipping malformed saved post row id=%s", row.get("id"))
        return posts

    async def save_post(self, user_id: int, post: dict) -> dict | None:
        """Insert a saved post. Returns None if the same post is already saved (unique constraint)."""
        try:
            response = (
                await self.client.table("save_for_later")
                .insert({
                    "user_id": user_id,
                    "channel": post.get("channel", ""),
                    "post_id": post.get("post_id", ""),
                    "saved_post": json.dumps(post, ensure_ascii=False),
                })
                .execute()
            )
            return response.data[0] if response.data else None
        except Exception:
            return None

    async def unsave_post(self, user_id: int, channel: str, post_id: str) -> bool:
        response = (
            await self.client.table("save_for_later")
            .delete()
            .eq("user_id", user_id)
            .eq("channel", channel)
            .eq("post_id", post_id)
            .execute()
        )
        return bool(response.data)
