"""Fetch and prepare research context from URLs and card description."""
from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

MAX_ARTICLE_CHARS = 5000
FETCH_TIMEOUT = 10


def extract_urls(text: str) -> list[str]:
    """Find all URLs in text."""
    return re.findall(r'https?://[^\s<>")\]]+', text)


def extract_instructions(text: str) -> str:
    """Return description text with URLs stripped out (the human instructions)."""
    cleaned = re.sub(r'https?://[^\s<>")\]]+', '', text)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned


def _strip_html(html: str) -> str:
    """Basic HTML-to-text: remove scripts/nav/footer, strip tags, clean whitespace."""
    text = re.sub(
        r'<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>',
        '', html, flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r'<[^>]+>', ' ', text)
    text = (
        text.replace('&amp;', '&')
        .replace('&lt;', '<')
        .replace('&gt;', '>')
        .replace('&quot;', '"')
        .replace('&#39;', "'")
        .replace('&nbsp;', ' ')
    )
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n[ \t]*\n', '\n\n', text)
    return text.strip()


def _is_twitter_url(url: str) -> bool:
    """Check if URL is a Twitter/X tweet."""
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower().replace("www.", "")
    return any(d in domain for d in ["twitter.com", "x.com"])


async def _fetch_tweet_oembed(url: str) -> str | None:
    """Fetch tweet text via Twitter's public oEmbed API (no auth needed)."""
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(
                "https://publish.twitter.com/oembed",
                params={"url": url, "omit_script": "true"},
            )
            resp.raise_for_status()
            data = resp.json()
            html = data.get("html", "")
            author = data.get("author_name", "")
            text = _strip_html(html)
            if text:
                return f"Tweet by {author}: {text}" if author else text
            return None
    except Exception as e:
        logger.warning("oEmbed failed for %s: %s", url, e)
        return None


async def fetch_url_content(url: str) -> str | None:
    """Fetch a URL and extract its text content. Returns None on failure."""
    if _is_twitter_url(url):
        return await _fetch_tweet_oembed(url)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)",
            })
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                logger.info("Skipping non-text URL: %s (%s)", url, content_type)
                return None

            if "text/plain" in content_type:
                return resp.text[:MAX_ARTICLE_CHARS]

            text = _strip_html(resp.text)
            return text[:MAX_ARTICLE_CHARS] if text else None

    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None


async def prepare_context(description: str) -> dict:
    """Parse card description into instructions + fetched article content.

    Returns dict with:
        instructions: str  — human-written notes (non-URL text)
        articles: list[dict]  — [{"url": str, "content": str}, ...]
    """
    if not description:
        return {"instructions": "", "articles": []}

    instructions = extract_instructions(description)
    urls = extract_urls(description)

    articles = []
    for url in urls[:15]:
        content = await fetch_url_content(url)
        if content:
            articles.append({"url": url, "content": content})
            logger.info("Fetched %s: %d chars", url, len(content))
        else:
            logger.info("No content from %s (skipped)", url)

    return {"instructions": instructions, "articles": articles}
