"""Read/write helpers shared by the CLI and the MCP server."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lead_engine.models import Company, Export, Outcome, Score, Signal


def search_leads(session: Session, segment: str | None = None, route: str | None = None,
                 min_priority: float = 0.0, limit: int = 20) -> list[dict]:
    q = (select(Company, Score).join(Score).where(Score.priority >= min_priority)
         .order_by(Score.priority.desc()).limit(limit))
    if segment:
        q = q.where(Company.segment == segment)
    if route:
        q = q.where(Score.route == route)
    return [
        {
            "id": c.id, "company": c.name, "domain": c.domain, "segment": c.segment, "city": c.city,
            "route": s.route, "priority": s.priority, "fit": s.fit, "intent": s.intent,
            "contact": s.contact, "product_lines": s.product_lines,
        }
        for c, s in session.execute(q)
    ]


def get_lead(session: Session, company_id: int) -> dict | None:
    c = session.get(Company, company_id)
    if c is None:
        return None
    outcomes = session.scalars(select(Outcome).where(Outcome.company_id == c.id).order_by(Outcome.created_at))
    return {
        "id": c.id, "company": c.name, "domain": c.domain, "segment": c.segment, "city": c.city,
        "country": c.country, "employee_count": c.employee_count,
        "score": None if c.score is None else {
            "route": c.score.route, "priority": c.score.priority, "fit": c.score.fit,
            "intent": c.score.intent, "contact": c.score.contact,
            "product_lines": c.score.product_lines, "explanation": c.score.explanation,
        },
        "signals": [
            {"type": s.signal_type, "title": s.title, "url": s.url, "evidence": s.evidence,
             "published_at": s.published_at.isoformat(), "source": s.source}
            for s in c.signals
        ],
        "contacts": [{"email": x.email, "kind": x.kind, "name": x.name, "role": x.role} for x in c.contacts],
        "outcomes": [{"outcome": o.outcome, "note": o.note, "at": o.created_at.isoformat()} for o in outcomes],
    }


def log_outcome(session: Session, company_id: int, outcome: str, note: str | None = None) -> None:
    if session.get(Company, company_id) is None:
        raise ValueError(f"no company with id {company_id}")
    session.add(Outcome(company_id=company_id, outcome=outcome, note=note))
    session.commit()


def pipeline_status(session: Session) -> dict:
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    routes = dict(session.execute(select(Score.route, func.count()).group_by(Score.route)).all())
    outcomes = dict(session.execute(select(Outcome.outcome, func.count()).group_by(Outcome.outcome)).all())
    return {
        "companies": session.scalar(select(func.count()).select_from(Company)),
        "signals": session.scalar(select(func.count()).select_from(Signal)),
        "signals_last_7_days": session.scalar(select(func.count()).select_from(Signal).where(Signal.created_at >= week_ago)),
        "leads_by_route": routes,
        "exports": session.scalar(select(func.count()).select_from(Export)),
        "outcomes": outcomes,
    }
