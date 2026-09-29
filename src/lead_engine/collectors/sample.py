"""Offline collector that loads synthetic signals from eval/sample_signals.json.

All companies in that file are fictional. Use it to demo and test the pipeline
without network access, an LLM or real company data.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Iterator

from lead_engine.schemas import RawSignal
from lead_engine.settings import ROOT


class SampleCollector:
    name = "sample"

    def __init__(self, path=ROOT / "eval" / "sample_signals.json"):
        self.path = path

    def collect(self) -> Iterator[RawSignal]:
        now = datetime.now(timezone.utc)
        with open(self.path, encoding="utf-8") as f:
            items = json.load(f)
        for item in items:
            # Dates are stored as "days ago" so the demo never goes stale.
            days_ago = item.pop("days_ago")
            yield RawSignal(**item, source=self.name, published_at=now - timedelta(days=days_ago))
