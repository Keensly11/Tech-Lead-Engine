from lead_engine.sinks.base import LeadContact, LeadPayload, LeadSink


def get_sink(name: str) -> LeadSink:
    if name == "local":
        from lead_engine.sinks.local import LocalSink
        return LocalSink()
    if name == "odoo_api":
        from lead_engine.sinks.odoo_api import OdooApiSink
        return OdooApiSink()
    if name == "odoo_email":
        from lead_engine.sinks.odoo_email import OdooEmailAliasSink
        return OdooEmailAliasSink()
    raise ValueError(f"unknown sink {name!r} (expected local | odoo_api | odoo_email)")


__all__ = ["LeadContact", "LeadPayload", "LeadSink", "get_sink"]
