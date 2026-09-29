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
    "lead-engine": {
      "command": "C:\\path\\to\\lead-engine\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server.server"],
      "cwd": "C:\\path\\to\\lead-engine"
    }
  }
}
```
Tools: `search_leads`, `get_lead`, `explain_score`, `rescore`, `log_outcome`, `pipeline_status`.
Example prompts: "top film leads and why", "log that Northwind replied, meeting booked".

## Roadmap
1. ~~MVP: sample + RSS collectors, dedup, scoring, routing, local/Odoo sinks, MCP (read + outcomes)~~
2. Eval set: hand-label 100 real leads (`eval/`) and measure precision@20 of the scorer
3. Email drafting grounded in evidence + fact-check + approval via Odoo stage
4. Sender: dedicated subdomain, SPF/DKIM/DMARC, daily caps, suppression list, 2 follow-ups max
5. Feedback loop: outcomes → reply rate per signal type → suggested weight updates
6. More sources: tenders, free-zone registries, job boards (official APIs only)

## Compliance notes
Use official APIs, RSS and public registries, and respect robots.txt. Don't scrape
LinkedIn. Cold email must include an unsubscribe option and honour it. Get your company's
sign-off on the sending domain and data sources before sending anything.
