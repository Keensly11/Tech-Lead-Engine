"""Sender guardrails and inbox processing, with a fake transport (no SMTP/IMAP)."""

from datetime import datetime, timezone
from email.message import EmailMessage

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lead_engine import drafting, inbox, pipeline, sender
from lead_engine.models import Base, Company, Draft, Outcome, Send, Suppression
from lead_engine.schemas import DraftCopy
from lead_engine.sender import OutboxTransport, send_approved

SENDER = {"name": "Gulf Tech Supply", "website": "https://gulftech.example", "phone": "+971 4 123 4567",
          "email": "sales@gulftech.example", "signature": "Gulf Tech Supply Sales"}


class FakeTransport:
    def __init__(self, fail: bool = False):
        self.sent: list[EmailMessage] = []
        self.fail = fail

    def send(self, msg):
        if self.fail:
            raise OSError("SMTP connection refused")
        self.sent.append(msg)


def fake_llm(prompt, model, client):
    return DraftCopy(opener="Congratulations on the news.", subject="Your news", product_focus=[])


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(drafting, "sender_details", lambda: SENDER)
    monkeypatch.setattr(sender, "sender_details", lambda: SENDER)
    for k, v in {"SMTP_USER": "sales@gulftech.example", "TEST_INBOX": "me@gulftech.example",
                 "DAILY_SEND_CAP": "20", "SEND_MODE": "dry_run"}.items():
        monkeypatch.setenv(k, v)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        pipeline.run(s, ["sample"])
        for r in drafting.draft_leads(s, llm=fake_llm):  # Falcon, Northwind, Duneline
            drafting.approve_draft(s, r["draft_id"])
        yield s


def approved(s) -> list[Draft]:
    return s.scalars(select(Draft).where(Draft.status == "approved").order_by(Draft.id)).all()


# ---------- modes ----------

def test_dry_run_writes_eml_and_sends_nothing(session, tmp_path):
    report = send_approved(session, "dry_run", transport=OutboxTransport(tmp_path))
    assert [r["result"] for r in report] == ["written"] * 3
    assert len(list(tmp_path.glob("*.eml"))) == 3
    assert len(approved(session)) == 3  # still waiting for a real send
    # each draft is written once per mode
    assert send_approved(session, "dry_run", transport=OutboxTransport(tmp_path)) == []


def test_test_mode_redirects_to_test_inbox(session):
    t = FakeTransport()
    send_approved(session, "test", transport=t)
    assert {m["To"] for m in t.sent} == {"me@gulftech.example"}
    assert all(m["Subject"].startswith("[TEST → ") for m in t.sent)
    assert len(approved(session)) == 3


def test_live_send_headers_and_bookkeeping(session):
    t = FakeTransport()
    report = send_approved(session, "live", transport=t)
    assert [r["result"] for r in report] == ["sent"] * 3

    msg = t.sent[0]
    assert msg["From"] == "Gulf Tech Supply <sales@gulftech.example>"
    assert msg["List-Unsubscribe"] == "<mailto:sales@gulftech.example?subject=unsubscribe>"
    assert msg["Message-ID"].endswith("@gulftech.example>")
    assert msg["Date"].endswith("+0000")

    assert approved(session) == []
    assert {d.status for d in session.scalars(select(Draft))} == {"sent"}
    assert session.scalar(select(Send).where(Send.message_id == msg["Message-ID"])).status == "sent"
    assert len(session.scalars(select(Outcome).where(Outcome.outcome == "emailed")).all()) == 3


# ---------- guardrails ----------

def test_suppressed_address_and_domain_are_skipped(session):
    first, second, _ = approved(session)
    sender.suppress(session, first.to_email, "unsubscribed")
    sender.suppress(session, "@" + second.to_email.split("@")[1], "manual")
    session.commit()

    t = FakeTransport()
    report = send_approved(session, "live", transport=t)
    assert [r["result"] for r in report] == ["skipped", "skipped", "sent"]
    assert "suppressed" in report[0]["reason"] and "@" in report[1]["reason"]


def test_daily_cap(session, monkeypatch):
    monkeypatch.setenv("DAILY_SEND_CAP", "2")
    report = send_approved(session, "live", transport=FakeTransport())
    assert [r["result"] for r in report] == ["sent", "sent", "stopped"]
    assert len(approved(session)) == 1


def test_company_cooldown_blocks_second_email(session):
    send_approved(session, "live", transport=FakeTransport())
    company = session.scalar(select(Company).where(Company.domain == "falconpixel.example"))
    extra = Draft(company_id=company.id, to_email="other@falconpixel.example", subject="s", body="b unsubscribe",
                  signal_url="u", status="approved", issues=[], reviewed_at=datetime.now(timezone.utc))
    session.add(extra)
    session.commit()
    report = send_approved(session, "live", transport=FakeTransport())
    assert report[0]["result"] == "skipped" and "90 days" in report[0]["reason"]


