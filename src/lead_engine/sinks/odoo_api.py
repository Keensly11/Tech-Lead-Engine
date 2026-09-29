"""Pushes leads into Odoo CRM (crm.lead) over the external XML-RPC API.

Requires API access on your Odoo plan (on Odoo Online this generally means the
Custom plan) and an API key: Preferences → Account Security → New API Key.
Upserts by website so a company is never duplicated.
"""

import xmlrpc.client

from lead_engine.settings import env
from lead_engine.sinks.base import LeadPayload


def _stars(priority: float) -> str:
    # crm.lead.priority is a selection of '0'..'3' (shown as stars).
    if priority >= 0.6:
        return "3"
    if priority >= 0.45:
        return "2"
    if priority >= 0.2:
        return "1"
    return "0"


class OdooApiSink:
    name = "odoo_api"
    supports_updates = True

    def __init__(self):
        self.url = env("ODOO_URL").rstrip("/")
        self.db = env("ODOO_DB")
        self.user = env("ODOO_USER")
        self.key = env("ODOO_API_KEY")
        if not all([self.url, self.db, self.user, self.key]):
            raise RuntimeError("Set ODOO_URL, ODOO_DB, ODOO_USER and ODOO_API_KEY in .env")

        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self.uid = common.authenticate(self.db, self.user, self.key, {})
        if not self.uid:
            raise RuntimeError("Odoo authentication failed")
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    def _call(self, method: str, *args, **kwargs):
        return self.models.execute_kw(self.db, self.uid, self.key, "crm.lead", method, list(args), kwargs)

    def _find_existing(self, lead: LeadPayload) -> int | None:
        domain = [["website", "ilike", lead.domain]] if lead.domain else [["partner_name", "=ilike", lead.company_name]]
        ids = self._call("search", domain, limit=1)
        return ids[0] if ids else None

    def upsert_lead(self, lead: LeadPayload) -> str:
        best = lead.best_contact
        title = lead.product_lines[0] if lead.product_lines else "IT equipment"
        vals = {
            "name": f"{lead.company_name}: {title}",
            "partner_name": lead.company_name,
            "website": f"https://{lead.domain}" if lead.domain else False,
            "city": lead.city or False,
            "priority": _stars(lead.priority),
            "description": lead.render_html(),
            "email_from": best.email if best else False,
            "contact_name": (best.name if best else None) or False,
            "function": (best.role if best else None) or False,
        }
        # Once Studio fields exist (x_studio_fit etc.), add them here.

        existing = self._find_existing(lead)
        if existing:
            self._call("write", [existing], vals)
            return str(existing)
        return str(self._call("create", vals))
