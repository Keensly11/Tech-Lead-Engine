"""The pipeline: collect → resolve → store signal → rescore → route → export.

Every step is idempotent: re-running on the same input produces no duplicate
signals, companies or CRM leads.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from lead_engine.collectors import COLLECTORS
from lead_engine.models import Company, Contact, Export, Score, Signal
from lead_engine.resolve import normalize_name, resolve_company
from lead_engine.router import route
from lead_engine.schemas import RawSignal
from lead_engine.scoring import CompanyFacts, SignalFacts, score_company
from lead_engine.sinks import LeadContact, LeadPayload, LeadSink

log = logging.getLogger(__name__)

EXPORT_ROUTES = ("high", "review")


@dataclass
class RunStats:
    collected: int = 0
    new_signals: int = 0
    duplicate_signals: int = 0
    rescored: int = 0
    exported: int = 0
    unchanged: int = 0
    failed_exports: list[str] = field(default_factory=list)


def signal_dedup_key(raw: RawSignal) -> str:
    return hashlib.sha256(f"{raw.url}|{normalize_name(raw.company_name)}".encode()).hexdigest()


def ingest(session: Session, raw: RawSignal) -> tuple[Company, bool]:
    """Store one signal. Returns (company, was_new)."""
    company = resolve_company(session, raw)

    for c in raw.contacts:
        exists = session.scalar(select(Contact).where(Contact.company_id == company.id, Contact.email == c.email))
        if not exists:
            session.add(Contact(company_id=company.id, **c.model_dump()))

    key = signal_dedup_key(raw)
    if session.scalar(select(Signal.id).where(Signal.dedup_key == key)):
        return company, False

    session.add(Signal(
        company_id=company.id,
        signal_type=raw.signal_type,
        title=raw.title,
        url=raw.url,
        source=raw.source,
        evidence=raw.evidence,
        extraction_confidence=raw.extraction_confidence,
        published_at=raw.published_at,
        dedup_key=key,
    ))
    session.flush()
    return company, True


def rescore(session: Session, company: Company, now: datetime | None = None) -> Score:
    session.flush()
    session.refresh(company)  # pick up signals/contacts added this run
    result = score_company(
        CompanyFacts(company.segment, company.employee_count, company.city, company.country),
        [SignalFacts(s.signal_type, s.title, s.url, s.published_at, s.extraction_confidence) for s in company.signals],
        [c.kind for c in company.contacts],
        now=now,
    )
    lead_route, reason = route(result)

    score = company.score
    if score is None:
        score = Score()
        company.score = score  # set via the relationship so company.score is populated immediately
    score.fit, score.intent, score.contact, score.priority = result.fit, result.intent, result.contact, result.priority
    score.route = lead_route
    score.product_lines = result.product_lines
    score.explanation = [f"route: {lead_route} ({reason})"] + result.explanation
    score.scored_at = datetime.now(timezone.utc)
    session.flush()
    return score


def build_payload(company: Company) -> LeadPayload:
    s = company.score
    return LeadPayload(
        company_name=company.name,
        domain=company.domain,
        segment=company.segment,
        city=company.city,
        route=s.route,
        fit=s.fit,
        intent=s.intent,
        contact=s.contact,
        priority=s.priority,
        product_lines=list(s.product_lines),
        evidence_urls=[sig.url for sig in company.signals],
        contacts=[LeadContact(c.email, c.kind, c.name, c.role) for c in company.contacts],
        explanation=list(s.explanation),
    )


def export(session: Session, sink: LeadSink, company: Company) -> str:
    """Push to the sink only if new or changed. Returns 'exported' | 'unchanged' | 'skipped'."""
    payload = build_payload(company)
    digest = payload.content_hash()
    record = session.scalar(select(Export).where(Export.company_id == company.id, Export.sink == sink.name))

    if record and (record.payload_hash == digest or not sink.supports_updates):
        return "unchanged"

    ref = sink.upsert_lead(payload)
    if record is None:
        record = Export(company_id=company.id, sink=sink.name, payload_hash=digest)
        session.add(record)
    record.payload_hash = digest
    record.external_ref = ref
    record.exported_at = datetime.now(timezone.utc)
    session.flush()
    return "exported"


def run(session: Session, sources: list[str], sink: LeadSink | None = None) -> RunStats:
    stats = RunStats()
    touched: dict[int, Company] = {}

    for source in sources:
        collector = COLLECTORS[source]()
        if hasattr(collector, "skip_url"):
            collector.skip_url = lambda url: session.scalar(select(Signal.id).where(Signal.url == url)) is not None
        for raw in collector.collect():
            stats.collected += 1
            company, is_new = ingest(session, raw)
            touched[company.id] = company
            if is_new:
                stats.new_signals += 1
            else:
                stats.duplicate_signals += 1
        session.commit()  # checkpoint per source

    for company in touched.values():
        rescore(session, company)
        stats.rescored += 1
    session.commit()

    if sink is not None:
        for company in touched.values():
            if company.score.route not in EXPORT_ROUTES:
                continue
            try:
                outcome = export(session, sink, company)
                session.commit()
            except Exception as exc:  # one bad lead must not stop the run
                session.rollback()
                log.exception("export failed for %s", company.name)
                stats.failed_exports.append(f"{company.name}: {exc}")
                continue
            if outcome == "exported":
                stats.exported += 1
            else:
                stats.unchanged += 1
    return stats


def rescore_all(session: Session) -> int:
    """Recency decay means scores drift daily; run this on a schedule."""
    companies = list(session.scalars(select(Company)))
    for c in companies:
        rescore(session, c)
    session.commit()
    return len(companies)
