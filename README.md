# Tech Lead Engine

Signal-based B2B lead discovery for a UAE tech reseller (laptops, servers, peripherals,
cameras/AV). It finds companies showing **buying signals**, scores them with **three
explainable confidence scores**, routes them by confidence, and delivers qualified
leads to **Odoo CRM**. An **MCP server** lets an AI assistant query and operate the pipeline.

```
collectors ─► resolve/dedup ─► signals ─► score (fit × intent, contact) ─► route ─► sink (Odoo / CSV)
                                                                           │
                                  MCP server: search · explain · rescore · log outcomes
```

## Why signals, not company lists
Almost every company "needs laptops". What matters is **who needs them now**. The engine
looks for events that precede purchases: new offices, hiring surges, funding rounds, new
licences, film/TV productions, IT tenders and IT/data-centre hiring.

## Scoring
| Score | Question | Built from |
|---|---|---|
| **fit** | Is this our kind of customer? | segment, size band, UAE location (`config/icp.yaml`) |
| **intent** | Are they likely buying soon? | each signal's weight × recency decay × extraction confidence, combined by noisy-OR |
| **contact** | Can we reach the right person? | best contact's provenance: verified / pattern / generic |

`priority = fit × intent`. Routing (`config/scoring.yaml`):
- **high**: priority ≥ 0.45 and a usable contact → CRM, ready for sales
- **review**: promising but uncertain, or no contact yet → CRM, a human checks first
- **archive**: kept in the DB and rescored automatically when new signals arrive

Every score carries a line-by-line explanation with source URLs.

## Contact finder
Promising leads with no usable contact stay in **review**. `lead-engine find-contacts`
(or `run --find-contacts`) searches for public business emails:

1. **Website.** Uses the known domain, or guesses candidates from the name (`braindigits.com`,
   `qamarcloud.ae`…). A guess is accepted only if the homepage title names the company and
   the page isn't parked. Single-word names ("EQT") aren't guessed because they're too
   ambiguous; use `set-domain` instead.
2. **Crawl.** At most 6 pages on the company's own site (home, contact, about, team),
   honouring `robots.txt`, with a delay between requests. Sites behind bot protection are
   reported for a human to check, never bypassed.
3. **Emails.** Extracted from mailto links, plain text, `[at]` obfuscation and Cloudflare email
   protection. Only addresses on the company's domain are kept, and careers, HR, no-reply and
   privacy inboxes are dropped. Personal emails are **never guessed**, because guessed
   addresses bounce and damage the sending domain's reputation.

A lead that gains a usable contact is rescored and can move from review to high. Each
company is searched at most once every 30 days.

## Email drafts
`lead-engine draft` writes an outreach email for each **high** lead that has a contact.
**Nothing is sent**: drafts wait for a person to approve them.

- The LLM writes only the **subject**, a **1–2 sentence opener** about the lead's strongest
  news signal, and which **product lines** to lead with. Everything else (who we are,
  website, phone, opt-out line) is a fixed template filled from `config/products.yaml`.
- **Fact check:** numbers and names in the opener or subject must appear in the source
  article, and offer or price language ("free", "discount", "%", "AED 500") is flagged.
  Flagged drafts get status `needs_review` with the reasons listed.
- **Approval rules:** a draft can't be approved while the sender details are placeholders or
  if the unsubscribe line was removed. Approved drafts are never regenerated.

```bash
lead-engine draft                 # draft for high leads without an active draft
lead-engine drafts                # list drafts awaiting review
lead-engine draft-show 9          # full email + source + issues
lead-engine approve 9 --note "ok"
lead-engine reject 9 --reason "wrong contact"
```

## Sending
`lead-engine send` sends **approved** drafts. It has three modes (`SEND_MODE` in `.env`, or `--mode`):

| Mode | What happens |
|---|---|
| `dry_run` (default) | writes `out/outbox/draft-<id>.eml`, which you can open in any mail client. Nothing is sent. |
| `test` | sends everything to `TEST_INBOX`, with the real recipient shown in the subject |
| `live` | sends to the real recipient. Also requires `--confirm-live`. |

Live guardrails: a suppression list (unsubscribed and bounced addresses, or whole `@domain`s),
**one email per company per 90 days**, a **daily cap** (`DAILY_SEND_CAP`, default 20), and a
`pending` record saved before each SMTP call so a crash can't cause a double send. Emails include
`Reply-To` and a `List-Unsubscribe` header.

