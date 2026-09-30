"""Database tables.

company  1─* signal     (evidence that they may be buying)
company  1─* contact    (who we could email, with how we found them)
company  1─1 score      (latest fit/intent/contact scores + explanation)
company  1─* export     (what we pushed to which sink; makes exports idempotent)
company  1─* draft      (outreach emails waiting for human approval)
draft    1─* send       (every send attempt: dry run, test or live)
suppression             (addresses/domains never to email: unsubscribed, bounced)
inbound_message         (inbox messages already processed for replies/bounces)
company  1─* outcome    (what happened: replied, meeting, bounced… → feedback loop)
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    segment: Mapped[str] = mapped_column(String(50), default="unknown")
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    employee_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domain_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # signal | guessed | manual
    contact_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    signals: Mapped[list["Signal"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    contacts: Mapped[list["Contact"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    score: Mapped["Score | None"] = relationship(back_populates="company", uselist=False, cascade="all, delete-orphan")


class Signal(Base):
    __tablename__ = "signal"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    signal_type: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(100))
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # sha256 of (url, normalized company name): the same article is never ingested twice
    dedup_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[Company] = relationship(back_populates="signals")


class Contact(Base):
    __tablename__ = "contact"
    __table_args__ = (UniqueConstraint("company_id", "email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    email: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # verified | pattern | generic
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    company: Mapped[Company] = relationship(back_populates="contacts")


class Score(Base):
    __tablename__ = "score"

    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), primary_key=True)
    fit: Mapped[float] = mapped_column(Float)
    intent: Mapped[float] = mapped_column(Float)
    contact: Mapped[float] = mapped_column(Float)
    priority: Mapped[float] = mapped_column(Float, index=True)
    route: Mapped[str] = mapped_column(String(20), index=True)  # high | review | archive
    product_lines: Mapped[list] = mapped_column(JSON, default=list)
    explanation: Mapped[list] = mapped_column(JSON, default=list)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[Company] = relationship(back_populates="score")


class Export(Base):
    __tablename__ = "export"
    __table_args__ = (UniqueConstraint("company_id", "sink"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"))
    sink: Mapped[str] = mapped_column(String(50))
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Hash of the payload last pushed. Re-push only when it changes.
    payload_hash: Mapped[str] = mapped_column(String(64))
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Draft(Base):
    """An outreach email awaiting human review. Nothing is ever sent from here.

    status: draft (passed checks) | needs_review (fact check flagged something)
            | approved | rejected | sent (delivered to the real recipient)
    """

    __tablename__ = "draft"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    to_email: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    signal_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), index=True)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    reviewer_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    company: Mapped[Company] = relationship()


class Send(Base):
    """One attempt to send a draft. Written *before* the SMTP call, so a crash
    mid-send leaves a 'pending' row that blocks an automatic resend."""

    __tablename__ = "send"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("draft.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    to_email: Mapped[str] = mapped_column(String(255))  # where it actually went (the test inbox in test mode)
    mode: Mapped[str] = mapped_column(String(10))  # dry_run | test | live
    message_id: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(10))  # pending | sent | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Suppression(Base):
    """Addresses (or whole domains, as '@domain.com') we must never email."""

    __tablename__ = "suppression"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    reason: Mapped[str] = mapped_column(String(30))  # unsubscribed | not_interested | bounced | manual
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InboundMessage(Base):
    """Inbox messages already processed, so re-reading the inbox is idempotent."""

    __tablename__ = "inbound_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), unique=True)
    kind: Mapped[str] = mapped_column(String(20))  # reply | unsubscribe | bounce | auto_reply | unrelated
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id"), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Outcome(Base):
    __tablename__ = "outcome"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(30))  # emailed | replied | meeting | won | not_interested | bounced | unsubscribed
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
