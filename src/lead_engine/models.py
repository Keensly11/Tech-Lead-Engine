"""Database tables.

company  1─* signal     (evidence that they may be buying)
company  1─* contact    (who we could email, with how we found them)
company  1─1 score      (latest fit/intent/contact scores + explanation)
company  1─* export     (what we pushed to which sink; makes exports idempotent)
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


class Outcome(Base):
    __tablename__ = "outcome"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(30))  # emailed | replied | meeting | won | not_interested | bounced | unsubscribed
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
