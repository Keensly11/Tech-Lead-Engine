"""MCP server: lets an AI assistant (e.g. Claude Desktop) inspect and operate the pipeline.

Week 1 tools are read-mostly. Draft approval and sending arrive in Weeks 3-5 and
will go through the sender's caps and suppression list, never around them.

Run:  python -m mcp_server.server   (stdio transport)
"""

from mcp.server.mcpserver import MCPServer

from lead_engine import drafting, inbox, pipeline, queries, sender
from lead_engine.db import SessionLocal, init_db
from lead_engine.models import Company, Contact, Draft
from lead_engine.schemas import Outcome, RawContact

mcp = MCPServer("lead-engine")
init_db()


@mcp.tool()
def search_leads(segment: str | None = None, route: str | None = None,
                 min_priority: float = 0.0, limit: int = 20) -> list[dict]:
    """Find leads, best first.

    segment: tech | film_media | corporate | education | government | events | retail | other | unknown
    route: high (ready for sales) | review (needs a human) | archive (weak)
    """
    with SessionLocal() as s:
        return queries.search_leads(s, segment, route, min_priority, limit)


@mcp.tool()
def get_lead(company_id: int) -> dict:
    """Full lead detail: company, scores, every signal with source URL and evidence, contacts, outcomes."""
    with SessionLocal() as s:
        lead = queries.get_lead(s, company_id)
        return lead or {"error": f"no company with id {company_id}"}


@mcp.tool()
def explain_score(company_id: int) -> list[str]:
    """Why a lead got its score and route, line by line, with evidence links."""
    with SessionLocal() as s:
        lead = queries.get_lead(s, company_id)
        if not lead or not lead["score"]:
            return [f"company {company_id} not found or not scored"]
        return lead["score"]["explanation"]


@mcp.tool()
def rescore(company_id: int) -> dict:
    """Recompute one company's scores now (e.g. after adding a contact)."""
    with SessionLocal() as s:
        company = s.get(Company, company_id)
        if company is None:
            return {"error": f"no company with id {company_id}"}
        score = pipeline.rescore(s, company)
        s.commit()
        return {"route": score.route, "priority": score.priority, "fit": score.fit,
                "intent": score.intent, "contact": score.contact}


@mcp.tool()
def find_contacts(company_id: int | None = None, limit: int = 5) -> list[dict]:
    """Search company websites for public business emails, then rescore.

    With company_id, searches that company now. Without it, searches the best
    high/review leads that have no usable contact. Only reads each company's own
    site (≤6 pages, robots.txt honoured) and never guesses personal emails.
    """
    with SessionLocal() as s:
        ids = [company_id] if company_id is not None else None
        report = pipeline.find_contacts_for_leads(s, limit=limit, recheck_days=0 if ids else 30, company_ids=ids)
        return report or [{"note": "nothing to search: lead has a usable contact, is archived, or was searched recently"}]


@mcp.tool()
def set_domain(company_id: int, domain: str) -> dict:
    """Set a company's website by hand (e.g. when the name is too ambiguous to guess), then search it for contacts."""
    with SessionLocal() as s:
        company = s.get(Company, company_id)
        if company is None:
            return {"error": f"no company with id {company_id}"}
        error = pipeline.set_domain(s, company, domain, "manual")
        if error:
            return {"error": error}
        s.commit()
        report = pipeline.find_contacts_for_leads(s, recheck_days=0, company_ids=[company_id])
        return report[0] if report else {"company": company.name, "domain": company.domain,
                                         "note": "not searched: lead already has a usable contact or is archived"}


@mcp.tool()
def add_contact(company_id: int, email: str, name: str | None = None, role: str | None = None) -> dict:
    """Add a contact a salesperson confirmed (e.g. from a call or business card). Stored as verified."""
    with SessionLocal() as s:
        company = s.get(Company, company_id)
        if company is None:
            return {"error": f"no company with id {company_id}"}
        try:
            contact = RawContact(email=email, kind="verified", name=name, role=role, source_url="manual")
        except ValueError as exc:
            return {"error": str(exc)}
        if any(c.email == contact.email for c in company.contacts):
            return {"error": f"{contact.email} is already a contact"}
        s.add(Contact(company_id=company.id, **contact.model_dump()))
        score = pipeline.rescore(s, company)
        s.commit()
        return {"added": contact.email, "route": score.route, "contact": score.contact, "priority": score.priority}


