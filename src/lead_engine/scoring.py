"""Three separate scores, each explainable:

  fit     – is this our kind of customer?          (segment, size, location)
  intent  – is there recent evidence they're buying? (signals × recency decay, noisy-OR)
  contact – can we reach the right person?          (best contact's provenance)

priority = fit × intent. Contact gates whether the lead can be emailed (see router).

Functions here are pure (plain data in, plain data out) so they are easy to test
and later to recalibrate from outcomes.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from lead_engine.settings import load_config


@dataclass
class SignalFacts:
    signal_type: str
    title: str
    url: str
    published_at: datetime
    extraction_confidence: float = 1.0


@dataclass
class CompanyFacts:
    segment: str = "unknown"
    employee_count: int | None = None
    city: str | None = None
    country: str | None = None


@dataclass
class ScoreResult:
    fit: float
    intent: float
    contact: float
    priority: float
    product_lines: list[str] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)


def _aware(dt: datetime) -> datetime:
    # SQLite drops tzinfo; treat naive datetimes as UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def recency_decay(age_days: float, half_life_days: float) -> float:
    return 0.5 ** (max(age_days, 0.0) / half_life_days)


def fit_score(company: CompanyFacts, icp: dict, weights: dict) -> tuple[float, list[str]]:
    seg = icp["segments"].get(company.segment, icp["segments"]["unknown"])

    if company.employee_count is None:
        size = icp["unknown_size_score"]
        size_note = "size unknown"
    else:
        size = next(b["score"] for b in icp["size_bands"] if company.employee_count <= b["max"])
        size_note = f"{company.employee_count} employees"

    targets = {x.lower() for x in icp["target_countries"] + icp["target_cities"]}
    places = {x.lower() for x in (company.city, company.country) if x}
    if places & targets:
        loc, loc_note = 1.0, "in UAE"
    elif places:
        loc, loc_note = 0.2, f"outside UAE ({', '.join(sorted(places))})"
    else:
        loc, loc_note = 0.5, "location unknown"

    fit = weights["segment"] * seg + weights["size"] * size + weights["location"] * loc
    note = f"fit {fit:.2f}: segment={company.segment} ({seg}), {size_note} ({size}), {loc_note} ({loc})"
    return round(fit, 4), [note]


def intent_score(signals: list[SignalFacts], cfg: dict, now: datetime) -> tuple[float, list[str]]:
    """Noisy-OR: P(buying) = 1 - Π(1 - s_i). Several weak signals add up without exceeding 1."""
    weights = cfg["signal_weights"]
    half_life = cfg["recency_half_life_days"]
    max_age = cfg["max_signal_age_days"]

    not_buying = 1.0
    notes = []
    for s in sorted(signals, key=lambda s: _aware(s.published_at), reverse=True):
        age = (now - _aware(s.published_at)).total_seconds() / 86400
        if age > max_age:
            continue
        strength = weights.get(s.signal_type, weights["other"]) * recency_decay(age, half_life) * s.extraction_confidence
        not_buying *= 1.0 - strength
        notes.append(f"+ {s.signal_type} ({age:.0f}d ago, strength {strength:.2f}): {s.title} <{s.url}>")

    intent = 1.0 - not_buying
    return round(intent, 4), [f"intent {intent:.2f} from {len(notes)} recent signal(s)"] + notes


def contact_score(contact_kinds: list[str], cfg: dict) -> tuple[float, list[str]]:
    weights = cfg["contact_weights"]
    best = max((weights.get(k, 0.0) for k in contact_kinds), default=weights["none"])
    kinds = ", ".join(sorted(set(contact_kinds))) or "none"
    return best, [f"contact {best:.2f}: contacts found = {kinds}"]


def product_lines_for(signals: list[SignalFacts], products: dict) -> list[str]:
    types = {s.signal_type for s in signals}
    return [
        line["label"]
        for line in products["product_lines"].values()
        if types & set(line["signals"])
    ]


def score_company(
    company: CompanyFacts,
    signals: list[SignalFacts],
    contact_kinds: list[str],
    now: datetime | None = None,
    scoring_cfg: dict | None = None,
    icp: dict | None = None,
    products: dict | None = None,
) -> ScoreResult:
    now = now or datetime.now(timezone.utc)
    scoring_cfg = scoring_cfg or load_config("scoring")
    icp = icp or load_config("icp")
    products = products or load_config("products")

    fit, fit_notes = fit_score(company, icp, scoring_cfg["fit_weights"])
    intent, intent_notes = intent_score(signals, scoring_cfg, now)
    contact, contact_notes = contact_score(contact_kinds, scoring_cfg)
    priority = round(fit * intent, 4)

    return ScoreResult(
        fit=fit,
        intent=intent,
        contact=contact,
        priority=priority,
        product_lines=product_lines_for(signals, products),
        explanation=[f"priority {priority:.2f} = fit × intent"] + fit_notes + intent_notes + contact_notes,
    )
