"""Fetch and extract text content from web URLs."""

import logging
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Truncate extracted text to ~20k tokens (~80k chars)
MAX_CHARS = 80_000

READABLE_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}


async def fetch_url(url: str, timeout: float = 5.0) -> str:
    """Fetch a URL and return its text content.

    Returns a human-readable string: either the page text or an error message.
    Never raises — all errors are returned as strings.
    """
    # Validate URL scheme
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL."
    if parsed.scheme not in ("http", "https"):
        return f"Unsupported URL scheme: {parsed.scheme}. Only http/https are supported."
    if not parsed.netloc:
        return "Invalid URL — no host specified."

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
            follow_redirects=True,
            headers={"User-Agent": "Memchat/1.0 (URL Fetch)"},
        ) as client:
            response = await client.get(url)

        if response.status_code >= 400:
            return f"HTTP {response.status_code} error fetching {url}."

        # Check content type
        content_type = response.headers.get("content-type", "")
        mime = content_type.split(";")[0].strip().lower()
        if mime not in READABLE_CONTENT_TYPES:
            return f"Cannot read this content type ({mime})."

        html = response.text
        return _extract_text(html)

    except httpx.TimeoutException:
        return f"Timed out fetching {url}."
    except httpx.ConnectError:
        return f"Could not connect to {url}."
    except Exception as exc:
        logger.warning("URL fetch error for %s: %s", url, exc)
        return f"Error fetching URL: {exc}"


def _extract_text(html: str) -> str:
    """Parse HTML and extract readable text content."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content elements
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)

    # Truncate to budget
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[Content truncated]"

    return text
