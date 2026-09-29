from datetime import datetime, timedelta, timezone

import pytest

from lead_engine.router import route
from lead_engine.scoring import CompanyFacts, ScoreResult, SignalFacts, recency_decay, score_company

NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


def sig(kind: str, days_ago: float, conf: float = 1.0) -> SignalFacts:
    return SignalFacts(kind, f"{kind} news", f"https://x.example/{kind}", NOW - timedelta(days=days_ago), conf)


UAE_FILM = CompanyFacts(segment="film_media", employee_count=85, city="Abu Dhabi", country="UAE")


def test_recency_decay_half_life():
    assert recency_decay(0, 30) == 1.0
    assert recency_decay(30, 30) == pytest.approx(0.5)
    assert recency_decay(60, 30) == pytest.approx(0.25)


def test_perfect_fit_for_uae_midsize_film_company():
    r = score_company(UAE_FILM, [], [], now=NOW)
    assert r.fit == pytest.approx(1.0)
    assert r.intent == 0.0
    assert r.priority == 0.0


def test_intent_noisy_or_combines_without_exceeding_one():
    one = score_company(UAE_FILM, [sig("production_launch", 0)], [], now=NOW).intent
    two = score_company(UAE_FILM, [sig("production_launch", 0), sig("hiring_surge", 0)], [], now=NOW).intent
    assert one == pytest.approx(0.7)
    assert two == pytest.approx(1 - 0.3 * 0.4)
    assert one < two < 1.0


def test_many_reports_of_one_event_count_once():
    one = score_company(UAE_FILM, [sig("new_office", 4)], [], now=NOW)
    echoed = score_company(UAE_FILM, [sig("new_office", d) for d in (4, 5, 6, 6)], [], now=NOW)
    assert echoed.intent == pytest.approx(one.intent)
    assert any("+3 more report" in line for line in echoed.explanation)


def test_same_type_events_far_apart_both_count():
    one = score_company(UAE_FILM, [sig("new_office", 2)], [], now=NOW).intent
    two = score_company(UAE_FILM, [sig("new_office", 2), sig("new_office", 60)], [], now=NOW).intent
    assert two > one


def test_old_signals_count_less_and_ancient_ones_not_at_all():
    fresh = score_company(UAE_FILM, [sig("new_office", 1)], [], now=NOW).intent
    stale = score_company(UAE_FILM, [sig("new_office", 60)], [], now=NOW).intent
    ancient = score_company(UAE_FILM, [sig("new_office", 400)], [], now=NOW).intent
    assert fresh > stale > 0
    assert ancient == 0


def test_low_extraction_confidence_reduces_intent():
    sure = score_company(UAE_FILM, [sig("new_office", 0, 1.0)], [], now=NOW).intent
    unsure = score_company(UAE_FILM, [sig("new_office", 0, 0.5)], [], now=NOW).intent
    assert unsure == pytest.approx(sure / 2)


def test_contact_uses_best_available():
    r = score_company(UAE_FILM, [], ["generic", "verified"], now=NOW)
    assert r.contact == pytest.approx(0.95)


def test_outside_uae_lowers_fit():
    uk = CompanyFacts(segment="film_media", employee_count=85, city="London", country="United Kingdom")
    assert score_company(uk, [], [], now=NOW).fit < score_company(UAE_FILM, [], [], now=NOW).fit


def test_product_lines_follow_signals():
    r = score_company(UAE_FILM, [sig("production_launch", 1)], [], now=NOW)
    assert "Cameras, lenses & AV equipment" in r.product_lines
    assert "Servers, storage & networking" not in r.product_lines


def test_explanation_cites_evidence_url():
    r = score_company(UAE_FILM, [sig("new_office", 1)], [], now=NOW)
    assert any("https://x.example/new_office" in line for line in r.explanation)


@pytest.mark.parametrize("priority,contact,expected", [
    (0.8, 0.95, "high"),
    (0.8, 0.0, "review"),   # great lead, nobody to email yet
    (0.3, 0.95, "review"),
    (0.1, 0.95, "archive"),
])
def test_router(priority, contact, expected):
    assert route(ScoreResult(fit=1, intent=priority, contact=contact, priority=priority))[0] == expected
