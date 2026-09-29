"""The pipeline: collect → resolve → store signal → rescore → find contacts → route → export.

Every step is idempotent: re-running on the same input produces no duplicate
signals, companies or CRM leads.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from lead_engine.collectors import COLLECTORS
from lead_engine.contacts import ContactSearch, find_contacts
from lead_engine.models import Company, Contact, Export, Score, Signal
from lead_engine.resolve import normalize_domain, normalize_name, resolve_company
from lead_engine.router import route
from lead_engine.schemas import RawSignal
from lead_engine.scoring import CompanyFacts, SignalFacts, score_company
from lead_engine.settings import load_config
from lead_engine.sinks import LeadContact, LeadPayload, LeadSink

log = logging.getLogger(__name__)

EXPORT_ROUTES = ("high", "review")


@dataclass
class RunStats:
    collected: int = 0
    new_signals: int = 0
    duplicate_signals: int = 0
    rescored: int = 0
    contact_searches: int = 0
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


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def set_domain(session: Session, company: Company, domain: str, source: str) -> str | None:
    """Assign a website. Returns an error message instead if it belongs to another company."""
    domain = normalize_domain(domain)
    if not domain:
        return "not a valid company domain"
    owner = session.scalar(select(Company).where(Company.domain == domain, Company.id != company.id))
    if owner:
        return f"{domain} already belongs to company {owner.id} ({owner.name}): probably the same company"
    company.domain, company.domain_source = domain, source
    session.flush()
    return None


def enrich_contacts(session: Session, company: Company, client: httpx.Client | None = None) -> ContactSearch:
    """Find a website and public emails for one company, store them and rescore."""
    search = find_contacts(company.name, company.domain, client=client)
    if search.domain and not company.domain:
        error = set_domain(session, company, search.domain, search.domain_source or "guessed")
        if error:
            search.notes.append(error)
            search.contacts = []

    known = {c.email for c in company.contacts}
    for c in search.contacts:
        if c.email not in known:
            session.add(Contact(company_id=company.id, **c.model_dump()))
            known.add(c.email)
    company.contact_checked_at = datetime.now(timezone.utc)
    rescore(session, company)
    return search


def leads_needing_contacts(session: Session, recheck_days: int = 30,
                           company_ids: list[int] | None = None) -> list[Company]:
    """Promising leads (high/review) with no usable contact that weren't checked recently. Best first."""
    min_contact = load_config("scoring")["routing"]["min_contact_to_email"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=recheck_days)
    q = select(Company).join(Score).where(Score.route.in_(EXPORT_ROUTES), Score.contact < min_contact)
    if company_ids is not None:
        q = q.where(Company.id.in_(company_ids))
    companies = session.scalars(q.order_by(Score.priority.desc())).all()
    return [c for c in companies if c.contact_checked_at is None or _aware(c.contact_checked_at) < cutoff]


def find_contacts_for_leads(session: Session, limit: int = 20, recheck_days: int = 30,
                            company_ids: list[int] | None = None,
                            client: httpx.Client | None = None) -> list[dict]:
    client = client or httpx.Client()
    report = []
    for company in leads_needing_contacts(session, recheck_days, company_ids)[:limit]:
        try:
            search = enrich_contacts(session, company, client)
            session.commit()
        except Exception as exc:  # one broken website must not stop the batch
            session.rollback()
            log.exception("contact search failed for %s", company.name)
            report.append({"company": company.name, "error": str(exc)})
            continue
        report.append({
            "id": company.id, "company": company.name, "domain": company.domain,
            "emails": [f"{c.email} ({c.kind})" for c in search.contacts],
            "route": company.score.route, "notes": search.notes,
        })
    return report


def run(session: Session, sources: list[str], sink: LeadSink | None = None,
        find_contacts: bool = False) -> RunStats:
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

    if find_contacts and touched:
        stats.contact_searches = len(find_contacts_for_leads(session, limit=len(touched), company_ids=list(touched)))

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
