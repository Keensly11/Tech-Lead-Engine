"""Review page, with an in-memory DB and a fake transport (nothing is sent)."""

import re

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lead_engine import drafting, pipeline, sender
from lead_engine.models import Base, Draft, Send
from lead_engine.schemas import DraftCopy
from lead_engine.ui.app import create_app

SENDER = {"name": "Gulf Tech Supply", "website": "https://gulftech.example", "phone": "+971 4 123 4567",
          "email": "sales@gulftech.example", "signature": "Gulf Tech Supply Sales"}


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(drafting, "sender_details", lambda: SENDER)
    monkeypatch.setattr(sender, "sender_details", lambda: SENDER)
    for k, v in {"SMTP_USER": "sales@gulftech.example", "TEST_INBOX": "me@gulftech.example",
                 "DAILY_SEND_CAP": "20"}.items():
        monkeypatch.setenv(k, v)
    # One shared in-memory connection, usable from Flask's request thread.
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        pipeline.run(s, ["sample"])
        drafting.draft_leads(s, llm=lambda p, m, c: DraftCopy(opener="Saw the news.", subject="your news",
                                                              product_focus=[]))
    transport = FakeTransport()
    app = create_app(factory, transport)
    app.config["TESTING"] = True
    client = app.test_client()
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/").get_data(as_text=True)).group(1)
    return client, factory, transport, token


def first_draft(factory, status=None) -> Draft:
    with factory() as s:
        q = select(Draft).order_by(Draft.id)
        return s.scalars(q.where(Draft.status == status) if status else q).first()


def test_page_lists_drafts_with_recipient(env):
    client, factory, _, _ = env
    html = client.get("/").get_data(as_text=True)
    assert "Needs review" in html and "maya.r@falconpixel.example" in html
    assert "Why this lead" in html


def test_post_without_token_is_refused(env):
    client, factory, transport, _ = env
    d = first_draft(factory)
    assert client.post(f"/draft/{d.id}", data={"action": "send", "confirm": "yes"}).status_code == 403
    assert transport.sent == []


def test_edit_and_test_send_goes_to_test_inbox(env):
    client, factory, transport, token = env
    d = first_draft(factory)
    r = client.post(f"/draft/{d.id}", data={"csrf": token, "action": "test", "to_email": d.to_email,
                                            "subject": "edited subject", "body": d.body})
    assert r.status_code == 302
    assert transport.sent[0]["To"] == "me@gulftech.example"
    assert transport.sent[0]["Subject"] == f"[TEST → {d.to_email}] edited subject"
    assert first_draft(factory).status in ("draft", "needs_review")  # still waiting for review


def test_approve_and_send_live(env):
    client, factory, transport, token = env
    d = first_draft(factory)
    client.post(f"/draft/{d.id}", data={"csrf": token, "action": "send", "confirm": "yes",
                                        "to_email": d.to_email, "subject": d.subject, "body": d.body})
    assert transport.sent[0]["To"] == d.to_email
    with factory() as s:
        assert s.get(Draft, d.id).status == "sent"
        assert s.scalar(select(Send).where(Send.mode == "live")).status == "sent"
    html = client.get("/").get_data(as_text=True)
    assert "Sent to " + d.to_email in html


def test_send_without_confirmation_is_refused(env):
    client, factory, transport, token = env
    d = first_draft(factory)
    r = client.post(f"/draft/{d.id}", data={"csrf": token, "action": "send", "to_email": d.to_email,
                                            "subject": d.subject, "body": d.body})
    assert r.status_code == 400 and transport.sent == []


def test_guardrails_apply_in_the_ui(env):
    client, factory, transport, token = env
    d = first_draft(factory)
    with factory() as s:
        sender.suppress(s, d.to_email, "unsubscribed")
        s.commit()
    client.post(f"/draft/{d.id}", data={"csrf": token, "action": "send", "confirm": "yes",
                                        "to_email": d.to_email, "subject": d.subject, "body": d.body})
    assert transport.sent == []
    assert "Not sent: suppressed" in client.get("/").get_data(as_text=True)


def test_reject_and_unapprove(env):
    client, factory, _, token = env
    d = first_draft(factory)
    client.post(f"/draft/{d.id}", data={"csrf": token, "action": "reject", "reason": "not a fit"})
    with factory() as s:
        assert s.get(Draft, d.id).status == "rejected"
        other = s.scalars(select(Draft).where(Draft.status != "rejected")).first()
        drafting.approve_draft(s, other.id)
    client.post(f"/draft/{other.id}", data={"csrf": token, "action": "unapprove"})
    with factory() as s:
        assert s.get(Draft, other.id).status == "draft"


def test_invalid_recipient_is_rejected(env):
    client, factory, transport, token = env
    d = first_draft(factory)
    client.post(f"/draft/{d.id}", data={"csrf": token, "action": "send", "confirm": "yes",
                                        "to_email": "not-an-email", "subject": d.subject, "body": d.body})
    assert transport.sent == []
    assert "not an email" in client.get("/").get_data(as_text=True)
