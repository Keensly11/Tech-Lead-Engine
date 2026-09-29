"""Writes leads to out/leads.json and out/leads.csv. Good for Week 1 and demos."""

import csv
import json
from dataclasses import asdict

from lead_engine.settings import OUT_DIR
from lead_engine.sinks.base import KIND_ORDER, LeadPayload


class LocalSink:
    name = "local"
    supports_updates = True

    def __init__(self, out_dir=OUT_DIR):
        self.out_dir = out_dir
        self.out_dir.mkdir(exist_ok=True)
        self.json_path = self.out_dir / "leads.json"

    def _load(self) -> dict:
        if self.json_path.exists():
            return json.loads(self.json_path.read_text(encoding="utf-8"))
        return {}

    def upsert_lead(self, lead: LeadPayload) -> str:
        leads = self._load()
        leads[lead.key] = asdict(lead)
        self.json_path.write_text(json.dumps(leads, indent=2, ensure_ascii=False), encoding="utf-8")
        self._write_csv(leads)
        return lead.key

    def _write_csv(self, leads: dict) -> None:
        rows = sorted(leads.values(), key=lambda l: l["priority"], reverse=True)
        with open(self.out_dir / "leads.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["company", "domain", "segment", "city", "route", "priority", "fit", "intent",
                        "contact", "best_email", "product_lines", "evidence"])
            for l in rows:
                contacts = sorted(l["contacts"], key=lambda c: KIND_ORDER.get(c["kind"], 9))
                best = contacts[0]["email"] if contacts else ""
                w.writerow([l["company_name"], l["domain"], l["segment"], l["city"], l["route"],
                            l["priority"], l["fit"], l["intent"], l["contact"], best,
                            "; ".join(l["product_lines"]), " ".join(l["evidence_urls"])])
