"""News collector: RSS feeds → keyword prefilter → LLM extraction → RawSignal."""

import calendar
import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Callable, Iterator

import feedparser
import httpx

from lead_engine.extract import extract_from_news
from lead_engine.schemas import RawSignal
from lead_engine.settings import load_config

log = logging.getLogger(__name__)

FETCH_ATTEMPTS = 3
USER_AGENT = "Mozilla/5.0 (compatible; TechLeadEngine/0.1)"


def _published(entry) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        # feedparser's struct_time is UTC; timegm (unlike mktime) doesn't apply the local offset.
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return datetime.now(timezone.utc)


def _clean(text: str) -> str:
    """Feed summaries are often HTML (Google News: a link plus the headline)."""
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def _fetch(client: httpx.Client, url: str) -> feedparser.FeedParserDict | None:
    """Fetch with retries and exponential backoff; news feeds fail transiently."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            resp = client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if parsed.entries:
                return parsed
            reason = parsed.get("bozo_exception", "no entries")
        except httpx.HTTPError as exc:
            reason = exc
        if attempt < FETCH_ATTEMPTS:
            time.sleep(2 ** attempt)
    log.warning("feed %s failed after %d attempts: %s", url, FETCH_ATTEMPTS, reason)
    return None


class RssNewsCollector:
    name = "rss"

    def __init__(self, max_items_per_feed: int = 30, skip_url: Callable[[str], bool] | None = None):
        self.cfg = load_config("sources")
        self.max_items = max_items_per_feed
        self.keywords = [k.lower() for k in self.cfg["prefilter_keywords"]]
        # Lets the pipeline skip articles already stored, saving LLM calls on re-runs.
        self.skip_url = skip_url or (lambda url: False)

    def _passes_prefilter(self, text: str) -> bool:
        text = text.lower()
        return any(k in text for k in self.keywords)

    def collect(self) -> Iterator[RawSignal]:
        # Generous timeout: the first call also loads the model into GPU memory.
        client = httpx.Client(timeout=300)
        for feed in self.cfg["rss_feeds"]:
            parsed = _fetch(client, feed["url"])
            if parsed is None:
                continue

            for entry in parsed.entries[: self.max_items]:
                title = _clean(entry.get("title", ""))
                text = _clean(entry.get("summary", ""))
                url = entry.get("link", "")
                if not self._passes_prefilter(f"{title} {text}") or self.skip_url(url):
                    continue

                result = extract_from_news(title, text, client=client)
                if result is None:
                    continue

                yield RawSignal(
                    company_name=result.company_name,
                    company_domain=result.company_domain,
                    segment=result.segment,
                    city=result.city,
                    country=result.country,
                    signal_type=result.signal_type,
                    title=title,
                    url=url,
                    source=f"{self.name}:{feed['name']}",
                    evidence=result.evidence_quote,
                    extraction_confidence=result.confidence,
                    published_at=_published(entry),
                )
