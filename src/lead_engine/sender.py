"""Sends approved drafts, with guardrails that protect the sending domain.

Modes (SEND_MODE in .env, or --mode):
  dry_run  write each email to out/outbox/<draft>.eml; nothing leaves the machine (default)
  test     send every email to TEST_INBOX, with the real recipient in the subject
  live     send to the real recipient (needs --confirm-live on the command line)

Live guardrails:
  - suppression list: unsubscribed / bounced addresses and domains are never emailed
  - one email per company per COMPANY_COOLDOWN_DAYS
  - DAILY_SEND_CAP live emails per rolling 24 hours
  - a 'pending' Send row is committed before the SMTP call, so a crash can't cause a double send
"""

import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lead_engine.drafting import sender_details
from lead_engine.models import Draft, Outcome, Send, Suppression
from lead_engine.settings import OUT_DIR, env

log = logging.getLogger(__name__)

MODES = ("dry_run", "test", "live")
COMPANY_COOLDOWN_DAYS = 90


class Transport(Protocol):
    def send(self, msg: EmailMessage) -> None: ...


class SmtpTransport:
    """SMTP over SSL (port 465) or STARTTLS (587). Hostinger: smtp.hostinger.com:465."""

    def __init__(self):
        self.host = env("SMTP_HOST")
        self.port = int(env("SMTP_PORT", "465"))
        self.user = env("SMTP_USER")
        self.password = env("SMTP_PASSWORD")
        if not all([self.host, self.user, self.password]):
            raise RuntimeError("Set SMTP_HOST, SMTP_USER and SMTP_PASSWORD in .env")

    def send(self, msg: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) as smtp:
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls(context=context)
                smtp.login(self.user, self.password)
                smtp.send_message(msg)


class OutboxTransport:
    """dry_run: writes .eml files you can open in any mail client."""

    def __init__(self, out_dir=OUT_DIR / "outbox"):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def send(self, msg: EmailMessage) -> None:
        name = msg["X-Lead-Engine-Draft"]
        (self.out_dir / f"draft-{name}.eml").write_bytes(bytes(msg))


