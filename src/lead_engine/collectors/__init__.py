from lead_engine.collectors.rss_news import RssNewsCollector
from lead_engine.collectors.sample import SampleCollector

COLLECTORS = {
    "sample": SampleCollector,
    "rss": RssNewsCollector,
}
