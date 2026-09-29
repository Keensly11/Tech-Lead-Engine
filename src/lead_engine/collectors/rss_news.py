"""News collector: RSS feeds → keyword prefilter → LLM extraction → RawSignal."""

import logging
import time
from datetime import datetime, timezone
from typing import Iterator

import feedparser
import httpx

from lead_engine.extract import extract_from_news
from lead_engine.schemas import RawSignal
from lead_engine.settings import load_config

log = logging.getLogger(__name__)


def _published(entry) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
    return datetime.now(timezone.utc)


class RssNewsCollector:
    name = "rss"

    def __init__(self, max_items_per_feed: int = 30):
        self.cfg = load_config("sources")
        self.max_items = max_items_per_feed
        self.keywords = [k.lower() for k in self.cfg["prefilter_keywords"]]

    def _passes_prefilter(self, text: str) -> bool:
        text = text.lower()
        return any(k in text for k in self.keywords)

    def collect(self) -> Iterator[RawSignal]:
        client = httpx.Client(timeout=120)
        for feed in self.cfg["rss_feeds"]:
            parsed = feedparser.parse(feed["url"])
            if parsed.bozo and not parsed.entries:
                log.warning("feed %s failed: %s", feed["name"], parsed.bozo_exception)
                continue

            for entry in parsed.entries[: self.max_items]:
                title = entry.get("title", "")
                text = entry.get("summary", "")
                if not self._passes_prefilter(f"{title} {text}"):
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
                    url=entry.get("link", ""),
                    source=f"{self.name}:{feed['name']}",
                    evidence=result.evidence_quote,
                    extraction_confidence=result.confidence,
                    published_at=_published(entry),
                )