def test_failed_send_is_recorded_and_retryable(session):
    report = send_approved(session, "live", transport=FakeTransport(fail=True))
    assert {r["result"] for r in report} == {"failed"}
    assert len(approved(session)) == 3
    assert send_approved(session, "live", transport=FakeTransport())[0]["result"] == "sent"


def test_pending_row_from_a_crash_blocks_resend(session):
    d = approved(session)[0]
    session.add(Send(draft_id=d.id, company_id=d.company_id, to_email=d.to_email, mode="live",
                     message_id="<crashed@x>", status="pending"))
    session.commit()
    report = send_approved(session, "live", transport=FakeTransport())
    assert report[0]["draft_id"] != d.id  # never retried automatically
    assert d.id not in {r["draft_id"] for r in report}


# ---------- inbox ----------

def _live_send(session) -> tuple[FakeTransport, Send]:
    t = FakeTransport()
    send_approved(session, "live", transport=t, limit=1)
    return t, session.scalar(select(Send).where(Send.mode == "live"))


def _reply(to_msg_id: str, from_addr: str, subject="Re: Your news", body="Thanks, please send a quote.",
           msg_id="<r1@client>", **headers) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"], m["In-Reply-To"] = from_addr, subject, msg_id, to_msg_id
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m


def test_reply_is_recorded_once(session):
    _, s = _live_send(session)
    msg = _reply(s.message_id, s.to_email)
    assert inbox.process_messages(session, [msg])[0]["kind"] == "reply"
    assert inbox.process_messages(session, [msg]) == []  # idempotent
    assert session.scalar(select(Outcome).where(Outcome.outcome == "replied")).company_id == s.company_id


def test_unsubscribe_reply_suppresses_address(session):
    _, s = _live_send(session)
    inbox.process_messages(session, [_reply(s.message_id, s.to_email, body="Unsubscribe me please.")])
    assert session.scalar(select(Suppression)).email == s.to_email
    assert sender.is_suppressed(session, s.to_email)


@pytest.mark.parametrize("body,outcome", [
    ("Not relevant for us, thanks.", "not_interested"),
    ("No thanks.", "not_interested"),
    ("Please remove us from your list.", "unsubscribed"),
    ("Please don't contact us again.", "unsubscribed"),
])
def test_plain_language_opt_outs_are_suppressed(session, body, outcome):
    _, s = _live_send(session)
    report = inbox.process_messages(session, [_reply(s.message_id, s.to_email, body=body)])
    assert report[0]["kind"] == "unsubscribe"
    assert session.scalar(select(Suppression)).reason == outcome
    assert session.scalar(select(Outcome).where(Outcome.outcome == outcome)) is not None


def test_quoted_original_email_does_not_count_as_opt_out(session):
    _, s = _live_send(session)
    body = ("Yes please, send over some options.\n\nOn Wed, Fosutog wrote:\n"
            "> Hi there,\n> P.S. If this isn't relevant for you, just let us know.\n> not interested? no thanks")
    report = inbox.process_messages(session, [_reply(s.message_id, s.to_email, body=body)])
    assert report[0]["kind"] == "reply"
    assert session.scalar(select(Suppression)) is None


def test_auto_reply_is_not_a_reply(session):
    _, s = _live_send(session)
    report = inbox.process_messages(session, [_reply(s.message_id, s.to_email, subject="Out of office",
                                                     Auto_Submitted="auto-replied")])
    assert report[0]["kind"] == "auto_reply"
    assert session.scalar(select(Outcome).where(Outcome.outcome == "replied")) is None


def test_bounce_suppresses_failed_recipient(session):
    _, s = _live_send(session)
    bounce = EmailMessage()
    bounce["From"] = "Mail Delivery System <MAILER-DAEMON@smtp.hostinger.com>"
    bounce["Subject"] = "Undelivered Mail Returned to Sender"
    bounce["Message-ID"] = "<b1@hostinger>"
    bounce.set_content(f"Delivery failed.\nFinal-Recipient: rfc822; {s.to_email}\nStatus: 5.1.1")
    assert inbox.process_messages(session, [bounce])[0]["kind"] == "bounce"
    assert session.scalar(select(Suppression)).reason == "bounced"


def test_test_mode_reply_never_suppresses_test_inbox(session):
    t = FakeTransport()
    send_approved(session, "test", transport=t, limit=1)
    s = session.scalar(select(Send).where(Send.mode == "test"))
    inbox.process_messages(session, [_reply(s.message_id, "me@gulftech.example", body="unsubscribe")])
    assert session.scalar(select(Suppression)) is None


def test_unrelated_mail_is_ignored(session):
    _live_send(session)
    report = inbox.process_messages(session, [_reply("<someone-else@x>", "stranger@else.example")])
    assert report == []
