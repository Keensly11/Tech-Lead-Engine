"""LLM structured extraction via a local Ollama model.

The model is asked for JSON matching NewsExtraction's schema. The output is then
validated with Pydantic and checked against the source text: the evidence quote
must actually appear in the article. Anything that fails is dropped, not guessed.
"""

import logging

import httpx

from lead_engine.llm import ollama_json
from lead_engine.schemas import NewsExtraction

log = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.5

PROMPT = """You find B2B sales leads for a UAE company that sells laptops, desktops, monitors,
peripherals, servers, networking, cameras and AV equipment.

Read the news item. Decide if it describes ONE specific company doing something that implies
it will soon buy such equipment (new office, expansion, hiring many people, new company,
funding, film/TV production or studio launch, IT tender, IT/data-centre hiring).

Rules:
- company_name must be the company's name exactly as written in the text (e.g. "EQT", not "Global Private Markets Firm").
- If no specific company is named, or the company is a government ministry announcing policy, is_relevant=false.
- evidence_quote MUST be copied exactly from the text.
- Do not invent a domain. Leave company_domain null unless it appears in the text.
- confidence reflects how sure you are that this is a real buying signal (0-1).

Signal types: it_tender, new_office, production_launch, hiring_surge, new_company, funding_round, it_hiring, other
Segments: tech, film_media, corporate, education, government, events, retail, other, unknown

News item:
TITLE: {title}
TEXT: {text}
"""


def _evidence_is_grounded(quote: str | None, source_text: str) -> bool:
    if not quote:
        return False
    squash = lambda s: " ".join(s.lower().split())
    return squash(quote)[:80] in squash(source_text)


def _name_is_grounded(name: str, source_text: str) -> bool:
    """The company must be named in the article, which blocks invented companies."""
    name = name.strip()
    return len(name) >= 2 and name.lower() in source_text.lower()


def extract_from_news(title: str, text: str, client: httpx.Client | None = None) -> NewsExtraction | None:
    result = ollama_json(PROMPT.format(title=title, text=text), NewsExtraction, client)
    if result is None or not result.is_relevant:
        return None
    if not _name_is_grounded(result.company_name, f"{title}\n{text}"):
        log.info("company %r not named in source, skipped: %s", result.company_name, title)
        return None
    if result.confidence < MIN_CONFIDENCE:
        log.info("low confidence (%.2f), skipped: %s", result.confidence, title)
        return None
    if not _evidence_is_grounded(result.evidence_quote, f"{title}\n{text}"):
        log.info("evidence quote not found in source, skipped: %s", title)
        return None
    return result
