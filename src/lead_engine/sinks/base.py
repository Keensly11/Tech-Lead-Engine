"""Where qualified leads are delivered. Swappable via LEAD_SINK in .env."""

import hashlib
import html
import json
from dataclasses import asdict, dataclass, field
from typing import Protocol

# Most to least trustworthy contact provenance.
KIND_ORDER = {"verified": 0, "pattern": 1, "generic": 2}


@dataclass
class LeadContact:
    email: str
    kind: str
    name: str | None = None
    role: str | None = None


@dataclass
class LeadPayload:
    company_name: str
    domain: str | None
    segment: str
    city: str | None
    route: str
    fit: float
    intent: float
    contact: float
    priority: float
    product_lines: list[str] = field(default_factory=list)
    evidence_urls: list[str] = field(default_factory=list)
    contacts: list[LeadContact] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.domain or self.company_name.lower()

    @property
    def best_contact(self) -> LeadContact | None:
        return min(self.contacts, key=lambda c: KIND_ORDER.get(c.kind, 9), default=None)

    def content_hash(self) -> str:
        """Hash of what matters for re-export. Excludes the explanation, whose
        "N days ago" text changes daily and would cause a pointless re-push."""
        d = asdict(self)
        d.pop("explanation")
        for k in ("fit", "intent", "contact", "priority"):
            d[k] = round(d[k], 2)
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()

    def render_text(self) -> str:
        lines = [
            f"Company: {self.company_name}",
            f"Website: {self.domain or 'unknown'}",
            f"Segment: {self.segment} | City: {self.city or 'unknown'}",
            f"Route: {self.route.upper()} | Priority {self.priority:.2f} (fit {self.fit:.2f} × intent {self.intent:.2f}) | Contact confidence {self.contact:.2f}",
            f"Lead with: {', '.join(self.product_lines) or 'general IT equipment'}",
            "",
            "Contacts:",
            *[f"  - {c.email} ({c.kind}){' - ' + c.name if c.name else ''}{', ' + c.role if c.role else ''}" for c in self.contacts],
            "",
            "Why this lead:",
            *[f"  {e}" for e in self.explanation],
        ]
        return "\n".join(lines)

    def render_html(self) -> str:
        return "<pre>" + html.escape(self.render_text()) + "</pre>"


class LeadSink(Protocol):
    name: str
    # False for sinks that can't update an existing record (e.g. email alias):
    # those leads are exported once and never re-sent.
    supports_updates: bool

    def upsert_lead(self, lead: LeadPayload) -> str:
        """Create or update the lead. Returns the external reference."""
        ...
