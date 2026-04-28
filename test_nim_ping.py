#!/usr/bin/env python3
"""Smoke-test the NIM provider with a one-word prompt.

Run: python test_nim_ping.py [model]
Default model: z-ai/glm-5.1
"""
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

from test_generate_alternate_digest import NIMProvider

load_dotenv()


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else "z-ai/glm-5.1"
    api_key = os.environ.get("NVIDIA_API_KEY", "")
    if not api_key:
        log("ERROR: NVIDIA_API_KEY not set in .env")
        sys.exit(1)

    provider = NIMProvider(api_key, model)
    log(f"Pinging NIM model {model!r} with 'Hi'...")
    started = time.monotonic()
    reply = provider.chat_json(
        system_prompt="You are a friendly assistant. Reply briefly.",
        user_content="Hi",
        temperature=0.2,
        timeout=900.0,
    )
    elapsed = time.monotonic() - started
    log(f"Reply received in {elapsed:.1f}s")
    log("--- reply ---")
    print(reply)


if __name__ == "__main__":
    main()
