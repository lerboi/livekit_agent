"""
Service-Area classifier for the LiveKit agent (M16 P1, Capability A).

Pure membership check answering ONE question: does the tenant serve the
caller's address at all? Runs at validate_address time on the Google-NORMALIZED
postal code + town (never the caller's raw speech), against the tenant's
coverage list — the union of `postal_codes[]` and `cities[]` across all of the
tenant's `service_zones` rows. The zones are repurposed as one flat coverage
list (the dashboard now presents a single "Service Area"); the dormant pairwise
`zone_travel_buffers` matrix is being retired separately.

Design (audit M16 RECOMMENDED DESIGN, Capability A + decision c):
- Match on ZIP membership OR town-name membership — not radius/polygons. This
  mirrors how every major FSM tool (Jobber, Housecall Pro, ServiceTitan) does
  coverage: a list of ZIP / town strings, matched against the address.
- Bias HARD to false-ACCEPT: a lost lead is the owner's #1 fear. We return
  'out_of_area' ONLY when there is a configured coverage list AND we hold a
  trusted signal (postal or town) AND neither matches. Everything ambiguous
  resolves to 'unknown' (proceed and book) or 'unconfigured' (feature off for
  this tenant) — never to a decline.

Cost asymmetry that justifies the bias: a false 'out_of_area' just routes an
in-area caller to a call-back instead of an instant booking (the lead is still
captured + the owner flagged — self-correcting), whereas letting the gate go
silent would defeat its only purpose. An in-area match on EITHER signal always
wins, so a forgotten ZIP can't cause a false decline as long as the town is
listed (and vice-versa).

Verdicts:
  in_area      — postal OR town is in the coverage list → proceed to book.
  out_of_area  — coverage configured + a trusted signal + no match → apply the
                 owner's out_of_area_action (callback / decline_referral /
                 trip_fee); always capture the lead + flag the owner.
  unknown      — coverage configured but nothing trustworthy to check (no
                 postal, no town) → proceed; never decline on no evidence.
  unconfigured — tenant has no coverage list at all → gate is off; behave
                 exactly as before this feature existed.

Intentionally dependency-free and side-effect-free: trivially unit-testable
and safe on the hot call path.
"""

from __future__ import annotations

import re


def _norm(value: str | None) -> str:
    """Casefold + strip + drop '.'/',' + collapse internal whitespace.

    Handles the common town-name spelling drift between what an owner types in
    the dashboard and what Google returns as the normalized locality (e.g.
    'St. Louis' vs 'St Louis', '  New   York ' vs 'New York'). Deliberately
    light — no synonym expansion ('Saint' != 'St'), which would risk false
    matches; that is out of scope for v1.
    """
    s = (value or "").strip().casefold()
    s = s.replace(".", "").replace(",", "")
    s = re.sub(r"\s+", " ", s)
    return s


def _norm_postal(value: str | None) -> str:
    """Postal normalization: `_norm` then strip ALL internal whitespace so a
    CA/UK postal matches with or without its space ('K1A 0B1' == 'K1A0B1').
    US (5-digit) and SG (6-digit) postals are unaffected."""
    return _norm(value).replace(" ", "")


def _coverage_sets(zones: list[dict] | None) -> tuple[set[str], set[str]]:
    """Build the (postals, cities) coverage sets from the union of all the
    tenant's service_zones rows. Normalized; empties dropped."""
    postals: set[str] = set()
    cities: set[str] = set()
    for z in zones or []:
        for pc in (z.get("postal_codes") or []):
            n = _norm_postal(pc)
            if n:
                postals.add(n)
        for city in (z.get("cities") or []):
            n = _norm(city)
            if n:
                cities.add(n)
    return postals, cities


def classify_service_area(
    *,
    zones: list[dict] | None,
    postal_code: str | None = None,
    locality: str | None = None,
) -> dict:
    """Classify a (postal_code, locality) against the tenant's coverage list.

    Args:
        zones: the tenant's service_zones rows (each a dict with optional
               'postal_codes' and 'cities' string lists). Pass the prefetched
               deps['_slot_cache']['service_zones'].
        postal_code: the Google-NORMALIZED postal code from the validated
               address (validation_result['address_components']['postal_code']).
               Pass None on any non-confirmed verdict — never the raw spoken value.
        locality: the Google-NORMALIZED town/locality
               (address_components['locality']). Pass None when absent.

    Returns:
        {"verdict": "in_area"|"out_of_area"|"unknown"|"unconfigured",
         "matched_on": "postal"|"city"|None}

    Never raises.
    """
    postals, cities = _coverage_sets(zones)

    # No coverage configured anywhere → gate is off for this tenant.
    if not postals and not cities:
        return {"verdict": "unconfigured", "matched_on": None}

    npostal = _norm_postal(postal_code)
    nlocality = _norm(locality)

    # In-area on EITHER signal (bias to accept; a match always wins).
    if npostal and npostal in postals:
        return {"verdict": "in_area", "matched_on": "postal"}
    if nlocality and nlocality in cities:
        return {"verdict": "in_area", "matched_on": "city"}

    # Coverage configured AND we have at least one trusted signal that did not
    # match → clearly outside the area.
    if npostal or nlocality:
        return {"verdict": "out_of_area", "matched_on": None}

    # Coverage configured but nothing trustworthy to check → cannot judge;
    # never decline on no evidence.
    return {"verdict": "unknown", "matched_on": None}
