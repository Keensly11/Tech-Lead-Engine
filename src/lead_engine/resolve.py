"""Entity resolution: make sure one real company is one database row.

The same company shows up as "Acme Tech FZ-LLC", "ACME Technologies" and acme.ae
across different sources. Match on domain first (reliable), then on fuzzy name.
"""

import re
from urllib.parse import urlparse

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from lead_engine.models import Company
from lead_engine.schemas import RawSignal

NAME_MATCH_THRESHOLD = 92

# UAE and generic legal suffixes that carry no identity.
_LEGAL_SUFFIXES = {
    "llc", "l.l.c", "fz", "fze", "fzco", "fzc", "fz-llc", "fzllc", "dmcc", "ltd", "limited",
    "inc", "co", "company", "est", "establishment", "plc", "pjsc", "psc", "llp", "gmbh", "sole", "proprietorship",
}

_GENERIC_EMAIL_PROVIDERS = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com", "live.com"}


def normalize_domain(value: str | None) -> str | None:
    """'https://www.Acme.ae/about' → 'acme.ae'. Free email providers aren't company domains."""
    if not value:
        return None
    value = value.strip().lower()
    if "@" in value:
        value = value.split("@", 1)[1]
    if "://" not in value:
        value = "http://" + value
    host = urlparse(value).hostname or ""
    host = host.removeprefix("www.")
    if not host or "." not in host or host in _GENERIC_EMAIL_PROVIDERS:
        return None
    return host


def normalize_name(name: str) -> str:
    """'ACME Technologies FZ-LLC.' → 'acme technologies'."""
    name = name.lower().replace("&", " and ")
    tokens = re.findall(r"[a-z0-9][a-z0-9.\-]*", name)
    tokens = [t.strip(".-") for t in tokens]
    tokens = [t for t in tokens if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)


def names_match(a: str, b: str) -> bool:
    return fuzz.token_sort_ratio(normalize_name(a), normalize_name(b)) >= NAME_MATCH_THRESHOLD


def resolve_company(session: Session, raw: RawSignal) -> Company:
    """Find the existing company for this signal, or create it. Fills in missing fields."""
    domain = normalize_domain(raw.company_domain)
    norm = normalize_name(raw.company_name)

    company = None
    if domain:
        company = session.scalar(select(Company).where(Company.domain == domain))
    if company is None:
        # Exact normalized match is cheap; fuzzy scan is the fallback.
        company = session.scalar(select(Company).where(Company.normalized_name == norm))
    if company is None:
        for candidate in session.scalars(select(Company)):
            # Two different domains means two different companies, however similar the names.
            if domain and candidate.domain and candidate.domain != domain:
                continue
            if names_match(candidate.name, raw.company_name):
                company = candidate
                break

    if company is None:
        company = Company(name=raw.company_name.strip(), normalized_name=norm, domain=domain)
        session.add(company)

    # Enrich blanks only: never overwrite known data with a weaker source.
    if domain and not company.domain:
        company.domain = domain
    if raw.segment != "unknown" and company.segment in (None, "unknown"):
        company.segment = raw.segment
    company.city = company.city or raw.city
    company.country = company.country or raw.country
    company.employee_count = company.employee_count or raw.employee_count
    session.flush()
    return company
