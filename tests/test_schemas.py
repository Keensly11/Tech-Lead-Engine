import pytest
from pydantic import ValidationError

from lead_engine.extract import _evidence_is_grounded
from lead_engine.schemas import NewsExtraction, RawContact


def test_llm_output_with_unknown_signal_type_is_rejected():
    with pytest.raises(ValidationError):
        NewsExtraction.model_validate({"is_relevant": True, "company_name": "X", "signal_type": "buys_stuff"})


def test_llm_confidence_out_of_range_is_rejected():
    with pytest.raises(ValidationError):
        NewsExtraction.model_validate({"is_relevant": True, "confidence": 1.7})


def test_contact_email_validation():
    assert RawContact(email=" Sara@Acme.AE ", kind="verified").email == "sara@acme.ae"
    with pytest.raises(ValidationError):
        RawContact(email="not-an-email", kind="generic")


def test_evidence_must_appear_in_source():
    text = "Acme LLC will open a new 200-seat office in Dubai Marina next month."
    assert _evidence_is_grounded("Acme LLC will open a new 200-seat office", text)
    assert not _evidence_is_grounded("Acme LLC is buying 500 laptops", text)
    assert not _evidence_is_grounded(None, text)
