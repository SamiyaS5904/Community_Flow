"""
services/search_service.py
===========================
Serper.

Search is an enhancement, not a dependency: a post whose slot asked for
research is still a post if Serper is down. So failures raise SearchError and
the caller decides — the workflow writes without research rather than losing
the post, while discovery, which has nothing to do without results, reports it.

The bare `return []` on a missing key stays a silent no-op on purpose: a
deployment with no Serper key is a valid configuration, not a fault.
"""
import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from exceptions import SearchError

log = logging.getLogger(__name__)

class SearchService:
    SERPER_URL = "https://google.serper.dev/search"

    def __init__(self, api_key: str):
        self.api_key = api_key

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def _fetch(self, query: str, num_results: int) -> list[dict]:
        response = httpx.post(
            self.SERPER_URL,
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num_results},
            timeout=15,
        )
        response.raise_for_status()
        return response.json().get("organic", [])

    def search(self, query: str, num_results: int = 5) -> list[dict]:
        """Organic results for one query. Raises SearchError after retries."""
        if not self.api_key:
            return []
        try:
            return self._fetch(query, num_results)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            detail = "quota exhausted or key rejected" if code in (401, 403, 429)                 else f"HTTP {code}"
            raise SearchError(f"Serper refused the query ({detail}).") from exc
        except Exception as exc:
            raise SearchError(f"Serper unreachable: {exc}") from exc

    def format_results(self, query: str, results: list[dict]) -> str:
        """Formats search results as a block to inject into the LLM prompt."""
        if not results:
            return ""

        lines = ["[CURRENT INFORMATION FROM WEB SEARCH]"]
        lines.append(f"Query: {query}")
        lines.append("")
        for i, r in enumerate(results[:5], 1):
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            link = r.get("link", "")
            lines.append(f"{i}. {title}")
            if snippet:
                lines.append(f"   {snippet}")
            if link:
                lines.append(f"   Source: {link}")
            lines.append("")
        lines.append("[END OF SEARCH RESULTS]")
        return "\n".join(lines)
