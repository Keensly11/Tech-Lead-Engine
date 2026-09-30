"""Outreach email drafts, grounded in the lead's actual buying signal.

The LLM writes only three things: a subject, a 1-2 sentence opener about the
news, and which product lines to lead with. The rest (who we are, website,
contact details, opt-out line) is a fixed template, so the model has no room to
invent prices, promises or contact details.

Every draft is fact-checked: numbers and names in the opener must appear in the
source article, and offer/price language is flagged. Flagged drafts get status
`needs_review`. Nothing is sent from here: drafts wait for a human to approve them.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from lead_engine.llm import ollama_json
from lead_engine.models import Company, Contact, Draft, Score, Signal
from lead_engine.resolve import display_name
from lead_engine.schemas import DraftCopy, RawContact
from lead_engine.scoring import recency_decay
from lead_engine.settings import CONFIG_DIR, load_config

log = logging.getLogger(__name__)

ACTIVE = ("draft", "needs_review", "approved")
MAX_OPENER_WORDS = 45
MAX_SUBJECT_CHARS = 50
MAX_SUBJECT_WORDS = 7
# A plain-language opt-out. It reads like a person, not a mailing list; the inbox
# checker treats replies like "not relevant" or "please remove us" as opt-outs.
OPT_OUT = "P.S. If this isn't relevant for you, just let us know and we won't reach out again."
OPT_OUT_MARKERS = ("won't reach out", "unsubscribe")
ROLE_PREFERENCE = {"Procurement": 0, "IT": 1, "Sales": 3, "Administration": 3, "Office": 3}

# Words the opener may use even though they aren't in the article.
ALLOWED_WORDS = {
    "uae", "dubai", "abu", "dhabi", "sharjah", "ajman", "emirates", "middle", "east", "gcc",
    "congratulations", "congrats", "it", "av", "pc", "pcs", "saw", "i",
}
BANNED = re.compile(
    r"\b(free|discounts?|cheapest|lowest|best price|guarantee[ds]?|limited[- ]time|offer expires|"
    r"special offer|deal|promo(tion)?|save \d+)\b|%|\baed\s?\d|\$\s?\d|\busd\s?\d",
    re.I,
)
# Empty, speculative or typically machine-written phrases. They add nothing, claim
# things the article doesn't say, or make the email read like a template.
FILLER = re.compile(
    r"\b(strategic|excited|curious|seamless(ly)?|cutting[- ]edge|state[- ]of[- ]the[- ]art|"
    r"world[- ]class|synerg\w*|game[- ]chang\w*|leverag\w*|increasing demands|come to life|"
    r"robust|streamlin\w*|elevat\w*|empower\w*|delve|tailored|solutions?|infrastructure needs|"
    r"in today's|look no further|rest assured|i hope this (email|message) finds you)\b",
    re.I,
)
PLACEHOLDER_MARKERS = ("YOUR COMPANY NAME", "example.com", "+971 00 000 0000")

PROMPT = """You're a salesperson at {sender}, a UAE supplier of IT and AV equipment, writing a quick,
friendly note to a company you just read about. Write the way a real person types an email: short,
plain words, no buzzwords, nothing that sounds like marketing copy.

Their company: {company}
The news: "{title}"
Detail: "{evidence}"
Products we could offer: {labels}

Write:
- product_focus: the 1-2 most relevant product labels from the list above, copied exactly.
- opener: exactly 2 short sentences, under 35 words in total.
  Sentence 1 mentions their news casually, using only facts above.
    Good: "Saw that you've just opened a new office in Abu Dhabi, congrats."
    Good: "Saw the news about your tender for 400 student laptops and classroom displays."
  Sentence 2 makes one practical link to equipment, the way a person would say it.
    Good: "Setting up a new office usually means a lot of laptops and screens to sort out."
  The examples show the tone only: write your own sentences about THIS news, never copy the examples.
  Don't write "Congratulations on", "I hope this email finds you well", "solutions" or "infrastructure needs".
  No opinions about their strategy, no prices, discounts, offers or promises.
  No greeting, no sign-off, no dashes.
- subject: 2 to 6 words, lowercase except names, like a colleague would write. No dashes or colons.
    Good: "your new Abu Dhabi office"   Good: "laptops for the new office"
"""

LLM = Callable[[str, type[DraftCopy], httpx.Client | None], DraftCopy | None]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def best_contact(company: Company) -> Contact | None:
    """A named person on the company's site first, then the most relevant shared inbox."""
    def rank(c: Contact) -> tuple:
        return (0 if c.kind == "verified" else 1 if c.kind == "pattern" else 2,
                ROLE_PREFERENCE.get(c.role or "", 2), c.email)
    usable = [c for c in company.contacts if c.kind in ("verified", "pattern", "generic")]
    return min(usable, key=rank, default=None)


