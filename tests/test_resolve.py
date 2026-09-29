from lead_engine.resolve import names_match, normalize_domain, normalize_name


def test_normalize_domain():
    assert normalize_domain("https://www.Acme.ae/about?x=1") == "acme.ae"
    assert normalize_domain("acme.ae") == "acme.ae"
    assert normalize_domain("sara@acme.ae") == "acme.ae"
    assert normalize_domain("someone@gmail.com") is None
    assert normalize_domain("") is None
    assert normalize_domain("not a domain") is None


def test_normalize_name_strips_uae_legal_suffixes():
    assert normalize_name("ACME Technologies FZ-LLC.") == "acme technologies"
    assert normalize_name("Duneline Logistics L.L.C") == "duneline logistics"
    assert normalize_name("Qamar Cloud Technologies FZCO") == "qamar cloud technologies"


def test_names_match_variants_but_not_different_companies():
    assert names_match("Falcon Pixel Studios FZ-LLC", "falcon pixel studios")
    assert names_match("Technologies Qamar Cloud", "Qamar Cloud Technologies")
    assert not names_match("Falcon Pixel Studios", "Falcon Logistics")
