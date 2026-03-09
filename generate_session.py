"""
One-time script to generate a Telethon StringSession.

Run locally:
    pip install telethon
    python generate_session.py

You will be prompted for:
  1. Your Telegram API ID     (from https://my.telegram.org)
  2. Your Telegram API Hash   (from https://my.telegram.org)
  3. Your phone number        (e.g. +1234567890)
  4. The verification code Telegram sends you

The script prints a session string. Copy it and set it as the
TELEGRAM_SESSION environment variable on your server (e.g. Render.com).
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=== Telegram StringSession Generator ===\n")

    api_id = int(input("Enter your API ID: ").strip())
    api_hash = input("Enter your API Hash: ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()

    session_string = client.session.save()
    await client.disconnect()

    print("\n✅ Session generated successfully!\n")
    print("Copy the string below and add it to your environment variables:\n")
    print(f"TELEGRAM_SESSION={session_string}\n")
    print("Also make sure these are set:")
    print(f"TELEGRAM_API_ID={api_id}")
    print(f"TELEGRAM_API_HASH={api_hash}")


if __name__ == "__main__":
    asyncio.run(main())