def lead_signal(company: Company, now: datetime | None = None) -> Signal | None:
    """The strongest recent signal: what the email should be about."""
    now = now or datetime.now(timezone.utc)
    cfg = load_config("scoring")
    weights, half_life = cfg["signal_weights"], cfg["recency_half_life_days"]

    def strength(s: Signal) -> float:
        age = (now - _aware(s.published_at)).total_seconds() / 86400
        return weights.get(s.signal_type, weights["other"]) * recency_decay(age, half_life) * s.extraction_confidence

    return max(company.signals, key=strength, default=None)


def sender_details() -> dict:
    """Company details for the email template.

    Real details live in config/sender.yaml (git-ignored, so they stay out of the
    public repo) and override the placeholders in products.yaml.
    """
    details = dict(load_config("products")["company"])
    local = CONFIG_DIR / "sender.yaml"
    if local.exists():
        with open(local, encoding="utf-8") as f:
            details.update(yaml.safe_load(f) or {})
    return details


def _clean_title(title: str) -> str:
    """Drop the ' - Publisher' suffix news feeds add to headlines."""
    return re.sub(r"\s+-\s+[^-]{2,60}$", "", title).strip()


def sentence_case(text: str, sources: list[str]) -> str:
    """'New HQ In Dubai South - IT Infrastructure Considerations' → 'New HQ in Dubai South - IT infrastructure considerations'.

    Models write subjects in Title Case, which makes every word look like a name.
    Keep capitals only for acronyms and words capitalised in the article.
    """
    names = {w for s in sources for w in re.findall(r"\b[A-Z][\w’'-]*", s)}
    out = []
    for i, w in enumerate(re.split(r"(\s+)", text.strip())):
        core = w.strip(".,:;!?()\"“”")  # "Dhabi:" must still match "Dhabi"
        if i == 0 or w.isspace() or not core[:1].isalpha() or core.isupper() or core in names or _word(core) in names:
            out.append(w)
        else:
            out.append(w.lower())
    return "".join(out)


def mentioned_products(text: str, products: dict | None = None) -> list[str]:
    """Product lines whose equipment the article names, e.g. a tender for "400 laptops and 25 displays".

    When the source is this explicit, the email must pitch exactly that, not
    whatever the model prefers.
    """
    products = products or load_config("products")
    text = text.lower()
    found = []
    for line in products["product_lines"].values():
        if any(re.search(rf"\b{re.escape(k)}\b", text) for k in line.get("keywords", [])):
            found.append(line["label"])
    return found


def label_in_sentence(label: str) -> str:
    """'Cameras, lenses & AV equipment' → 'cameras, lenses & AV equipment' (acronyms keep their capitals)."""
    return " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in label.split())


def spoken(label: str, products: dict | None = None) -> str:
    """How a person would say a product line: 'Servers, storage & networking' → 'servers and networking gear'."""
    products = products or load_config("products")
    for line in products["product_lines"].values():
        if line["label"] == label and line.get("spoken"):
            return line["spoken"]
    return label_in_sentence(label)


def join_naturally(items: list[str]) -> str:
    """['laptops', 'monitors'] → 'laptops and monitors'; but when an item already has
    'and' or a comma, 'laptops and desktops, plus monitors and accessories'."""
    if len(items) < 2:
        return "".join(items)
    if any("," in i or " and " in i for i in items):
        return ", plus ".join(items)
    return " and ".join(items)


def _word(w: str) -> str:
    """Normalise a token: drop possessives and trailing punctuation ("Digits’s" → "digits")."""
    return re.sub(r"(['’]s?|[-&])$", "", w)


