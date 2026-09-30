"""Email drafting with a fake LLM (deterministic, no Ollama needed)."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lead_engine import drafting, pipeline
from lead_engine.drafting import best_contact, check_copy
from lead_engine.models import Base, Company, Draft
from lead_engine.schemas import DraftCopy

REAL_SENDER = {"name": "Gulf Tech Supply", "website": "https://gulftech.example", "phone": "+971 4 123 4567",
               "email": "sales@gulftech.example", "signature": "Gulf Tech Supply Sales"}

PRODUCTS = ["Cameras, lenses & AV equipment", "Editing & rendering workstations", "Laptops & desktops"]
SOURCE = ["Falcon Pixel Studios begins filming feature series at new Abu Dhabi soundstage",
          "Falcon Pixel Studios has started principal photography on an eight-part series.",
          "Falcon Pixel Studios FZ-LLC"]


def copy(opener="Congratulations on starting the feature series at your new Abu Dhabi soundstage. "
                "Productions like this usually need reliable edit and camera kit.",
         subject="Kit for your new Abu Dhabi soundstage",
         focus=("Cameras, lenses & AV equipment", "Editing & rendering workstations")) -> DraftCopy:
    return DraftCopy(opener=opener, subject=subject, product_focus=list(focus))


def fake_llm(result: DraftCopy | None = None):
    calls = []

    def llm(prompt, model, client):
        calls.append(prompt)
        return result if result is not None else copy()

    llm.calls = calls
    return llm


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(drafting, "sender_details", lambda: REAL_SENDER)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        pipeline.run(s, ["sample"])
        yield s


def falcon(s) -> Company:
    return s.scalar(select(Company).where(Company.domain == "falconpixel.example"))


# ---------- fact check ----------

def test_grounded_copy_passes():
    assert check_copy(copy(), SOURCE, PRODUCTS) == []


def test_possessive_of_a_known_name_is_fine():
    c = copy(opener="Falcon Pixel Studios’ new series in Abu Dhabi sounds exciting.")
    assert check_copy(c, SOURCE, PRODUCTS) == []


@pytest.mark.parametrize("opener,expected", [
    ("Congrats on the 12-episode series at the new soundstage.", "states '12'"),
    ("Congrats on the new soundstage, which Netflix will use.", "mentions 'Netflix'"),
    ("Congrats on the series! We offer a 20% discount this month.", "sales/offer language"),
    ("Congrats on the series. Get free delivery on cameras.", "sales/offer language: 'free'"),
])
def test_ungrounded_or_salesy_copy_is_flagged(opener, expected):
    issues = check_copy(copy(opener=opener), SOURCE, PRODUCTS)
    assert any(expected in i for i in issues), issues


def test_numbers_from_the_article_are_allowed():
    src = ["Northwind Academy tender: supply of 400 student laptops"]
    c = copy(opener="Saw the tender for 400 student laptops.", subject="Your laptop tender")
    assert check_copy(c, src, PRODUCTS) == []


@pytest.mark.parametrize("subject,expected", [
    ("New HQ In Dubai South - IT Infrastructure Considerations",
     "New HQ in Dubai South - IT infrastructure considerations"),
    ("Expansion In Abu Dhabi - Infrastructure Considerations",
     "Expansion in Abu Dhabi - infrastructure considerations"),
    ("Kit for your new soundstage", "Kit for your new soundstage"),
    ("Expansion In Abu Dhabi: Next Steps", "Expansion in Abu Dhabi: next steps"),
])
def test_sentence_case_keeps_names_and_acronyms(subject, expected):
    sources = ["Duneline Logistics moves HQ to 3-floor office in Dubai South",
               "Brain Digits opens new office in Abu Dhabi"]
    assert drafting.sentence_case(subject, sources) == expected


def test_title_case_subject_is_not_flagged(session):
    llm = fake_llm(copy(subject="Kit For Your New Abu Dhabi Soundstage"))
    report = drafting.draft_leads(session, company_ids=[falcon(session).id], llm=llm)
    assert report[0]["subject"] == "Kit for your new Abu Dhabi soundstage"
    assert report[0]["issues"] == []


# ---------- generation ----------

def test_best_contact_prefers_named_person(session):
    assert best_contact(falcon(session)).email == "maya.r@falconpixel.example"


def test_generate_draft_uses_template_and_signal(session):
    llm = fake_llm()
    report = drafting.draft_leads(session, company_ids=[falcon(session).id], llm=llm)

    d = session.get(Draft, report[0]["draft_id"])
    assert d.status == "draft" and d.issues == []
    assert d.to_email == "maya.r@falconpixel.example"
    assert d.body.startswith("Hi Maya,")
    assert "We supply cameras, lenses and AV gear, plus editing workstations to companies" in d.body
    # a normal signature, not a Website:/Phone:/Email: block, and a friendly P.S. instead of a footer
    assert d.body.endswith(
        "Best regards,\nGulf Tech Supply Sales\n+971 4 123 4567\ngulftech.example\n\n" + drafting.OPT_OUT)
    assert "Website:" not in d.body and "unsubscribe" not in d.body.lower() and "---" not in d.body
    # the prompt is grounded in the strongest signal, with the publisher suffix stripped
    assert "begins filming feature series at new Abu Dhabi soundstage" in llm.calls[0]


@pytest.mark.parametrize("opener,subject,exp_opener,exp_subject", [
    ("Saw your new office — congrats. Offices need laptops.", "New Abu Dhabi office - IT infrastructure considerations",
     "Saw your new office, congrats. Offices need laptops.", "New Abu Dhabi office"),
    ("Saw the news – nice one.", "Expansion in Abu Dhabi: next steps", "Saw the news, nice one.", "Expansion in Abu Dhabi"),
    ("A well-known firm.", "your new office.", "A well-known firm.", "your new office"),
])
def test_humanize_removes_dashes_and_headline_subjects(opener, subject, exp_opener, exp_subject):
    c = drafting.humanize(copy(opener=opener, subject=subject))
    assert (c.opener, c.subject) == (exp_opener, exp_subject)


def test_headline_length_subject_is_flagged():
    c = copy(subject="Supporting the next phase of growth at your brand new Abu Dhabi soundstage")
    assert any("words" in i for i in check_copy(c, SOURCE, PRODUCTS))


@pytest.mark.parametrize("items,expected", [
    (["servers and networking gear"], "servers and networking gear"),
    (["laptops", "monitors"], "laptops and monitors"),
    (["laptops and desktops", "servers and networking gear"], "laptops and desktops, plus servers and networking gear"),
])
def test_join_naturally(items, expected):
    assert drafting.join_naturally(items) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://fosutog.com", "fosutog.com"), ("https://www.fosutog.com/", "fosutog.com"), ("fosutog.com", "fosutog.com")])
def test_website_display(url, expected):
    assert drafting._website_display(url) == expected


def test_filler_and_speculation_are_flagged():
    c = copy(opener="Congrats on the new Abu Dhabi soundstage, a strategic move. We're excited to see it.")
    issues = check_copy(c, SOURCE, PRODUCTS)
    assert any("'strategic'" in i for i in issues) and any("'excited'" in i for i in issues)


def test_mentioned_products_from_tender_text():
    text = "Northwind Academy tender: supply of 400 student laptops and classroom displays"
    assert drafting.mentioned_products(text) == ["Laptops & desktops", "Monitors, docks & peripherals"]
    assert drafting.mentioned_products("EQT opens Abu Dhabi office") == []
    # whole words only: "average" must not match the "av" keyword
    assert drafting.mentioned_products("an average expansion") == []


def test_tender_draft_pitches_what_the_tender_names(session):
    northwind = session.scalar(select(Company).where(Company.name == "Northwind Academy"))
    llm = fake_llm(copy(opener="Saw the tender for 400 student laptops.", subject="Your laptop tender",
                        focus=("Servers, storage & networking",)))  # model picks the wrong line
    report = drafting.draft_leads(session, company_ids=[northwind.id], llm=llm)
    body = session.get(Draft, report[0]["draft_id"]).body
    assert "We supply laptops and desktops, plus monitors and accessories" in body
    assert "servers" not in body
    assert "Products we could offer: Laptops & desktops; Monitors, docks & peripherals" in llm.calls[0]


def test_shared_inbox_gets_a_plain_greeting_and_acronyms_keep_capitals(session):
    duneline = session.scalar(select(Company).where(Company.domain == "duneline.example"))
    report = drafting.draft_leads(session, company_ids=[duneline.id], llm=fake_llm())
    body = session.get(Draft, report[0]["draft_id"]).body
    assert body.startswith("Hi there,")  # "Hello Duneline Logistics L.L.C team" read like a mail merge
    assert "L.L.C" not in body
    assert drafting.label_in_sentence("Cameras, lenses & AV equipment") == "cameras, lenses & AV equipment"


@pytest.mark.parametrize("name,expected", [
    ("Duneline Logistics L.L.C", "Duneline Logistics"),
    ("Falcon Pixel Studios FZ-LLC", "Falcon Pixel Studios"),
    ("Qamar Cloud Technologies FZCO", "Qamar Cloud Technologies"),
    ("The Boring Company", "The Boring Company"),
    ("EQT", "EQT"),
])
def test_display_name(name, expected):
    from lead_engine.resolve import display_name
    assert display_name(name) == expected


def test_clean_title_drops_publisher_suffix():
    assert drafting._clean_title("Brain Digits opens new office in Abu Dhabi - TradeArabia") == \
        "Brain Digits opens new office in Abu Dhabi"
    assert drafting._clean_title("No publisher suffix here") == "No publisher suffix here"


def test_invalid_product_focus_falls_back_to_lead_products(session):
    llm = fake_llm(copy(focus=("Drones",)))
    report = drafting.draft_leads(session, company_ids=[falcon(session).id], llm=llm)
    d = session.get(Draft, report[0]["draft_id"])
    assert "drones" not in d.body.lower()
    assert "We supply laptops and desktops" in d.body


def test_placeholder_sender_is_flagged_and_blocks_approval(session, monkeypatch):
    monkeypatch.setattr(drafting, "sender_details",
                        lambda: REAL_SENDER | {"name": "YOUR COMPANY NAME", "website": "https://example.com"})
    report = drafting.draft_leads(session, company_ids=[falcon(session).id], llm=fake_llm())
    assert report[0]["status"] == "needs_review"
    assert any("placeholders" in i for i in report[0]["issues"])
    with pytest.raises(ValueError, match="placeholder"):
        drafting.approve_draft(session, report[0]["draft_id"])


def test_llm_failure_creates_no_draft(session):
    report = drafting.draft_leads(session, company_ids=[falcon(session).id], llm=lambda p, m, c: None)
    assert "error" in report[0]
    assert session.scalar(select(Draft)) is None


def test_only_high_leads_with_contacts_are_drafted(session):
    report = drafting.draft_leads(session, llm=fake_llm())
    drafted = {r["company"] for r in report}
    assert drafted == {"Falcon Pixel Studios FZ-LLC", "Northwind Academy", "Duneline Logistics L.L.C"}


# ---------- review workflow ----------

def test_drafting_is_idempotent_and_regenerate_supersedes(session):
    fid = [falcon(session).id]
    first = drafting.draft_leads(session, company_ids=fid, llm=fake_llm())
    assert drafting.draft_leads(session, company_ids=fid, llm=fake_llm()) == []

    again = drafting.draft_leads(session, company_ids=fid, regenerate=True, llm=fake_llm())
    old = session.get(Draft, first[0]["draft_id"])
    assert old.status == "rejected" and "superseded" in old.reviewer_note
    assert again[0]["draft_id"] != first[0]["draft_id"]


def test_approve_reject_edit(session):
    report = drafting.draft_leads(session, llm=fake_llm())
    a, b, c = (r["draft_id"] for r in report)

    drafting.edit_draft(session, a, subject="New subject")
    assert drafting.approve_draft(session, a, "ok").status == "approved"
    assert drafting.reject_draft(session, b, "wrong contact").status == "rejected"

    with pytest.raises(ValueError, match="already approved"):
        drafting.approve_draft(session, a)
    # approved drafts are never regenerated
    assert drafting.draft_leads(session, company_ids=[session.get(Draft, a).company_id],
                                regenerate=True, llm=fake_llm()) == []

    drafting.edit_draft(session, c, body="Hello, no opt-out here.")
    with pytest.raises(ValueError, match="opt-out"):
        drafting.approve_draft(session, c)

    pending = {d["draft_id"] for d in drafting.list_drafts(session)}
    assert pending == {c}
