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

    print("\nChoose environment:")
    print("  1. Production (Render)")
    print("  2. Development (local)")
    choice = input("Enter 1 or 2: ").strip()
    device = "TGUpdates-Prod" if choice == "1" else "TGUpdates-Dev"

    phone = input("Enter your phone number (e.g. +1234567890): ").strip()

    client = TelegramClient(
        StringSession(), api_id, api_hash,
        device_model=device,
        system_version="1.0",
        app_version="1.0",
    )
    await client.connect()

    if not await client.is_user_authorized():
        await client.send_code_request(phone, force_sms=True)
        code = input("Enter the verification code: ").strip()
        try:
            await client.sign_in(phone, code)
        except Exception:
            password = input("Enter your 2FA password: ").strip()
            await client.sign_in(password=password)

    session_string = client.session.save()
    print(f"\nSession generated for: {device}")
    print(f"Session ends with: ...{session_string[-20:]}")

    await client.disconnect()

    print("\nCopy the string below and add it to your environment variables:\n")
    print(f"TELEGRAM_SESSION={session_string}\n")
    print("Also make sure these are set:")
    print(f"TELEGRAM_API_ID={api_id}")
    print(f"TELEGRAM_API_HASH={api_hash}")
    print(f"\nThis session will show as '{device}' in Telegram > Settings > Devices.")


if __name__ == "__main__":
    asyncio.run(main())