def check_copy(copy: DraftCopy, sources: list[str], allowed_products: list[str]) -> list[str]:
    """Return a list of problems. Empty means the copy is grounded in the sources."""
    issues = []
    source = " ".join(sources).lower()
    source_words = {_word(w) for w in re.findall(r"[a-z0-9][\w&'’-]*", source)}
    source_numbers = set(re.findall(r"\d[\d,.]*\d|\d", source))
    allowed = ALLOWED_WORDS | {w for p in allowed_products for w in re.findall(r"[a-z]+", p.lower())}

    if not copy.opener.strip():
        issues.append("opener is empty")
    if not copy.subject.strip():
        issues.append("subject is empty")
    if len(copy.subject) > MAX_SUBJECT_CHARS:
        issues.append(f"subject longer than {MAX_SUBJECT_CHARS} characters")
    if len(copy.subject.split()) > MAX_SUBJECT_WORDS:
        issues.append(f"subject longer than {MAX_SUBJECT_WORDS} words (reads like a headline)")
    if len(copy.opener.split()) > MAX_OPENER_WORDS:
        issues.append(f"opener longer than {MAX_OPENER_WORDS} words")

    for text, label in ((copy.opener, "opener"), (copy.subject, "subject")):
        for m in BANNED.finditer(text):
            issues.append(f"{label} uses sales/offer language: {m.group(0)!r}")
        for m in FILLER.finditer(text):
            issues.append(f"{label} uses filler or speculation: {m.group(0)!r}")
        for num in re.findall(r"\d[\d,.]*\d|\d", text):
            if num not in source_numbers:
                issues.append(f"{label} states {num!r}, which isn't in the article")
        # Capitalised words mid-sentence are names; they must come from the article.
        for sentence in re.split(r"(?<=[.!?:])\s+", text):
            words = re.findall(r"[A-Za-z][\w&'’-]*", sentence)
            for w in words[1:]:
                word = _word(w.lower())
                if w[0].isupper() and word not in source_words and word not in allowed:
                    issues.append(f"{label} mentions {w!r}, which isn't in the article")

    if not copy.product_focus:
        issues.append("no valid product focus")
    return list(dict.fromkeys(issues))


def humanize(copy: DraftCopy) -> DraftCopy:
    """Remove the tells of machine-written text that the prompt doesn't always prevent.

    Dashes become commas; a headline-style subject ("New office - IT considerations")
    keeps only its first part.
    """
    copy.opener = re.sub(r"\s*[—–]\s*|\s+-\s+", ", ", copy.opener.strip())
    copy.opener = re.sub(r",\s*,", ",", copy.opener)
    subject = re.split(r"\s+[-—–]\s+|\s*[—–]\s*|:\s+", copy.subject.strip())[0]
    copy.subject = subject.rstrip(".!?, ")
    return copy


def _website_display(url: str) -> str:
    """'https://fosutog.com/' → 'fosutog.com', the way people write it in a signature."""
    return (urlparse(url if "://" in url else f"https://{url}").hostname or url).removeprefix("www.")


def render(copy: DraftCopy, company: Company, contact: Contact, signal: Signal, sender: dict) -> tuple[str, str]:
    """A short, plain email a person would write: one ask, a normal signature, a friendly opt-out."""
    if contact.kind == "verified" and contact.name:
        greeting = f"Hi {contact.name.split()[0]},"
    else:
        greeting = "Hi there,"

    focus = join_naturally([spoken(p) for p in copy.product_focus])
    body = (
        f"{greeting}\n\n"
        f"{copy.opener.strip()}\n\n"
        f"We're {sender['name']}. We supply {focus} to companies across the UAE. "
        f"Would it help if we put together a few options and a quick quote for you?\n\n"
        f"Best regards,\n"
        f"{sender['signature']}\n"
        f"{sender['phone']}\n"
        f"{_website_display(sender['website'])}\n\n"
        f"{OPT_OUT}"
    )
    return copy.subject.strip(), body


def generate_draft(session: Session, company: Company, llm: LLM = ollama_json,
                   client: httpx.Client | None = None) -> Draft | None:
    contact = best_contact(company)
    signal = lead_signal(company)
    if contact is None or signal is None:
        return None

    sender = sender_details()
    title = _clean_title(signal.title)
    # If the article names the equipment (a tender, say), pitch exactly that.
    named = mentioned_products(f"{title} {signal.evidence or ''}")
    labels = named or (list(company.score.product_lines) if company.score and company.score.product_lines
                       else ["Laptops & desktops", "Monitors, docks & peripherals"])
    prompt = PROMPT.format(sender=sender["name"], company=display_name(company.name), title=title,
                           evidence=signal.evidence or title, labels="; ".join(labels))

    copy = llm(prompt, DraftCopy, client)
    if copy is None:
        return None
    if named:
        copy.product_focus = named[:2]
    else:
        # Keep only product labels we offer for this signal; fall back to the top ones.
        by_lower = {l.lower(): l for l in labels}
        copy.product_focus = [by_lower[p.lower()] for p in copy.product_focus if p.lower() in by_lower][:2] or labels[:2]

    sources = [signal.title, signal.evidence or "", company.name]
    copy = humanize(copy)
    copy.subject = sentence_case(copy.subject, sources)
    issues = check_copy(copy, sources, labels)
    if any(m in str(sender) for m in PLACEHOLDER_MARKERS):
        issues.append("sender details in config/products.yaml are still placeholders")

    subject, body = render(copy, company, contact, signal, sender)
    draft = Draft(company_id=company.id, to_email=contact.email, subject=subject, body=body,
                  signal_url=signal.url, status="needs_review" if issues else "draft", issues=issues)
    session.add(draft)
    session.flush()
    return draft


