import asyncio
import os
import subprocess
import time
import webbrowser
from urllib.parse import quote_plus

import pyautogui
import pyperclip
import win32con
import win32gui

from perplexity_search import PerplexitySearchError

_BRAVE_PATHS = [
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
]

_WINDOW_WAIT = 30   # seconds to wait for Brave window to appear
_ANSWER_WAIT = 60   # seconds to wait for clipboard to change after shortcut


def _brave_windows() -> set[int]:
    seen = set()

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and "brave" in win32gui.GetWindowText(hwnd).lower():
            seen.add(hwnd)

    win32gui.EnumWindows(_cb, None)
    return seen


def _find_new_brave_window(before: set[int], timeout: float) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = _brave_windows() - before
        if new:
            return next(iter(new))
        time.sleep(0.5)
    return None


def _focus_window(hwnd: int) -> None:
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    rect = win32gui.GetWindowRect(hwnd)
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    pyautogui.click(cx, cy)
    time.sleep(0.3)


def _search_sync(url_query: str, timeout: int) -> str:
    url = f"https://www.perplexity.ai/search?q={quote_plus(url_query)}"
    previous_clip = pyperclip.paste()
    existing_windows = _brave_windows()

    brave_path = next((p for p in _BRAVE_PATHS if os.path.exists(p)), None)
    if brave_path:
        print(f"[debug] launching: {brave_path}")
        subprocess.Popen([brave_path, "--new-window", url])
    else:
        print("[debug] brave not found, using default browser")
        webbrowser.open(url)

    print("[debug] waiting for new brave window...")
    hwnd = _find_new_brave_window(existing_windows, timeout=_WINDOW_WAIT)
    if hwnd is None:
        raise PerplexitySearchError("Brave window not found")
    print(f"[debug] found window: {win32gui.GetWindowText(hwnd)!r}")

    print("[debug] waiting for answer to stream (20s)...")
    time.sleep(20)

    _focus_window(hwnd)
    print("[debug] sending Ctrl+Shift+Y")
    pyautogui.hotkey("ctrl", "shift", "y")

    print("[debug] polling clipboard...")
    deadline = time.time() + timeout
    ticks = 0
    while time.time() < deadline:
        clip = pyperclip.paste()
        if clip != previous_clip:
            print(clip)
            return clip
        ticks += 1
        if ticks % 10 == 0:
            print(f"[debug] still waiting... ({ticks // 2}s)")
        time.sleep(0.5)

    raise PerplexitySearchError("Timed out waiting for clipboard after Ctrl+Shift+Y")


class PerplexityBrowser:
    def __init__(self, prompt_file: str = "", headless: bool = True):
        self._prompt = ""
        if prompt_file and os.path.exists(prompt_file):
            with open(prompt_file, encoding="utf-8") as f:
                self._prompt = f.read().strip()

    async def search(self, query: str, timeout: int = _ANSWER_WAIT, max_chars: int = 0) -> dict:
        parts = [self._prompt] if self._prompt else []
        if max_chars > 0:
            parts.append(f"Keep your total answer under {max_chars} characters.")
        parts.append(query)
        full_query = "\n\n".join(parts)
        summary = await asyncio.to_thread(_search_sync, full_query, timeout)
        return {"summary": summary, "sources": [], "query": query}


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "hello world"
    result = asyncio.run(PerplexityBrowser().search(q))

    print(result["summary"] or "No answer received.")
