"""Contact finder: company name → verified website → public business emails.

1. Domain. Use the known domain, or guess candidates from the name (braindigits.com,
   brain-digits.ae, …) and accept one only if its homepage <title> contains the
   company name and it isn't a parked / for-sale page. Single-word names are not
   guessed: "EQT" → eqt.com is a different EQT. Those need set_domain by hand.
2. Crawl at most MAX_PAGES pages on that site only (home, contact, about, team),
   honouring robots.txt, with a polite delay between requests.
3. Extract emails (mailto links, plain text, "[at]" obfuscation, Cloudflare
   email protection). Keep only addresses on the company's own domain; drop
   careers/HR/no-reply/privacy inboxes. Personal emails are never guessed.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from lead_engine.resolve import normalize_domain, normalize_name
from lead_engine.schemas import RawContact

log = logging.getLogger(__name__)

USER_AGENT = "TechLeadEngineBot/0.1 (B2B contact finder; honours robots.txt)"
MAX_PAGES = 6
REQUEST_DELAY = 0.5  # seconds between page requests to the same site
TLDS = [".com", ".ae", ".com.ae", ".io", ".co", ".net"]
# Dropped to form a second, shorter candidate: "Qamar Cloud Technologies" → qamarcloud.com
GENERIC_NAME_WORDS = {
    "group", "holding", "holdings", "technologies", "technology", "tech", "studios", "studio",
    "solutions", "international", "global", "partners", "ventures", "media", "productions",
    "pictures", "films", "events", "capital", "the", "and",
}
CONTACT_PATH_HINTS = ("contact", "about", "team", "people", "leadership", "get-in-touch", "reach-us")
FALLBACK_PATHS = ("/contact", "/contact-us", "/about", "/about-us")
PARKED_MARKERS = (
    "domain is for sale", "buy this domain", "this domain may be for sale", "domain for sale",
    "parked free", "hugedomains", "sedo domain parking", "dan.com", "afternic",
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_SITE_RE = re.compile(r"""<meta[^>]+property=["']og:(?:site_name|title)["'][^>]+content=["']([^"']+)["']""", re.I)
CFEMAIL_RE = re.compile(r"""data-cfemail=["']([0-9a-fA-F]+)["']""")

# Shared inboxes. Useful, but weaker than a named person's address.
GENERIC_LOCALS = {
    "info", "hello", "hi", "contact", "contactus", "sales", "enquiries", "enquiry", "inquiries",
    "inquiry", "admin", "office", "mail", "support", "team", "general", "marketing", "business",
    "procurement", "purchasing", "it", "bd", "partnerships", "uae", "dubai", "abudhabi",
}
# Role labels for generic inboxes that matter to an IT/AV seller.
ROLE_HINTS = {"procurement": "Procurement", "purchasing": "Procurement", "it": "IT", "sales": "Sales",
              "admin": "Administration", "office": "Office"}
# Never contact these.
EXCLUDED_LOCALS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "privacy", "dpo", "gdpr", "legal", "abuse",
    "postmaster", "webmaster", "hostmaster", "unsubscribe", "careers", "career", "jobs", "job",
    "hr", "recruitment", "recruiting", "talent", "cv", "resume", "press", "investor", "investors",
}
FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")


@dataclass
class ContactSearch:
    domain: str | None = None
    domain_source: str | None = None  # known | guessed
    pages_checked: list[str] = field(default_factory=list)
    contacts: list[RawContact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------- domain discovery ----------

def candidate_domains(name: str) -> list[str]:
    tokens = normalize_name(name).split()
    tokens = [re.sub(r"[^a-z0-9-]", "", t) for t in tokens]
    tokens = [t for t in tokens if t]
    if len(tokens) < 2:
        return []  # single-word names are too ambiguous to guess

    stems = ["".join(tokens), "-".join(tokens)]
    core = [t for t in tokens if t not in GENERIC_NAME_WORDS]
    if core and core != tokens:
        stems.insert(1, "".join(core))
    seen, out = set(), []
    for stem in stems:
        for tld in TLDS:
            d = stem + tld
            if d not in seen:
                seen.add(d)
                out.append(d)
    return out


def page_title(html: str) -> str:
    parts = TITLE_RE.findall(html) + OG_SITE_RE.findall(html)
    return unescape(" ".join(parts))


def is_parked(html: str) -> bool:
    text = html.lower()
    return any(m in text for m in PARKED_MARKERS)


def title_matches(name: str, html: str) -> bool:
    """Every token of the company name appears in the page title (or the joined form does)."""
    tokens = normalize_name(name).split()
    title = page_title(html).lower()
    title_tokens = set(re.findall(r"[a-z0-9]+", title))
    joined = "".join(tokens)
    return bool(tokens) and (all(t in title_tokens for t in tokens) or joined in title.replace(" ", ""))


def _fetch(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        return client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=10)
    except httpx.HTTPError:
        return None


def _is_ok_html(resp: httpx.Response | None) -> bool:
    return resp is not None and resp.status_code == 200 and "html" in resp.headers.get("content-type", "html")


def _get(client: httpx.Client, url: str) -> httpx.Response | None:
    resp = _fetch(client, url)
    return resp if _is_ok_html(resp) else None


def is_bot_challenge(resp: httpx.Response) -> bool:
    """Cloudflare-style interstitials. We never try to get past these."""
    if resp.status_code not in (403, 429, 503):
        return False
    text = resp.text[:5000].lower()
    return any(m in text for m in ("just a moment", "cf-chl", "captcha", "attention required", "access denied"))


def verify_domain(client: httpx.Client, name: str, domain: str,
                  blocked: list[str] | None = None) -> tuple[str, str] | None:
    """Fetch the homepage. Returns (final_domain, html) if it is really this company's site.

    Sites that answer with a bot challenge are appended to `blocked`: they may
    well be the right site, but a person has to check them.
    """
    for url in (f"https://{domain}", f"https://www.{domain}"):
        resp = _fetch(client, url)
        if resp is not None and is_bot_challenge(resp):
            if blocked is not None:
                blocked.append(normalize_domain(str(resp.url)) or domain)
            return None
        if not _is_ok_html(resp):
            continue
        html = resp.text
        if is_parked(html) or not title_matches(name, html):
            return None
        return normalize_domain(str(resp.url)) or domain, html
    return None


# ---------- email extraction ----------

def decode_cfemail(hex_str: str) -> str:
    """Cloudflare email protection: first byte is the XOR key for the rest."""
    data = bytes.fromhex(hex_str)
    return "".join(chr(b ^ data[0]) for b in data[1:])


def extract_emails(html: str) -> set[str]:
    text = unescape(html)
    text = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", text, flags=re.I)
    found = {m.group(0) for m in EMAIL_RE.finditer(text)}
    for hex_str in CFEMAIL_RE.findall(html):
        try:
            found.add(decode_cfemail(hex_str))
        except ValueError:
            pass
    return {e.strip(".").lower() for e in found}


def classify(email: str, domain: str) -> tuple[str, str | None] | None:
    """Return (kind, role) for a usable company email, or None to discard it."""
    if email.endswith(FILE_SUFFIXES) or "@" not in email:
        return None
    local, email_domain = email.rsplit("@", 1)
    if not (email_domain == domain or email_domain.endswith("." + domain)):
        return None  # third-party address (agency, web developer, gmail…)
    base = re.split(r"[.+_-]", local)[0]
    if local in EXCLUDED_LOCALS or base in EXCLUDED_LOCALS:
        return None
    if local in GENERIC_LOCALS or base in GENERIC_LOCALS:
        return "generic", ROLE_HINTS.get(base)
    return "verified", None  # a named person's address published on the company's own site


# ---------- crawl ----------

def _robots(client: httpx.Client, domain: str) -> RobotFileParser:
    rp = RobotFileParser()
    try:
        resp = client.get(f"https://{domain}/robots.txt", headers={"User-Agent": USER_AGENT},
                          follow_redirects=True, timeout=10)
        rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
    except httpx.HTTPError:
        rp.parse([])
    return rp


def _contact_links(html: str, base_url: str, domain: str) -> list[str]:
    links = []
    for href in HREF_RE.findall(html):
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        host = normalize_domain(url)
        if parsed.scheme not in ("http", "https") or host != domain:
            continue
        if any(h in parsed.path.lower() for h in CONTACT_PATH_HINTS):
            links.append(url.split("#")[0])
    return links


def find_contacts(name: str, domain: str | None = None, client: httpx.Client | None = None,
                  delay: float | None = None) -> ContactSearch:
    client = client or httpx.Client()
    delay = REQUEST_DELAY if delay is None else delay
    result = ContactSearch()
    homepage_html = None

    if domain:
        result.domain, result.domain_source = domain, "known"
    else:
        candidates = candidate_domains(name)
        if not candidates:
            result.notes.append("single-word name: too ambiguous to guess a website; set the domain manually")
            return result
        blocked: list[str] = []
        for cand in candidates:
            verified = verify_domain(client, name, cand, blocked)
            if verified:
                result.domain, homepage_html = verified
                result.domain_source = "guessed"
                result.notes.append(f"website {result.domain} verified: homepage title names the company")
                break
        if not result.domain:
            if blocked:
                result.notes.append(f"possible website(s) block automated access: {', '.join(dict.fromkeys(blocked))}. "
                                    "Check by hand, then use set_domain / add_contact")
            else:
                result.notes.append(f"no website found among {len(candidates)} candidates")
            return result

    robots = _robots(client, result.domain)
    home = f"https://{result.domain}/"
    queue = [home] + [f"https://{result.domain}{p}" for p in FALLBACK_PATHS]
    seen: set[str] = set()
    emails: dict[str, str] = {}  # email → page it was found on

    while queue and len(result.pages_checked) < MAX_PAGES:
        url = queue.pop(0)
        key = url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        if not robots.can_fetch(USER_AGENT, url):
            result.notes.append(f"robots.txt disallows {url}")
            continue

        if url == home and homepage_html is not None:
            html, final_url = homepage_html, home
        else:
            if result.pages_checked:
                time.sleep(delay)
            resp = _get(client, url)
            if resp is None:
                continue
            html, final_url = resp.text, str(resp.url)
        result.pages_checked.append(final_url)

        for email in extract_emails(html):
            emails.setdefault(email, final_url)
        # Links found on the page are more reliable than the fallback paths, so they go first.
        queue[:0] = [u for u in _contact_links(html, final_url, result.domain) if u.rstrip("/").lower() not in seen]

    for email, source in sorted(emails.items()):
        c = classify(email, result.domain)
        if c:
            kind, role = c
            result.contacts.append(RawContact(email=email, kind=kind, role=role, source_url=source))
    result.notes.append(f"{len(result.contacts)} usable email(s) from {len(result.pages_checked)} page(s)")
    return result
