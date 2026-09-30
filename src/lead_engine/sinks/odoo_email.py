"""Creates Odoo CRM leads by emailing the CRM alias (e.g. leads@company.odoo.com).

Works on any Odoo plan with the CRM app, no API access needed. Each email becomes
one lead: subject → lead name, body → description. Odoo can't update a lead this
way, so each company is exported once (supports_updates = False). Later score
changes stay visible in our own DB and the MCP tools.
"""

from email.message import EmailMessage

from lead_engine.sender import SmtpTransport
from lead_engine.settings import env
from lead_engine.sinks.base import LeadPayload


class OdooEmailAliasSink:
    name = "odoo_email"
    supports_updates = False

    def __init__(self):
        self.alias = env("ODOO_LEAD_ALIAS")
        if not self.alias:
            raise RuntimeError("Set ODOO_LEAD_ALIAS in .env")
        self.transport = SmtpTransport()  # SSL on 465 or STARTTLS on 587

    def upsert_lead(self, lead: LeadPayload) -> str:
        title = lead.product_lines[0] if lead.product_lines else "IT equipment"
        msg = EmailMessage()
        msg["From"] = self.transport.user
        msg["To"] = self.alias
        msg["Subject"] = f"[{lead.route.upper()} {lead.priority:.2f}] {lead.company_name}: {title}"
        msg.set_content(lead.render_text())
        self.transport.send(msg)
        return msg["Subject"]
