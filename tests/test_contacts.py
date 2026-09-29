"""Contact finder against a fake website (no network)."""

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lead_engine import contacts, pipeline
from lead_engine.contacts import candidate_domains, classify, extract_emails, find_contacts
from lead_engine.models import Base, Company


def cf_encode(email: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


HOME = """<html><head><title>Brain Digits | AI Transformation</title></head>
<body><a href="/contact-us">Contact</a> <a href="/private/team">Team</a>
<a href="https://twitter.com/braindigits">Twitter</a></body></html>"""

CONTACT = f"""<html><head><title>Contact - Brain Digits</title></head><body>
<a href="mailto:info@braindigits.com">info@braindigits.com</a>
<p>Sara Khan, Head of IT: sara.khan@braindigits.com</p>
<p>Sales: sales [at] braindigits [dot] com</p>
<a href="/cdn-cgi/l/email-protection" data-cfemail="{cf_encode('procurement@braindigits.com')}">[email protected]</a>
<p>Jobs: careers@braindigits.com, no-reply@braindigits.com</p>
<p>Site by hello@webagency.example</p>
<img src="logo@2x.png">
</body></html>"""

PARKED = "<html><head><title>braindigits.ae</title></head><body>This domain is for sale!</body></html>"

ROBOTS = "User-agent: *\nDisallow: /private/\n"


def site(pages: dict[str, tuple[int, str]]):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).rstrip("/")
        requested.append(url)
        status, body = pages.get(url, (404, "not found"))
        ctype = "text/plain" if url.endswith("robots.txt") else "text/html"
        return httpx.Response(status, text=body, headers={"content-type": ctype})

    return httpx.Client(transport=httpx.MockTransport(handler)), requested


BRAIN_DIGITS = {
    "https://braindigits.com": (200, HOME),
    "https://braindigits.com/contact-us": (200, CONTACT),
    "https://braindigits.com/robots.txt": (200, ROBOTS),
}


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(contacts, "REQUEST_DELAY", 0)


def test_candidate_domains():
    c = candidate_domains("Qamar Cloud Technologies FZCO")
    assert c[0] == "qamarcloudtechnologies.com"
    assert "qamarcloud.com" in c and "qamarcloud.ae" in c and "qamar-cloud-technologies.com" in c


def test_single_word_names_are_not_guessed():
    assert candidate_domains("EQT") == []
    result = find_contacts("EQT", client=site({})[0])
    assert result.domain is None
    assert "set the domain manually" in result.notes[0]


def test_extract_emails_handles_obfuscation_and_cloudflare():
    emails = extract_emails(CONTACT)
    assert {"info@braindigits.com", "sara.khan@braindigits.com", "sales@braindigits.com",
            "procurement@braindigits.com"} <= emails


@pytest.mark.parametrize("email,expected", [
    ("info@braindigits.com", ("generic", None)),
    ("procurement@braindigits.com", ("generic", "Procurement")),
    ("sara.khan@braindigits.com", ("verified", None)),
    ("sara@uae.braindigits.com", ("verified", None)),      # subdomain of the company is fine
    ("careers@braindigits.com", None),
    ("no-reply@braindigits.com", None),
    ("hello@webagency.example", None),                      # third party
    ("logo@2x.png", None),
])
def test_classify(email, expected):
    assert classify(email, "braindigits.com") == expected


def test_finds_site_and_contacts_by_guessing_domain():
    client, requested = site(BRAIN_DIGITS)
    result = find_contacts("Brain Digits", client=client)

    assert result.domain == "braindigits.com"
    assert result.domain_source == "guessed"
    found = {c.email: c.kind for c in result.contacts}
    assert found == {
        "info@braindigits.com": "generic",
        "procurement@braindigits.com": "generic",
        "sales@braindigits.com": "generic",
        "sara.khan@braindigits.com": "verified",
    }
    assert all(c.source_url == "https://braindigits.com/contact-us" for c in result.contacts)
    # robots.txt disallows /private/, and off-site links are never followed
    assert not any("/private/" in u for u in requested)
    assert not any("twitter.com" in u for u in requested)


def test_parked_domain_is_rejected():
    client, _ = site({"https://braindigits.com": (200, PARKED), "https://www.braindigits.com": (200, PARKED)})
    assert find_contacts("Brain Digits", client=client).domain is None


def test_bot_protected_site_is_reported_not_bypassed():
    challenge = "<html><head><title>Just a moment...</title></head><body>cf-chl</body></html>"
    client, requested = site({"https://braindigits.com": (403, challenge)})
    result = find_contacts("Brain Digits", client=client)
    assert result.domain is None
    assert "block automated access: braindigits.com" in result.notes[-1]
    assert requested.count("https://braindigits.com") == 1  # no retries against the challenge


def test_site_with_different_company_name_is_rejected():
    other = "<html><head><title>Brain Health Clinic</title></head></html>"
    client, _ = site({"https://braindigits.com": (200, other)})
    assert find_contacts("Brain Digits", client=client).domain is None


def test_page_limit(monkeypatch):
    many_links = "<title>Brain Digits</title>" + "".join(f'<a href="/about/p{i}">x</a>' for i in range(20))
    pages = {"https://braindigits.com": (200, many_links)}
    pages.update({f"https://braindigits.com/about/p{i}": (200, "<p>nothing</p>") for i in range(20)})
    client, _ = site(pages)
    result = find_contacts("Brain Digits", client=client)
    assert len(result.pages_checked) == contacts.MAX_PAGES


# ---------- pipeline integration ----------

@pytest.fixture
def session(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        pipeline.run(s, ["sample"])
        yield s


def test_enrich_promotes_review_lead_to_high(session):
    qamar = session.scalar(select(Company).where(Company.domain == "qamarcloud.example"))
    assert qamar.score.route == "review"  # strong intent, no contact

    client, _ = site({
        "https://qamarcloud.example": (200, "<title>Qamar Cloud</title><a href='/contact'>c</a>"),
        "https://qamarcloud.example/contact": (200, "Talk to us: sales@qamarcloud.example"),
    })
    report = pipeline.find_contacts_for_leads(session, company_ids=[qamar.id], client=client)

    assert report[0]["emails"] == ["sales@qamarcloud.example (generic)"]
    assert qamar.score.route == "high"
    assert qamar.contact_checked_at is not None
    # searched recently → skipped next time
    assert pipeline.leads_needing_contacts(session, company_ids=[qamar.id]) == []


def test_set_domain_refuses_another_companys_domain(session):
    qamar = session.scalar(select(Company).where(Company.domain == "qamarcloud.example"))
    other = session.scalar(select(Company).where(Company.domain == "brightwave.example"))
    error = pipeline.set_domain(session, other, "https://www.qamarcloud.example/about", "manual")
    assert "already belongs" in error and str(qamar.id) in error
