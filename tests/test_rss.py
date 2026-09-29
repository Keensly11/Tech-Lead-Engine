import time
from datetime import datetime, timezone

from lead_engine.collectors.rss_news import _clean, _published


def test_clean_strips_html_and_entities():
    raw = '<a href="https://news.google.com/x">Acme opens Dubai office</a>&nbsp;&nbsp;<font color="#6f6f6f">Gulf News</font>'
    assert _clean(raw) == "Acme opens Dubai office Gulf News"


def test_published_is_utc_regardless_of_local_timezone():
    struct = time.strptime("2026-09-29 08:00:00", "%Y-%m-%d %H:%M:%S")
    assert _published({"published_parsed": struct}) == datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