def active_draft(session: Session, company_id: int) -> Draft | None:
    return session.scalar(select(Draft).where(Draft.company_id == company_id, Draft.status.in_(ACTIVE))
                          .order_by(Draft.created_at.desc()))


def draft_leads(session: Session, limit: int = 10, company_ids: list[int] | None = None,
                regenerate: bool = False, llm: LLM = ollama_json) -> list[dict]:
    """Draft emails for high-route leads that have a contact and no active draft."""
    q = select(Company).join(Score).where(Score.route == "high").order_by(Score.priority.desc())
    if company_ids is not None:
        q = q.where(Company.id.in_(company_ids))

    client = httpx.Client()
    report = []
    for company in session.scalars(q).all():
        if len(report) >= limit:
            break
        existing = active_draft(session, company.id)
        if existing and (existing.status == "approved" or not regenerate):
            continue
        if existing:
            existing.status, existing.reviewer_note = "rejected", "superseded by a regenerated draft"

        draft = generate_draft(session, company, llm, client)
        session.commit()
        if draft is None:
            report.append({"company": company.name, "error": "no contact, no signal, or the LLM call failed"})
        else:
            report.append(draft_summary(draft))
    return report


def draft_summary(d: Draft) -> dict:
    return {"draft_id": d.id, "company_id": d.company_id, "company": d.company.name, "to": d.to_email,
            "subject": d.subject, "status": d.status, "issues": d.issues}


def draft_detail(d: Draft) -> dict:
    return draft_summary(d) | {"body": d.body, "signal_url": d.signal_url, "reviewer_note": d.reviewer_note}


def list_drafts(session: Session, status: str | None = None) -> list[dict]:
    q = select(Draft).order_by(Draft.created_at.desc())
    q = q.where(Draft.status == status) if status else q.where(Draft.status.in_(("draft", "needs_review")))
    return [draft_summary(d) for d in session.scalars(q)]


def edit_draft(session: Session, draft_id: int, subject: str | None = None, body: str | None = None,
               to_email: str | None = None) -> Draft:
    d = _pending(session, draft_id)
    changed = False
    if subject is not None and subject.strip() != d.subject:
        d.subject, changed = subject.strip(), True
    if body is not None and body.replace("\r\n", "\n").strip() != d.body:
        d.body, changed = body.replace("\r\n", "\n").strip(), True
    if to_email is not None and to_email.strip().lower() != d.to_email:
        d.to_email, changed = RawContact(email=to_email, kind="verified").email, True  # validates the address
    if changed:
        d.reviewer_note = "edited by reviewer"
    session.commit()
    return d


def unapprove_draft(session: Session, draft_id: int) -> Draft:
    """Move an approved draft back to review, e.g. to edit it before sending."""
    d = session.get(Draft, draft_id)
    if d is None:
        raise ValueError(f"no draft with id {draft_id}")
    if d.status != "approved":
        raise ValueError(f"draft {draft_id} is {d.status}, not approved")
    d.status, d.reviewed_at = "draft", None
    session.commit()
    return d


def approve_draft(session: Session, draft_id: int, note: str | None = None) -> Draft:
    d = _pending(session, draft_id)
    if any(m in d.body for m in PLACEHOLDER_MARKERS):
        raise ValueError("can't approve: the email still contains placeholder company details. "
                         "Fill in config/products.yaml and regenerate, or edit the body")
    if not any(m in d.body.lower() for m in OPT_OUT_MARKERS):
        raise ValueError("can't approve: the email must keep the opt-out line (the P.S.)")
    d.status, d.reviewer_note, d.reviewed_at = "approved", note or d.reviewer_note, datetime.now(timezone.utc)
    session.commit()
    return d


def reject_draft(session: Session, draft_id: int, reason: str) -> Draft:
    d = _pending(session, draft_id)
    d.status, d.reviewer_note, d.reviewed_at = "rejected", reason, datetime.now(timezone.utc)
    session.commit()
    return d


def _pending(session: Session, draft_id: int) -> Draft:
    d = session.get(Draft, draft_id)
    if d is None:
        raise ValueError(f"no draft with id {draft_id}")
    if d.status not in ("draft", "needs_review"):
        raise ValueError(f"draft {draft_id} is already {d.status}")
    return d