def daily_cap() -> int:
    return int(env("DAILY_SEND_CAP", "20"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_suppressed(session: Session, email: str) -> Suppression | None:
    email = email.lower()
    domain = "@" + email.rsplit("@", 1)[-1]
    return session.scalar(select(Suppression).where(Suppression.email.in_([email, domain])))


def suppress(session: Session, email: str, reason: str) -> bool:
    """Add to the suppression list. Returns False if it was already there."""
    email = email.strip().lower()
    if session.scalar(select(Suppression).where(Suppression.email == email)):
        return False
    session.add(Suppression(email=email, reason=reason))
    session.flush()
    return True


def live_sent_last_24h(session: Session) -> int:
    since = _now() - timedelta(hours=24)
    return session.scalar(select(func.count()).select_from(Send).where(
        Send.mode == "live", Send.status.in_(("pending", "sent")), Send.created_at >= since)) or 0


def recently_emailed(session: Session, company_id: int) -> bool:
    since = _now() - timedelta(days=COMPANY_COOLDOWN_DAYS)
    return session.scalar(select(Send.id).where(
        Send.company_id == company_id, Send.mode == "live", Send.status.in_(("pending", "sent")),
        Send.created_at >= since)) is not None


def build_message(draft: Draft, to_email: str, subject: str, sender: dict) -> EmailMessage:
    from_addr = env("SMTP_USER") or sender["email"]
    domain = from_addr.rsplit("@", 1)[-1]
    msg = EmailMessage()
    msg["From"] = formataddr((sender["name"], from_addr))
    msg["To"] = to_email
    msg["Reply-To"] = sender["email"]
    msg["Subject"] = subject
    # Aware datetime → "+0000". A naive time renders "-0000" ("unknown zone"), which can hurt spam scores.
    msg["Date"] = _now()
    msg["Message-ID"] = make_msgid(idstring=f"draft{draft.id}", domain=domain)
    # One-click unsubscribe support in Gmail/Outlook; replies are handled by check-inbox.
    msg["List-Unsubscribe"] = f"<mailto:{sender['email']}?subject=unsubscribe>"
    msg["X-Lead-Engine-Draft"] = str(draft.id)
    msg.set_content(draft.body)
    return msg


def _check_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if mode == "test" and not env("TEST_INBOX"):
        raise RuntimeError("Set TEST_INBOX in .env for test mode")
    return mode


def _default_transport(mode: str) -> Transport:
    return OutboxTransport() if mode == "dry_run" else SmtpTransport()


def deliver(session: Session, draft: Draft, mode: str, transport: Transport, sender: dict) -> dict:
    """Run every guardrail for one draft, then send it. Returns a report entry.

    result: sent | written (dry run) | skipped | stopped (daily cap) | failed
    """
    base = {"draft_id": draft.id, "company": draft.company.name, "to": draft.to_email, "mode": mode}

    blocked = is_suppressed(session, draft.to_email)
    if blocked:
        return base | {"result": "skipped", "reason": f"suppressed ({blocked.reason}: {blocked.email})"}
    if mode == "live":
        if recently_emailed(session, draft.company_id):
            return base | {"result": "skipped", "reason": f"company emailed in the last {COMPANY_COOLDOWN_DAYS} days"}
        if live_sent_last_24h(session) >= daily_cap():
            return base | {"result": "stopped", "reason": f"daily cap of {daily_cap()} reached"}

    to_email, subject = draft.to_email, draft.subject
    if mode == "test":
        to_email, subject = env("TEST_INBOX"), f"[TEST → {draft.to_email}] {draft.subject}"
    msg = build_message(draft, to_email, subject, sender)

    record = Send(draft_id=draft.id, company_id=draft.company_id, to_email=to_email, mode=mode,
                  message_id=msg["Message-ID"], status="pending")
    session.add(record)
    session.commit()  # the pending row exists before anything leaves the machine

    try:
        transport.send(msg)
    except Exception as exc:
        record.status, record.error = "failed", str(exc)[:500]
        session.commit()
        log.warning("send failed for draft %s: %s", draft.id, exc)
        return base | {"result": "failed", "reason": str(exc)[:200]}

    record.status = "sent"
    if mode == "live":
        draft.status = "sent"
        session.add(Outcome(company_id=draft.company_id, outcome="emailed", note=f"draft {draft.id}"))
    session.commit()
    if mode == "dry_run":
        return base | {"result": "written", "file": f"out/outbox/draft-{draft.id}.eml"}
    return base | {"result": "sent", "delivered_to": to_email}


def send_approved(session: Session, mode: str | None = None, limit: int | None = None,
                  transport: Transport | None = None) -> list[dict]:
    """Batch: send every approved draft, each at most once per mode."""
    mode = _check_mode(mode or env("SEND_MODE", "dry_run"))
    transport = transport or _default_transport(mode)
    sender = sender_details()
    drafts = session.scalars(select(Draft).where(Draft.status == "approved").order_by(Draft.reviewed_at)).all()
    report = []
    for draft in drafts:
        if limit is not None and len([r for r in report if r["result"] in ("sent", "written")]) >= limit:
            break
        if session.scalar(select(Send.id).where(Send.draft_id == draft.id, Send.mode == mode,
                                                Send.status.in_(("pending", "sent")))):
            continue  # already done in this mode
        entry = deliver(session, draft, mode, transport, sender)
        report.append(entry)
        if entry["result"] == "stopped":
            break
    return report


def send_one(session: Session, draft_id: int, mode: str, transport: Transport | None = None) -> dict:
    """Send one specific draft (used by the review UI).

    Test sends work for any pending or approved draft and may be repeated after edits.
    Live sends need an approved draft and pass through every guardrail.
    """
    mode = _check_mode(mode)
    draft = session.get(Draft, draft_id)
    if draft is None:
        raise ValueError(f"no draft with id {draft_id}")
    if mode == "live" and draft.status != "approved":
        raise ValueError(f"draft {draft_id} is {draft.status}; only approved drafts can be sent for real")
    if draft.status not in ("draft", "needs_review", "approved"):
        raise ValueError(f"draft {draft_id} is already {draft.status}")
    return deliver(session, draft, mode, transport or _default_transport(mode), sender_details())


def send_status(session: Session) -> dict:
    by_mode = dict(session.execute(
        select(Send.mode, func.count()).where(Send.status == "sent").group_by(Send.mode)).all())
    return {
        "mode": env("SEND_MODE", "dry_run"),
        "approved_waiting": session.scalar(select(func.count()).select_from(Draft).where(Draft.status == "approved")),
        "live_sent_last_24h": live_sent_last_24h(session),
        "daily_cap": daily_cap(),
        "sent_by_mode": by_mode,
        "failed": session.scalar(select(func.count()).select_from(Send).where(Send.status == "failed")),
        "stuck_pending": session.scalar(select(func.count()).select_from(Send).where(Send.status == "pending")),
        "suppressed": session.scalar(select(func.count()).select_from(Suppression)),
    }
