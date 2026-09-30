"""Local review page: see each draft, where it goes and why, then send it with a click.

Runs on 127.0.0.1 only. Every POST carries a per-process token, so other websites
open in your browser can't trigger a send. The same guardrails as the CLI apply
(suppression list, 90-day company cooldown, daily cap).

    lead-engine ui            → http://127.0.0.1:8765
"""

import secrets

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from lead_engine import drafting, inbox, sender
from lead_engine.db import SessionLocal, init_db
from lead_engine.drafting import best_contact, lead_signal
from lead_engine.models import Draft, Send
from lead_engine.settings import env


def create_app(session_factory=SessionLocal, transport=None) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    csrf_token = secrets.token_urlsafe(32)

    @app.context_processor
    def inject():
        return {"csrf_token": csrf_token, "test_inbox": env("TEST_INBOX")}

    @app.before_request
    def protect():
        if request.method == "POST" and request.form.get("csrf") != csrf_token:
            abort(403)

    @app.get("/")
    def index():
        with session_factory() as s:
            drafts = s.scalars(select(Draft).order_by(Draft.created_at.desc())).all()
            cards = [_card(s, d) for d in drafts]
            status = sender.send_status(s)
        return render_template(
            "index.html",
            ready=[c for c in cards if c["status"] == "approved"],
            review=[c for c in cards if c["status"] in ("draft", "needs_review")],
            sent=[c for c in cards if c["status"] == "sent"][:20],
            status=status,
        )

    @app.post("/draft/<int:draft_id>")
    def act(draft_id: int):
        action = request.form.get("action")
        with session_factory() as s:
            d = s.get(Draft, draft_id)
            if d is None:
                abort(404)
            company = d.company.name
            try:
                if d.status in ("draft", "needs_review") and action in ("save", "test", "send"):
                    drafting.edit_draft(s, draft_id, request.form.get("subject"), request.form.get("body"),
                                        request.form.get("to_email"))

                if action == "save":
                    flash(f"Saved changes to the {company} email.", "ok")
                elif action == "reject":
                    drafting.reject_draft(s, draft_id, request.form.get("reason") or "rejected in review page")
                    flash(f"Rejected the {company} email.", "ok")
                elif action == "unapprove":
                    drafting.unapprove_draft(s, draft_id)
                    flash(f"Moved the {company} email back to review so you can edit it.", "ok")
                elif action == "test":
                    _report(sender.send_one(s, draft_id, "test", transport), company)
                elif action == "send":
                    if request.form.get("confirm") != "yes":
                        abort(400)
                    if d.status != "approved":
                        drafting.approve_draft(s, draft_id, "approved and sent from review page")
                    _report(sender.send_one(s, draft_id, "live", transport), company)
                else:
                    abort(400)
            except (ValueError, RuntimeError, OSError) as exc:
                flash(str(exc), "error")
        return redirect(url_for("index") + f"#draft-{draft_id}")

    @app.post("/inbox")
    def check_inbox():
        with session_factory() as s:
            try:
                found = inbox.check_inbox(s)
            except (RuntimeError, OSError) as exc:
                flash(f"Inbox check failed: {exc}", "error")
            else:
                if not found:
                    flash("No new replies, opt-outs or bounces.", "ok")
                for r in found:
                    flash(f"{r['kind'].replace('_', ' ').title()} from {r['from']}: {r['subject']}", "ok")
        return redirect(url_for("index"))

    return app


def _report(result: dict, company: str) -> None:
    if result["result"] == "sent" and result["mode"] == "test":
        flash(f"Test copy of the {company} email sent to {result['delivered_to']}.", "ok")
    elif result["result"] == "sent":
        flash(f"Sent to {result['delivered_to']}. Replies will arrive in your inbox.", "ok")
    else:
        flash(f"Not sent: {result.get('reason', result['result'])}", "error")


def _card(s, d: Draft) -> dict:
    company = d.company
    signal = lead_signal(company) if company.signals else None
    contact = next((c for c in company.contacts if c.email == d.to_email), None) or best_contact(company)
    sends = s.scalars(select(Send).where(Send.draft_id == d.id).order_by(Send.created_at.desc())).all()
    live = next((x for x in sends if x.mode == "live" and x.status == "sent"), None)
    return {
        "id": d.id, "status": d.status, "company": company.name, "domain": company.domain,
        "to_email": d.to_email, "subject": d.subject, "body": d.body, "issues": d.issues or [],
        "note": d.reviewer_note,
        "contact_kind": {"verified": "named person", "generic": "shared inbox", "pattern": "guessed"}.get(
            contact.kind if contact else "", "unknown"),
        "contact_source": contact.source_url if contact else None,
        "signal_title": drafting._clean_title(signal.title) if signal else None,
        "signal_url": d.signal_url,
        "priority": company.score.priority if company.score else None,
        "tests_sent": sum(1 for x in sends if x.mode == "test" and x.status == "sent"),
        "sent_at": live.created_at if live else None,
        "failed": next((x.error for x in sends if x.status == "failed"), None),
    }


def serve(port: int = 8765) -> None:
    init_db()
    print(f"Review page: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    create_app().run(host="127.0.0.1", port=port, debug=False)