`lead-engine check-inbox` reads the inbox over IMAP **read-only** and records outcomes:
- replies (matched by `In-Reply-To` / `References`), which ignore out-of-office auto-replies
- "unsubscribe" replies, which are added to the suppression list
- bounces, where the failed address is added to the suppression list

Hostinger settings: `smtp.hostinger.com:465`, `imap.hostinger.com:993`. Keep volume low while
the domain builds a reputation, and make sure SPF, DKIM and DMARC are set up in hPanel.

## Reliability
- **Validated LLM output.** News extraction must match a Pydantic schema, meet a
  confidence threshold, and quote evidence that actually appears in the article.
- **Idempotent.** Signals are deduplicated by (URL, company), companies by domain then
  fuzzy name, and exports by payload hash. Re-running never duplicates anything.
- **Failure isolation.** One failed export is logged and skipped; the run continues.

## Quick start
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -e ".[dev]"
copy .env.example .env          # cp on macOS/Linux
pytest
lead-engine run --source sample --sink local
lead-engine top
lead-engine show 1
```
> **Windows with Smart App Control:** if imports fail with *"An Application Control policy
> has blocked this file"*, reinstall SQLAlchemy as pure Python (no compiled DLLs):
> `set DISABLE_SQLALCHEMY_CEXT=1` then
> `pip install --force-reinstall --no-deps --no-binary sqlalchemy "sqlalchemy>=2.0"`.

`--source sample` uses fictional companies in `eval/sample_signals.json`, with no network or
LLM needed. `--source rss` reads the feeds in `config/sources.yaml` and needs
[Ollama](https://ollama.com) running (`ollama pull llama3.1:8b`).

## Odoo
Set `LEAD_SINK` in `.env`:
- `odoo_api`: creates/updates `crm.lead` records over XML-RPC and upserts by website.
  Needs API access (on Odoo Online, generally the Custom plan) and an API key.
- `odoo_email`: emails the CRM alias (`leads@yourcompany.odoo.com`) and Odoo creates one lead
  per email. Works on any plan with the CRM app, but can't update leads afterwards.
- `local`: writes `out/leads.csv` and `out/leads.json`.

For development, `docker compose up odoo odoo-db` runs Odoo Community at http://localhost:8069.

## MCP server
```bash
python -m mcp_server.server
```
Claude Desktop config (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "tech-lead-engine": {
      "command": "C:\\path\\to\\Tech-Lead-Engine\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\Tech-Lead-Engine\\mcp_server\\server.py"]
    }
  }
}
```
Claude Desktop doesn't set a working directory, so use absolute paths. Relative SQLite
paths in `.env` resolve against the project root, so the CLI and MCP server share one database.
Fully quit Claude Desktop (from the system tray) and reopen it to load the server.
Tools: `search_leads`, `get_lead`, `explain_score`, `rescore`, `find_contacts`, `set_domain`,
`add_contact`, `draft_emails`, `list_drafts`, `get_draft`, `edit_draft`, `approve_draft`,
`reject_draft`, `send_status`, `check_inbox`, `suppress_email`, `log_outcome`, `pipeline_status`.
There is deliberately no MCP tool that sends email: sending stays a CLI action by a person.
Example prompts: "top film leads and why", "find contacts for the review leads",
"EQT's website is eqtgroup.com", "log that Northwind replied, meeting booked".

## Roadmap
1. ~~MVP: sample + RSS collectors, dedup, scoring, routing, local/Odoo sinks, MCP (read + outcomes)~~
1. ~~Contact finder: domain discovery, polite crawl, email extraction, manual set_domain / add_contact~~
1. ~~Email drafting grounded in evidence + fact-check + human approval (CLI and MCP)~~
1. ~~Sender: dry-run/test/live modes, daily cap, suppression list, reply/unsubscribe/bounce detection~~
2. Eval set: hand-label 100 real leads (`eval/`) and measure precision@20 of the scorer
3. Email drafting grounded in evidence + fact-check + approval via Odoo stage
4. Sender: dedicated subdomain, SPF/DKIM/DMARC, daily caps, suppression list, 2 follow-ups max
5. Feedback loop: outcomes → reply rate per signal type → suggested weight updates
6. More sources: tenders, free-zone registries, job boards (official APIs only)

## Compliance notes
Use official APIs, RSS and public registries, and respect robots.txt. Don't scrape
LinkedIn. Cold email must include an unsubscribe option and honour it. Get your company's
sign-off on the sending domain and data sources before sending anything.
