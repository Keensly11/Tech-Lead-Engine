"""MCP server: lets an AI assistant (e.g. Claude Desktop) inspect and operate the pipeline.

Week 1 tools are read-mostly. Draft approval and sending arrive in Weeks 3-5 and
will go through the sender's caps and suppression list, never around them.

Run:  python -m mcp_server.server   (stdio transport)
"""

from mcp.server.mcpserver import MCPServer

from lead_engine import pipeline, queries
from lead_engine.db import SessionLocal, init_db
from lead_engine.models import Company
from lead_engine.schemas import Outcome

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
