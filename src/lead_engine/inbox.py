"""Reads the sales inbox (read-only) to record replies, unsubscribes and bounces.

- A reply is matched to our email through In-Reply-To / References (our Message-ID),
  falling back to the sender's address.
- "unsubscribe" in a reply's subject or first lines → suppression list + outcome.
- Bounces (mailer-daemon / delivery reports) → the failed address is suppressed.
- Out-of-office auto-replies are recorded but not counted as replies.

The mailbox is opened read-only and bodies are fetched with BODY.PEEK, so nothing
is marked as read. Each inbound Message-ID is processed once.
"""

import email
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from lead_engine.models import InboundMessage, Outcome, Send
from lead_engine.sender import suppress
from lead_engine.settings import env

log = logging.getLogger(__name__)

MSGID_RE = re.compile(r"<[^<>\s]+>")
BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery")
FAILED_RECIPIENT_RE = re.compile(r"(?:Final|Original)-Recipient:\s*rfc822;\s*([^\s>]+)", re.I)


def fetch_messages(since_days: int = 14) -> list[Message]:
    host = env("IMAP_HOST")
    user, password = env("SMTP_USER"), env("SMTP_PASSWORD")
    if not all([host, user, password]):
        raise RuntimeError("Set IMAP_HOST, SMTP_USER and SMTP_PASSWORD in .env")
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(host, int(env("IMAP_PORT", "993"))) as imap:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "SINCE", since)
        messages = []
        for num in data[0].split():
            _, parts = imap.fetch(num, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                messages.append(email.message_from_bytes(raw))
        return messages


def _text(msg: Message, limit: int = 20000) -> str:
    """All text/plain parts (and delivery-status parts, for bounces)."""
    chunks = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "message/delivery-status", "text/rfc822-headers"):
            payload = part.get_payload(decode=True)
            if payload is None and part.is_multipart():
                payload = "".join(str(p) for p in part.get_payload()).encode()
            if payload:
                chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(chunks)[:limit]


def _is_auto_reply(msg: Message) -> bool:
    auto = (msg.get("Auto-Submitted") or "no").lower()
    return auto != "no" or msg.get("X-Autoreply") is not None or msg.get("X-Autorespond") is not None \
        or bool(re.match(r"(automatic reply|auto[- ]?reply|out of (the )?office)", msg.get("Subject", ""), re.I))


def _is_bounce(msg: Message) -> bool:
    sender = (msg.get("From") or "").lower()
    return msg.get_content_type() == "multipart/report" or any(s in sender for s in BOUNCE_SENDERS)


# The email's P.S. says "if this isn't relevant, just let us know and we won't reach
# out again", so replies like these are opt-outs, not just "unsubscribe".
OPT_OUT_RE = re.compile(
    r"\b(unsubscribe|remove (me|us)|take (me|us) off|stop (emailing|contacting)|opt[- ]?out|"
    r"not relevant|not interested|no thanks|no, thanks|not for us|don'?t (contact|email|reach out)|"
    r"do not (contact|email|reach out))\b",
    re.I,
)


UNSUBSCRIBE_RE = re.compile(r"\b(unsubscribe|remove|take (me|us) off|stop|opt[- ]?out|contact|email|reach out)\b", re.I)


def _reply_text(msg: Message) -> str:
    """The first few lines the person actually wrote: quoted history ('> …') is skipped."""
    lines = [l for l in _text(msg).strip().splitlines() if l.strip() and not l.lstrip().startswith(">")]
    return "\n".join(lines[:5])


def _asks_to_unsubscribe(msg: Message) -> bool:
    return OPT_OUT_RE.search(msg.get("Subject") or "") is not None or OPT_OUT_RE.search(_reply_text(msg)) is not None


def _opt_out_outcome(msg: Message) -> str:
    """'unsubscribed' for explicit removal requests, 'not_interested' for a polite no. Both are suppressed."""
    text = f"{msg.get('Subject', '')}\n{_reply_text(msg)}"
    match = OPT_OUT_RE.search(text)
    return "unsubscribed" if match and UNSUBSCRIBE_RE.search(match.group(0)) else "not_interested"


def classify(session: Session, msg: Message) -> tuple[str, Send | None]:
    """Return (kind, the Send it relates to)."""
    ours = {s.message_id: s for s in session.scalars(select(Send).where(Send.status == "sent"))}
    refs = MSGID_RE.findall(f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}")

    if _is_bounce(msg):
        body = _text(msg)
        send = next((ours[m] for m in MSGID_RE.findall(body) if m in ours), None)
        if send is None:
            failed = {a.lower() for a in FAILED_RECIPIENT_RE.findall(body)}
            failed |= {a.strip().lower() for a in (msg.get("X-Failed-Recipients") or "").split(",") if a.strip()}
            send = next((s for s in ours.values() if s.to_email.lower() in failed), None)
        return ("bounce", send) if send else ("unrelated", None)

    send = next((ours[r] for r in refs if r in ours), None)
    if send is None:
        # Fallback for clients that drop threading headers. Live sends only: in test
        # mode the recipient is our own inbox, so every message from it would match.
        from_addr = parseaddr(msg.get("From", ""))[1].lower()
        send = next((s for s in ours.values() if s.mode == "live" and s.to_email.lower() == from_addr), None)
    if send is None:
        return "unrelated", None
    if _is_auto_reply(msg):
        return "auto_reply", send
    if _asks_to_unsubscribe(msg):
        return "unsubscribe", send
    return "reply", send


def process_messages(session: Session, messages: list[Message]) -> list[dict]:
    report = []
    for msg in messages:
        message_id = (msg.get("Message-ID") or "").strip()
        if not message_id or session.scalar(select(InboundMessage.id).where(InboundMessage.message_id == message_id)):
            continue

        kind, send = classify(session, msg)
        session.add(InboundMessage(message_id=message_id, kind=kind, company_id=send.company_id if send else None))
        if send is None:
            session.commit()
            continue

        # In test mode the "recipient" is our own test inbox; never suppress that.
        recipient = send.to_email if send.mode == "live" else None
        if kind == "bounce":
            if recipient:
                suppress(session, recipient, "bounced")
            session.add(Outcome(company_id=send.company_id, outcome="bounced", note=f"draft {send.draft_id}"))
        elif kind == "unsubscribe":
            outcome = _opt_out_outcome(msg)
            if recipient:
                suppress(session, recipient, outcome)
            session.add(Outcome(company_id=send.company_id, outcome=outcome, note=f"draft {send.draft_id}"))
        elif kind == "reply":
            session.add(Outcome(company_id=send.company_id, outcome="replied",
                                note=f"draft {send.draft_id}: {msg.get('Subject', '')[:100]}"))
        session.commit()
        report.append({"kind": kind, "company_id": send.company_id, "draft_id": send.draft_id,
                       "from": msg.get("From"), "subject": msg.get("Subject"), "mode": send.mode})
    return report


def check_inbox(session: Session, since_days: int = 14) -> list[dict]:
    return process_messages(session, fetch_messages(since_days))
