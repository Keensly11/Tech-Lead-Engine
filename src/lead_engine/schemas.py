"""Pydantic schemas: the contract between collectors, the LLM and the pipeline.

Every LLM output is parsed into one of these. If it doesn't validate, it's rejected,
never silently trusted.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SignalType = Literal[
    "it_tender",
    "new_office",
    "production_launch",
    "hiring_surge",
    "new_company",
    "funding_round",
    "it_hiring",
    "other",
]

Segment = Literal[
    "tech", "film_media", "corporate", "education", "government", "events", "retail", "other", "unknown"
]

ContactKind = Literal["verified", "pattern", "generic"]

Outcome = Literal["emailed", "replied", "meeting", "won", "not_interested", "bounced", "unsubscribed"]


class RawContact(BaseModel):
    email: str
    kind: ContactKind
    name: str | None = None
    role: str | None = None
    source_url: str | None = None

    @field_validator("email")
    @classmethod
    def looks_like_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError(f"not an email: {v!r}")
        return v


class RawSignal(BaseModel):
    """One piece of evidence about one company, as produced by a collector."""

    company_name: str = Field(min_length=2)
    company_domain: str | None = None
    segment: Segment = "unknown"
    city: str | None = None
    country: str | None = None
    employee_count: int | None = None
    signal_type: SignalType
    title: str
    url: str
    source: str
    evidence: str | None = None  # the sentence that justifies the signal
    extraction_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    published_at: datetime
    contacts: list[RawContact] = []


class NewsExtraction(BaseModel):
    """What the LLM must return for a news item. Validated strictly."""

    is_relevant: bool = Field(description="True only if a specific company is doing something that implies buying IT/AV equipment")
    company_name: str | None = None
    company_domain: str | None = None
    segment: Segment = "unknown"
    city: str | None = None
    country: str | None = None
    signal_type: SignalType = "other"
    evidence_quote: str | None = Field(default=None, description="Exact sentence from the text supporting the signal")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
