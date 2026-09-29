"""Confidence routing: decide what happens to a scored lead.

  high    – strong priority and a usable contact → push to CRM for sales / drafting
  review  – promising but uncertain (or no contact) → a human looks first
  archive – weak; kept, and rescored automatically if new signals arrive
"""

from lead_engine.scoring import ScoreResult
from lead_engine.settings import load_config


def route(score: ScoreResult, cfg: dict | None = None) -> tuple[str, str]:
    """Return (route, reason)."""
    r = (cfg or load_config("scoring"))["routing"]

    if score.priority >= r["high_priority"]:
        if score.contact >= r["min_contact_to_email"]:
            return "high", "strong priority with a usable contact"
        return "review", "strong priority but no reliable contact yet: find one"
    if score.priority >= r["review_priority"]:
        return "review", "moderate priority: needs a human check"
    return "archive", "low priority"
