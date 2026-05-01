"""
PerplexitySearch class for querying perplexity.ai via headless browser.

Drives a headless Chromium instance (Playwright) to submit a query to the
public perplexity.ai website (no API key, no login) and returns the answer
text plus the cited sources.

One-time setup after installing requirements:
    python -m playwright install chromium
"""

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from playwright.async_api import (
    Browser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

logger = logging.getLogger(__name__)


class PerplexitySearchError(Exception):
    """Raised when the Perplexity website cannot be queried successfully."""


class PerplexitySearch:
    """Query perplexity.ai through its public website using a headless browser."""

    SEARCH_URL = "https://www.perplexity.ai/search?q={q}"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    VIEWPORT = {"width": 1366, "height": 900}

    SEL_ANSWER = '#main, main'
    SEL_PROSE = '.prose, [class*="prose"]'
    SEL_SOURCE_LINKS = 'a[href^="http"]'
    DEBUG_SCREENSHOT = 'perplexity_debug.png'

    STABLE_POLL_INTERVAL_S = 0.75
    STABLE_REQUIRED_S = 2.0

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 60_000,
        nav_timeout_ms: int = 30_000,
        prompt_file: str = "perplexity_prompt.md",
    ):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.nav_timeout_ms = nav_timeout_ms
        self.prompt = self._load_prompt(prompt_file)

    def _load_prompt(self, filename: str) -> str:
        path = Path(__file__).parent / filename
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise FileNotFoundError(f"Prompt file not found: {path}")

    async def search(self, query: str) -> dict[str, Any]:
        """Submit `query` to perplexity.ai and return the answer + sources.

        Returns:
            {
              "summary": "<answer text>",
              "sources": [{"title": "...", "url": "..."}, ...],
              "query": "<echoed query>"
            }

        Raises:
            ValueError: if `query` is empty.
            PerplexitySearchError: on navigation/timeout/extraction failure.
        """
        if not query or not query.strip():
            raise ValueError("query cannot be empty")
        query = query.strip()
        full_query = f"{self.prompt}\n\n{query}" if self.prompt else query

        playwright: Playwright | None = None
        browser: Browser | None = None
        page: Page | None = None
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                user_agent=self.USER_AGENT,
                viewport=self.VIEWPORT,
                locale="en-US",
            )
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)
            page.set_default_navigation_timeout(self.nav_timeout_ms)

            search_url = self.SEARCH_URL.format(q=quote_plus(full_query))
            logger.info("Navigating to %s", search_url)
            try:
                await page.goto(search_url, wait_until="networkidle")
            except PlaywrightTimeoutError:
                logger.info("networkidle wait timed out; continuing")

            await self._wait_for_answer(page)

            summary = await self._extract_answer(page)
            sources = await self._extract_sources(page)

            if not summary:
                raise PerplexitySearchError("Empty answer extracted from Perplexity")

            logger.info(
                "Perplexity answer: %d chars, %d sources", len(summary), len(sources)
            )
            return {"summary": summary, "sources": sources, "query": query}

        except PlaywrightTimeoutError as e:
            await self._save_debug_screenshot(page)
            raise PerplexitySearchError(f"Timeout while querying Perplexity: {e}") from e
        except PerplexitySearchError:
            await self._save_debug_screenshot(page)
            raise
        except Exception as e:
            await self._save_debug_screenshot(page)
            raise PerplexitySearchError(f"Perplexity search failed: {e}") from e
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception as e:
                    logger.warning("Error closing browser: %s", e)
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception as e:
                    logger.warning("Error stopping Playwright: %s", e)

    async def _save_debug_screenshot(self, page: Page | None) -> None:
        if page is None:
            return
        try:
            await page.screenshot(path=self.DEBUG_SCREENSHOT, full_page=True)
            logger.info("Saved debug screenshot to %s", self.DEBUG_SCREENSHOT)
        except Exception as e:
            logger.warning("Could not save debug screenshot: %s", e)

    async def _wait_for_answer(self, page: Page) -> None:
        """Wait for an answer to render and stop streaming."""
        await page.locator(self.SEL_PROSE).first.wait_for(state="visible")

        last_len = -1
        stable_for = 0.0
        deadline = self.timeout_ms / 1000.0
        elapsed = 0.0
        while elapsed < deadline:
            await asyncio.sleep(self.STABLE_POLL_INTERVAL_S)
            elapsed += self.STABLE_POLL_INTERVAL_S
            try:
                text = await page.locator(self.SEL_PROSE).first.inner_text()
            except Exception:
                text = ""
            cur_len = len(text or "")
            if cur_len > 0 and cur_len == last_len:
                stable_for += self.STABLE_POLL_INTERVAL_S
                if stable_for >= self.STABLE_REQUIRED_S:
                    return
            else:
                stable_for = 0.0
                last_len = cur_len
        raise PerplexitySearchError(
            "Answer did not finish streaming within timeout"
        )

    async def _extract_answer(self, page: Page) -> str:
        prose = page.locator(self.SEL_PROSE).first
        text = await prose.inner_text()
        return (text or "").strip()

    async def _extract_sources(self, page: Page) -> list[dict[str, str]]:
        """Collect external links from the answer area, de-duplicated by URL."""
        anchors = page.locator(self.SEL_SOURCE_LINKS)
        count = await anchors.count()
        seen: set[str] = set()
        sources: list[dict[str, str]] = []
        for i in range(count):
            a = anchors.nth(i)
            try:
                href = await a.get_attribute("href")
                title = (await a.inner_text()) or ""
            except Exception:
                continue
            if not href or href.startswith("https://www.perplexity.ai"):
                continue
            if href in seen:
                continue
            seen.add(href)
            sources.append({"title": title.strip() or href, "url": href})
        return sources
