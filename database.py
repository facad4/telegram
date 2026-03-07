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
        response = await self.client.table("Users").select("*").execute()
        return response.data

    async def get_all_feeds(self) -> list[dict]:
        response = await self.client.table("Feeds").select("*").execute()
        return response.data

    async def authenticate_user(self, user_name: str, password: str) -> dict | None:
        response = (
            await self.client.table("Users")
            .select("*")
            .eq("user_name", user_name)
            .eq("User_password", password)
            .execute()
        )
        return response.data[0] if response.data else None
