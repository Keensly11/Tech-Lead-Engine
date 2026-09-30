"""Command line entry point.

  lead-engine init
  lead-engine run --source sample [--source rss] [--sink local] [--find-contacts]
  lead-engine find-contacts [--limit 20] [--company 8]
  lead-engine set-domain 10 eqtgroup.com
  lead-engine draft [--limit 10] [--regenerate]
  lead-engine drafts [--status needs_review]
  lead-engine draft-show 1
  lead-engine approve 1 [--note "looks good"]
  lead-engine reject 1 --reason "wrong contact"
  lead-engine top [--route high] [--segment film_media]
  lead-engine show 3
  lead-engine rescore
  lead-engine status
"""

import argparse
import json
import logging

from lead_engine import drafting, pipeline, queries
from lead_engine.collectors import COLLECTORS
from lead_engine.db import SessionLocal, init_db
from lead_engine.models import Company, Draft
from lead_engine.settings import env
from lead_engine.sinks import get_sink


def dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="lead-engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create database tables")

    r = sub.add_parser("run", help="collect, score, route and export")
    r.add_argument("--source", action="append", choices=sorted(COLLECTORS), required=True)
    r.add_argument("--sink", default=env("LEAD_SINK", "local"), help="local | odoo_api | odoo_email | none")
    r.add_argument("--find-contacts", action="store_true", help="search company websites for emails before export")

    fc = sub.add_parser("find-contacts", help="find websites + public emails for promising leads without a contact")
    fc.add_argument("--limit", type=int, default=20)
    fc.add_argument("--company", type=int, action="append", help="only these company ids")
    fc.add_argument("--recheck-days", type=int, default=30, help="skip companies searched more recently than this")

    sd = sub.add_parser("set-domain", help="set a company's website by hand, then search it for contacts")
    sd.add_argument("company_id", type=int)
    sd.add_argument("domain")

    dr = sub.add_parser("draft", help="draft outreach emails for high leads (nothing is sent)")
    dr.add_argument("--limit", type=int, default=10)
    dr.add_argument("--company", type=int, action="append")
    dr.add_argument("--regenerate", action="store_true", help="replace unapproved drafts")

    ds = sub.add_parser("drafts", help="list drafts awaiting review")
    ds.add_argument("--status", choices=["draft", "needs_review", "approved", "rejected"])

    dv = sub.add_parser("draft-show", help="show a full draft")
    dv.add_argument("draft_id", type=int)

    ap = sub.add_parser("approve", help="approve a draft for sending")
    ap.add_argument("draft_id", type=int)
    ap.add_argument("--note")

    rj = sub.add_parser("reject", help="reject a draft")
    rj.add_argument("draft_id", type=int)
    rj.add_argument("--reason", required=True)

    t = sub.add_parser("top", help="list best leads")
    t.add_argument("--route", choices=["high", "review", "archive"])
    t.add_argument("--segment")
    t.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("show", help="full detail and explanation for one lead")
    s.add_argument("company_id", type=int)

    sub.add_parser("rescore", help="rescore every company (recency decay)")
    sub.add_parser("status", help="pipeline counts")

    args = p.parse_args(argv)
    init_db()
    session = SessionLocal()
    try:
        if args.cmd == "init":
            print("database ready")
        elif args.cmd == "run":
            sink = None if args.sink == "none" else get_sink(args.sink)
            stats = pipeline.run(session, args.source, sink, find_contacts=args.find_contacts)
            print(json.dumps(stats.__dict__, indent=2))
        elif args.cmd == "find-contacts":
            report = pipeline.find_contacts_for_leads(session, args.limit, args.recheck_days, args.company)
            print(json.dumps(report, indent=2, ensure_ascii=False))
        elif args.cmd == "set-domain":
            company = session.get(Company, args.company_id)
            if company is None:
                raise SystemExit(f"no company with id {args.company_id}")
            error = pipeline.set_domain(session, company, args.domain, "manual")
            if error:
                raise SystemExit(error)
            session.commit()
            report = pipeline.find_contacts_for_leads(session, recheck_days=0, company_ids=[company.id])
            print(json.dumps(report or {"company": company.name, "domain": company.domain,
                                        "note": "not searched: lead already has a usable contact or is archived"},
                             indent=2, ensure_ascii=False))
        elif args.cmd == "draft":
            dump(drafting.draft_leads(session, args.limit, args.company, args.regenerate))
        elif args.cmd == "drafts":
            dump(drafting.list_drafts(session, args.status))
        elif args.cmd == "draft-show":
            d = session.get(Draft, args.draft_id)
            if d is None:
                raise SystemExit(f"no draft with id {args.draft_id}")
            info = drafting.draft_detail(d)
            print(f"#{d.id} [{d.status}] {info['company']}\nTo: {d.to_email}\nSubject: {d.subject}\n\n{d.body}\n")
            print(f"Signal: {d.signal_url}")
            for issue in d.issues:
                print(f"! {issue}")
        elif args.cmd in ("approve", "reject"):
            try:
                if args.cmd == "approve":
                    d = drafting.approve_draft(session, args.draft_id, args.note)
                else:
                    d = drafting.reject_draft(session, args.draft_id, args.reason)
            except ValueError as exc:
                raise SystemExit(str(exc))
            print(f"draft {d.id} {d.status}")
        elif args.cmd == "top":
            rows = queries.search_leads(session, args.segment, args.route, limit=args.limit)
            print(f"{'id':>4}  {'route':7} {'prio':>5} {'fit':>5} {'int':>5} {'con':>5}  company")
            for x in rows:
                print(f"{x['id']:>4}  {x['route']:7} {x['priority']:5.2f} {x['fit']:5.2f} "
                      f"{x['intent']:5.2f} {x['contact']:5.2f}  {x['company']} ({x['segment']})")
        elif args.cmd == "show":
            print(json.dumps(queries.get_lead(session, args.company_id), indent=2, ensure_ascii=False))
        elif args.cmd == "rescore":
            print(f"rescored {pipeline.rescore_all(session)} companies")
        elif args.cmd == "status":
            print(json.dumps(queries.pipeline_status(session), indent=2))
    finally:
        session.close()


if __name__ == "__main__":
    main()
