"""Service-Area classifier (M16 P1, Capability A).

Covers the four verdicts, the EITHER-signal in-area rule, the false-accept
bias (ambiguous never declines), and town/postal normalization drift.
"""

from src.lib.service_area import classify_service_area


def _zones(postal_codes=None, cities=None):
    return [{"postal_codes": postal_codes or [], "cities": cities or []}]


# ── unconfigured (gate off) ──────────────────────────────────────────────────

def test_no_zones_is_unconfigured():
    assert classify_service_area(zones=None, postal_code="90210")["verdict"] == "unconfigured"


def test_empty_zones_is_unconfigured():
    assert classify_service_area(zones=[], postal_code="90210")["verdict"] == "unconfigured"


def test_zone_rows_with_only_empty_lists_is_unconfigured():
    z = _zones(postal_codes=[], cities=[])
    assert classify_service_area(zones=z, postal_code="90210", locality="Beverly Hills")["verdict"] == "unconfigured"


# ── in_area (postal OR town) ─────────────────────────────────────────────────

def test_postal_match_in_area():
    z = _zones(postal_codes=["90210", "90211"])
    r = classify_service_area(zones=z, postal_code="90210", locality="Nowhere")
    assert r["verdict"] == "in_area"
    assert r["matched_on"] == "postal"


def test_town_match_in_area_even_when_postal_absent_from_list():
    # Forgotten ZIP but listed town → still in-area (match on EITHER signal).
    z = _zones(postal_codes=["90210"], cities=["Santa Monica"])
    r = classify_service_area(zones=z, postal_code="90405", locality="Santa Monica")
    assert r["verdict"] == "in_area"
    assert r["matched_on"] == "city"


def test_postal_wins_when_both_present():
    z = _zones(postal_codes=["90210"], cities=["Santa Monica"])
    r = classify_service_area(zones=z, postal_code="90210", locality="Santa Monica")
    assert r["matched_on"] == "postal"


def test_union_across_multiple_zone_rows():
    zones = [
        {"postal_codes": ["10001"], "cities": ["Manhattan"]},
        {"postal_codes": ["11201"], "cities": ["Brooklyn"]},
    ]
    assert classify_service_area(zones=zones, postal_code="11201")["verdict"] == "in_area"
    assert classify_service_area(zones=zones, locality="Brooklyn")["verdict"] == "in_area"


# ── out_of_area (configured + trusted signal + no match) ─────────────────────

def test_postal_not_in_list_is_out_of_area():
    z = _zones(postal_codes=["90210", "90211"])
    r = classify_service_area(zones=z, postal_code="60601", locality="Chicago")
    assert r["verdict"] == "out_of_area"
    assert r["matched_on"] is None


def test_town_not_in_list_and_no_postal_is_out_of_area():
    z = _zones(cities=["Boston", "Cambridge"])
    assert classify_service_area(zones=z, locality="Chicago")["verdict"] == "out_of_area"


def test_neither_postal_nor_town_match_is_out_of_area():
    z = _zones(postal_codes=["90210"], cities=["Beverly Hills"])
    assert classify_service_area(zones=z, postal_code="60601", locality="Chicago")["verdict"] == "out_of_area"


# ── unknown (configured but nothing to check) — bias to accept ───────────────

def test_configured_but_no_signal_is_unknown():
    z = _zones(postal_codes=["90210"], cities=["Beverly Hills"])
    r = classify_service_area(zones=z, postal_code=None, locality=None)
    assert r["verdict"] == "unknown"


def test_blank_signals_are_unknown_not_out_of_area():
    z = _zones(postal_codes=["90210"])
    assert classify_service_area(zones=z, postal_code="", locality="   ")["verdict"] == "unknown"


# ── normalization drift ──────────────────────────────────────────────────────

def test_town_case_and_punctuation_insensitive():
    z = _zones(cities=["St. Louis"])
    assert classify_service_area(zones=z, locality="st louis")["verdict"] == "in_area"


def test_town_whitespace_collapse():
    z = _zones(cities=["New York"])
    assert classify_service_area(zones=z, locality="  new   york ")["verdict"] == "in_area"


def test_postal_space_insensitive_for_ca_uk():
    z = _zones(postal_codes=["K1A 0B1"])
    assert classify_service_area(zones=z, postal_code="k1a0b1")["verdict"] == "in_area"


def test_postal_us_zip_exact():
    z = _zones(postal_codes=["90210"])
    assert classify_service_area(zones=z, postal_code="90210")["verdict"] == "in_area"
    assert classify_service_area(zones=z, postal_code="90211")["verdict"] == "out_of_area"
