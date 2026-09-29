"""End-to-end on the synthetic sample data with an in-memory database."""

import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from lead_engine import pipeline
from lead_engine.models import Base, Company, Signal
from lead_engine.sinks.local import LocalSink


class CountingSink(LocalSink):
    def __init__(self, out_dir):
        super().__init__(out_dir)
        self.calls = 0

    def upsert_lead(self, lead):
        self.calls += 1
        return super().upsert_lead(lead)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def routes(session) -> dict[str, str]:
    return {c.name: c.score.route for c in session.scalars(select(Company))}


def test_sample_run_dedups_scores_and_routes(session, tmp_path):
    sink = CountingSink(tmp_path)
    stats = pipeline.run(session, ["sample"], sink)

    assert stats.collected == 9
    assert stats.new_signals == 9
    # Falcon Pixel (domain match) and Qamar Cloud (name match) each appear twice.
    assert session.scalar(select(func.count()).select_from(Company)) == 7

    r = routes(session)
    assert r["Falcon Pixel Studios FZ-LLC"] == "high"
    assert r["Northwind Academy"] == "high"            # fresh IT tender
    assert r["Duneline Logistics L.L.C"] == "high"
    assert r["Qamar Cloud Technologies"] == "review"   # strong intent, no contact
    assert r["Brightwave Events"] == "review"          # outside UAE
    assert r["Oasis Sweets Trading"] == "archive"
    assert r["Sahara Code Labs"] == "archive"          # 150-day-old signal

    leads = json.loads((tmp_path / "leads.json").read_text(encoding="utf-8"))
    assert set(leads) == {"falconpixel.example", "northwindacademy.example", "duneline.example",
                          "qamarcloud.example", "brightwave.example"}
    assert stats.exported == 5


def test_rerun_is_idempotent(session, tmp_path):
    sink = CountingSink(tmp_path)
    pipeline.run(session, ["sample"], sink)
    first_calls = sink.calls

    stats = pipeline.run(session, ["sample"], sink)
    assert stats.new_signals == 0
    assert stats.duplicate_signals == 9
    assert session.scalar(select(func.count()).select_from(Signal)) == 9
    assert sink.calls == first_calls  # nothing changed → nothing re-pushed
    assert stats.unchanged == 5
