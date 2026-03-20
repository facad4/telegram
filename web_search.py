"""
WebSearch class for searching the web and summarizing results.

This module provides a WebSearch class that:
1. Takes a Telegram post as input
2. Generates search terms using Mistral AI
3. Searches the web using Tavily API
4. Summarizes results using Mistral AI
5. Returns a structured summary with sources
"""

import json
import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class WebSearch:
    """Search the web and summarize results for Telegram posts."""

    def __init__(
        self,
        mistral_api_key: str | None = None,
        tavily_api_key: str | None = None,
        mistral_model: str = "mistral-large-latest",
        search_depth: str = "basic",
        max_results: int = 5,
    ):
        """Initialize WebSearch with API keys.

        Args:
            mistral_api_key: Mistral AI API key (defaults to MISTRAL_API_KEY env var)
            tavily_api_key: Tavily API key (defaults to TAVILY_API_KEY env var)
            mistral_model: Mistral model to use (default: mistral-large-latest)
            search_depth: Tavily search depth - "basic" or "advanced" (default: basic)
            max_results: Maximum number of search results to fetch (default: 5)
        """
        self.mistral_api_key = mistral_api_key or os.environ.get("MISTRAL_API_KEY", "")
        self.tavily_api_key = tavily_api_key or os.environ.get("TAVILY_API_KEY", "")
        self.mistral_model = mistral_model
        self.search_depth = search_depth
        self.max_results = max_results

        if not self.mistral_api_key:
            raise ValueError("MISTRAL_API_KEY is required")
        if not self.tavily_api_key:
            raise ValueError("TAVILY_API_KEY is required")

        self.search_terms_prompt = self._load_prompt("search_terms.md")
        self.summarize_prompt = self._load_prompt("summarize.md")

    def _load_prompt(self, filename: str) -> str:
        """Load a prompt file from disk."""
        prompt_path = Path(__file__).parent / filename
        try:
            return prompt_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    async def _call_mistral(self, system_prompt: str, user_content: str) -> str:
        """Call Mistral AI API.

        Args:
            system_prompt: System instruction for Mistral
            user_content: User message content

        Returns:
            Response text from Mistral

        Raises:
            Exception: If API call fails
        """
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.mistral_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.mistral_model,
                        "temperature": 0.3,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            except httpx.HTTPStatusError as e:
                logger.error(f"Mistral API error: HTTP {e.response.status_code}")
                raise Exception(f"Mistral API returned status {e.response.status_code}")
            except Exception as e:
                logger.error(f"Mistral API request failed: {e}")
                raise Exception(f"Mistral API request failed: {str(e)}")

    async def _generate_search_terms(self, post_text: str) -> str:
        """Generate search terms from a Telegram post using Mistral.

        Args:
            post_text: The Telegram post text

        Returns:
            Search terms as a string (newline-separated)
        """
        logger.info("Generating search terms for post")
        search_terms = await self._call_mistral(self.search_terms_prompt, post_text)
        logger.info(f"Generated search terms: {search_terms}")
        return search_terms

    async def _search_web(self, search_terms: str) -> list[dict]:
        """Search the web using Tavily API.

        Args:
            search_terms: Search terms (newline-separated)

        Returns:
            List of search results with title, url, and content

        Raises:
            Exception: If API call fails
        """
        terms_list = [term.strip() for term in search_terms.split("\n") if term.strip()]
        
        if not terms_list:
            logger.warning("No search terms generated")
            return []

        query = " ".join(terms_list)
        logger.info(f"Searching Tavily with query: {query}")

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Content-Type": "application/json"},
                    json={
                        "api_key": self.tavily_api_key,
                        "query": query,
                        "search_depth": self.search_depth,
                        "max_results": self.max_results,
                        "include_answer": False,
                        "include_raw_content": False,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                
                results = []
                for result in data.get("results", []):
                    results.append({
                        "title": result.get("title", ""),
                        "url": result.get("url", ""),
                        "content": result.get("content", ""),
                    })
                
                logger.info(f"Found {len(results)} search results")
                return results
            except httpx.HTTPStatusError as e:
                logger.error(f"Tavily API error: HTTP {e.response.status_code}")
                raise Exception(f"Tavily API returned status {e.response.status_code}")
            except Exception as e:
                logger.error(f"Tavily API request failed: {e}")
                raise Exception(f"Tavily API request failed: {str(e)}")

    async def _summarize_results(
        self, post_text: str, web_results: list[dict]
    ) -> str:
        """Summarize web search results using Mistral.

        Args:
            post_text: Original Telegram post text
            web_results: List of web search results

        Returns:
            Summary text
        """
        if not web_results:
            logger.warning("No web results to summarize")
            return "No relevant information found."

        results_text = "ORIGINAL POST:\n" + post_text + "\n\nWEB SEARCH RESULTS:\n"
        
        for result in web_results:
            results_text += "---\n"
            results_text += f"Title: {result['title']}\n"
            results_text += f"URL: {result['url']}\n"
            results_text += f"Content: {result['content']}\n"
        
        logger.info("Summarizing web results with Mistral")
        summary = await self._call_mistral(self.summarize_prompt, results_text)
        return summary

    async def search(self, post_text: str) -> dict:
        """Search the web and summarize results for a Telegram post.

        This is the main method that orchestrates the entire workflow:
        1. Generate search terms from the post
        2. Search the web using Tavily
        3. Summarize results using Mistral

        Args:
            post_text: The Telegram post text to analyze

        Returns:
            Dictionary with:
                - summary (str): The summarized context
                - sources (list[dict]): List of source URLs with titles

        Raises:
            Exception: If any step in the process fails
        """
        if not post_text or not post_text.strip():
            raise ValueError("post_text cannot be empty")

        try:
            search_terms = await self._generate_search_terms(post_text)
            
            web_results = await self._search_web(search_terms)
            
            summary = await self._summarize_results(post_text, web_results)
            
            sources = [
                {"title": result["title"], "url": result["url"]}
                for result in web_results
            ]
            
            return {
                "summary": summary,
                "sources": sources,
            }
        except Exception as e:
            logger.error(f"WebSearch failed: {e}")
            raise