@mcp.tool()
def draft_emails(company_id: int | None = None, limit: int = 5, regenerate: bool = False) -> list[dict]:
    """Draft outreach emails for high-priority leads that have a contact. Nothing is sent.

    Drafts are grounded in the lead's news signal and fact-checked; flagged ones get
    status needs_review. regenerate=True replaces unapproved drafts.
    """
    with SessionLocal() as s:
        ids = [company_id] if company_id is not None else None
        return drafting.draft_leads(s, limit, ids, regenerate) or [
            {"note": "nothing to draft: no high lead with a contact and without an active draft"}]


@mcp.tool()
def list_drafts(status: str | None = None) -> list[dict]:
    """Drafts awaiting review (draft + needs_review), or those with a given status: draft | needs_review | approved | rejected."""
    with SessionLocal() as s:
        return drafting.list_drafts(s, status)


@mcp.tool()
def get_draft(draft_id: int) -> dict:
    """The full email: recipient, subject, body, source signal and any fact-check issues."""
    with SessionLocal() as s:
        d = s.get(Draft, draft_id)
        return drafting.draft_detail(d) if d else {"error": f"no draft with id {draft_id}"}


@mcp.tool()
def edit_draft(draft_id: int, subject: str | None = None, body: str | None = None) -> dict:
    """Change a pending draft's subject and/or body before approving it."""
    with SessionLocal() as s:
        try:
            return drafting.draft_detail(drafting.edit_draft(s, draft_id, subject, body))
        except ValueError as exc:
            return {"error": str(exc)}


@mcp.tool()
def approve_draft(draft_id: int, note: str | None = None) -> dict:
    """Approve a draft for sending. Only do this when the user explicitly approves this specific draft.

    Approval doesn't send anything yet; the sender (with daily caps and a suppression list) is a later step.
    """
    with SessionLocal() as s:
        try:
            return drafting.draft_summary(drafting.approve_draft(s, draft_id, note))
        except ValueError as exc:
            return {"error": str(exc)}


@mcp.tool()
def reject_draft(draft_id: int, reason: str) -> dict:
    """Reject a draft, with the reason (it's kept as feedback for improving drafts)."""
    with SessionLocal() as s:
        try:
            return drafting.draft_summary(drafting.reject_draft(s, draft_id, reason))
        except ValueError as exc:
            return {"error": str(exc)}


@mcp.tool()
def send_status() -> dict:
    """Sending overview: current mode, approved drafts waiting, live emails in the last 24h vs the daily cap,
    failures and suppression-list size. Sending itself is CLI-only (lead-engine send)."""
    with SessionLocal() as s:
        return sender.send_status(s)


@mcp.tool()
def check_inbox(days: int = 14) -> list[dict]:
    """Read the sales inbox (read-only) and record replies, unsubscribes and bounces as lead outcomes."""
    with SessionLocal() as s:
        try:
            return inbox.check_inbox(s, days) or [{"note": "no new replies, unsubscribes or bounces"}]
        except (RuntimeError, OSError) as exc:
            return [{"error": f"inbox check failed: {exc}"}]


@mcp.tool()
def suppress_email(email: str, reason: str = "manual") -> str:
    """Never email this address again. Use '@domain.com' to block a whole company."""
    with SessionLocal() as s:
        added = sender.suppress(s, email, reason)
        s.commit()
        return f"{email} {'suppressed' if added else 'was already suppressed'}"


@mcp.tool()
def log_outcome(company_id: int, outcome: Outcome, note: str | None = None) -> str:
    """Record what happened with a lead. This is the feedback data used to recalibrate scoring."""
    with SessionLocal() as s:
        queries.log_outcome(s, company_id, outcome, note)
    return f"logged {outcome} for company {company_id}"


@mcp.tool()
def pipeline_status() -> dict:
    """Counts of companies, signals, leads per route, exports and outcomes."""
    with SessionLocal() as s:
        return queries.pipeline_status(s)


if __name__ == "__main__":
    mcp.run()
