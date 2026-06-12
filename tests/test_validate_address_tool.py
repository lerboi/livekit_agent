"""Early address-validation tool tests (2026-06-10).

Covers the new src/tools/validate_address.py tool and the cache-reuse
contract it establishes with book_appointment / capture_lead:

1. Verdict branches → correct STATE strings:
     confirmed              → STATE:address_ok
     confirmed_with_changes → STATE:address_corrected
     unconfirmed            → STATE:address_unclear
     skipped / unsupported_region / error → STATE:address_noted
2. deps["_validated_address"] cache written ({input, result, ts}).
3. Never raises — even when the validation helper itself raises
   (belt-and-braces; the helpers are contractually never-raising).
4. Cache reuse: book_appointment / capture_lead skip the second Google
   call when the input address matches the cached validation (normalized
   street + postal compare; unit differences tolerated) and fall back to
   live validation when it differs.
5. Caller-region derivation (src/lib/phone.py::derive_caller_region) and
   the caller-region fallback orchestrator
   (google_maps.validate_address_with_region_fallback) — tenant region is
   primary, caller region is the automatic second attempt on an unhelpful
   first verdict.

All tests mock the validation layer — no live Google calls. Tool/cache
tests mock validate_address_with_region_fallback (what the tools call);
fallback-orchestrator tests mock validate_address_bounded underneath it.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.validate_address import (
    create_validate_address_tool,
    get_cached_validation,
)


# ── Scaffolding ─────────────────────────────────────────────────────────────


def _make_deps(**overrides) -> dict:
    deps = {
        "supabase": MagicMock(),
        "tenant_id": "tenant-1",
        "call_id": "call-1",
        "country": "US",
    }
    deps.update(overrides)
    return deps


def _bounded_result(
    verdict: str,
    formatted: str | None = None,
    country_code: str = "US",
    country: str = "United States",
    postal: str | None = "94043",
) -> dict:
    # country_code/country are parametrized since the 2026-06-11 country
    # guard (findings.md P2): a confirmed* result whose country contradicts
    # the trusted region (caller region when supported, else tenant region)
    # is downgraded — fixtures must carry the country the scenario implies.
    return {
        "verdict": verdict,
        "formatted_address": formatted,
        "place_id": "ChIJ-place-1" if formatted else None,
        "latitude": 37.4224 if formatted else None,
        "longitude": -122.0840 if formatted else None,
        "address_components": {
            "street_number": "1600",
            "route": "Amphitheatre Pkwy",
            "subpremise": None,
            "locality": "Mountain View",
            "admin_area_level_1": "CA",
            "admin_area_level_2": None,
            "postal_code": postal,
            "country": country,
            "country_code": country_code,
        } if formatted else {
            "street_number": None,
            "route": None,
            "subpremise": None,
            "locality": None,
            "admin_area_level_1": None,
            "admin_area_level_2": None,
            "postal_code": None,
            "country": None,
            "country_code": None,
        },
        "latency_ms": 120,
        "raw_status": 200,
    }


def _raw_args(**overrides) -> dict:
    base = {
        "street": "1600 Amphitheatre Pkwy",
        "unit": "",
        "postal_code": "94043",
        "city": "",
    }
    base.update(overrides)
    return base


@pytest.fixture
def patched_validate():
    # The tool calls validate_address_with_region_fallback, which returns
    # (result, region_used). The fixture mocks that tuple contract.
    with patch(
        "src.tools.validate_address.validate_address_with_region_fallback",
        new_callable=AsyncMock,
    ) as mock_validate:
        yield mock_validate


FORMATTED = "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"


# ── Verdict → STATE branches ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirmed_returns_address_ok(patched_validate):
    patched_validate.return_value = (_bounded_result("confirmed", FORMATTED), "US")
    deps = _make_deps()
    tool = create_validate_address_tool(deps)

    result = await tool.__wrapped__(_raw_args(), MagicMock())

    assert result.startswith("STATE:address_ok")
    assert FORMATTED in result
    assert "ONE short sentence" in result
    assert deps["_last_tool_state"] == result


@pytest.mark.asyncio
async def test_corrected_returns_address_corrected(patched_validate):
    patched_validate.return_value = (
        _bounded_result("confirmed_with_changes", FORMATTED),
        "US",
    )
    deps = _make_deps()
    tool = create_validate_address_tool(deps)

    result = await tool.__wrapped__(_raw_args(), MagicMock())

    assert result.startswith("STATE:address_corrected")
    assert FORMATTED in result
    # Correction loop instruction present.
    assert "validate_address" in result


@pytest.mark.asyncio
async def test_unconfirmed_returns_address_unclear_with_hint(patched_validate):
    patched_validate.return_value = (_bounded_result("unconfirmed", None), "US")
    deps = _make_deps()
    tool = create_validate_address_tool(deps)

    # No postal given AND none found → best-effort hint is postal_code.
    result = await tool.__wrapped__(_raw_args(postal_code=""), MagicMock())

    assert result.startswith("STATE:address_unclear")
    assert "missing=postal_code" in result
    assert "one retry" in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["skipped", "unsupported_region", "error"])
async def test_noted_for_each_nonblocking_verdict(patched_validate, verdict):
    """skipped/unsupported_region/error never block and never expose
    internals — the caller's address is echoed and the call continues."""
    patched_validate.return_value = (_bounded_result(verdict, None), "US")
    deps = _make_deps()
    tool = create_validate_address_tool(deps)

    result = await tool.__wrapped__(_raw_args(), MagicMock())

    assert result.startswith("STATE:address_noted")
    # Caller's address as given is echoed for the readback.
    assert "1600 Amphitheatre Pkwy" in result
    assert "Never mention" in result


@pytest.mark.asyncio
async def test_never_raises_when_bounded_wrapper_raises(patched_validate):
    """The validation helper is contractually never-raising, but the tool
    must survive even if that contract breaks (API error / timeout path)."""
    patched_validate.side_effect = RuntimeError("simulated transport explosion")
    deps = _make_deps()
    tool = create_validate_address_tool(deps)

    result = await tool.__wrapped__(_raw_args(), MagicMock())

    assert result.startswith("STATE:address_noted")
    # The error result is still cached (but get_cached_validation refuses to
    # reuse verdict=error — see test below).
    assert deps["_validated_address"]["result"]["verdict"] == "error"


# ── Cache write ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_written_with_input_result_ts(patched_validate):
    patched_validate.return_value = (_bounded_result("confirmed", FORMATTED), "US")
    deps = _make_deps()
    tool = create_validate_address_tool(deps)

    before = time.time()
    await tool.__wrapped__(_raw_args(unit="Apt 2", city="Mountain View"), MagicMock())

    cached = deps["_validated_address"]
    assert cached["input"] == {
        "street": "1600 Amphitheatre Pkwy",
        "unit": "Apt 2",
        "postal_code": "94043",
        "city": "Mountain View",
    }
    assert cached["result"]["verdict"] == "confirmed"
    assert cached["result"]["formatted_address"] == FORMATTED
    assert cached["ts"] >= before


# ── get_cached_validation matching rules ────────────────────────────────────


def _cache_entry(verdict="confirmed", street="1600 Amphitheatre Pkwy",
                 unit="", postal="94043") -> dict:
    return {
        "input": {"street": street, "unit": unit, "postal_code": postal, "city": ""},
        "result": _bounded_result(verdict, FORMATTED if verdict.startswith("confirmed") else None),
        "ts": time.time(),
    }


def test_cache_match_is_casefold_and_strip_tolerant():
    deps = {"_validated_address": _cache_entry()}
    hit = get_cached_validation(deps, "  1600 AMPHITHEATRE PKWY ", "94043 ")
    assert hit is not None
    assert hit["verdict"] == "confirmed"


def test_cache_match_tolerates_unit_differences():
    deps = {"_validated_address": _cache_entry(unit="Apt 2")}
    # book_appointment passes a different (or no) unit — still a match.
    hit = get_cached_validation(deps, "1600 Amphitheatre Pkwy", "94043")
    assert hit is not None


def test_cache_miss_on_different_street():
    deps = {"_validated_address": _cache_entry()}
    assert get_cached_validation(deps, "456 Oak Avenue", "94043") is None


def test_cache_miss_on_different_postal():
    deps = {"_validated_address": _cache_entry()}
    assert get_cached_validation(deps, "1600 Amphitheatre Pkwy", "10001") is None


def test_cache_never_reuses_error_verdict():
    """A cached transient error must not poison booking-time validation —
    the tools should get a fresh attempt."""
    deps = {"_validated_address": _cache_entry(verdict="error")}
    assert get_cached_validation(deps, "1600 Amphitheatre Pkwy", "94043") is None


def test_cache_absent_returns_none():
    assert get_cached_validation({}, "1600 Amphitheatre Pkwy", "94043") is None


# ── Cache reuse in book_appointment ─────────────────────────────────────────


def _make_book_supabase() -> MagicMock:
    sb = MagicMock()
    tenant_chain = sb.table.return_value.select.return_value.eq.return_value
    tenant_chain.single.return_value.execute.return_value = MagicMock(
        data={
            "tenant_timezone": "America/Los_Angeles",
            "working_hours": {},
            "slot_duration_mins": 60,
            "business_name": "Test Co",
            "default_locale": "en",
        }
    )
    update_chain = sb.table.return_value.update.return_value.eq.return_value
    update_chain.is_.return_value.execute.return_value = MagicMock(data=[])
    update_chain.execute.return_value = MagicMock(data=[{"call_id": "c1"}])
    return sb


def _make_book_deps(sb: MagicMock, cache: dict | None) -> dict:
    slot_token = "tok-1"
    deps = {
        "supabase": sb,
        "tenant_id": "tenant-1",
        "call_id": "call-1",
        "call_uuid": "call-uuid-1",
        "country": "US",
        "from_number": "+15551234567",
        "to_number": "+15557654321",
        "_slot_tokens": {
            slot_token: {
                "slot_start_utc": "2026-07-04T20:00:00+00:00",
                "slot_end_utc": "2026-07-04T21:00:00+00:00",
                "created_at": time.time(),
            }
        },
        "_last_offered_token": slot_token,
    }
    if cache is not None:
        deps["_validated_address"] = cache
    return deps


def _book_args(**overrides) -> dict:
    base = {
        "slot_token": "tok-1",
        "street_name": "1600 Amphitheatre Pkwy",
        "postal_code": "94043",
        "caller_name": "Alice",
        "unit_number": "",
        "urgency": "routine",
    }
    base.update(overrides)
    return base


@pytest.fixture
def patched_book():
    with patch("src.tools.book_appointment.validate_address_with_region_fallback", new_callable=AsyncMock) as mock_validate, \
         patch("src.tools.book_appointment.atomic_book_slot", new_callable=AsyncMock) as mock_atomic, \
         patch("src.tools.book_appointment.push_booking_to_calendar"), \
         patch("src.tools.book_appointment.send_caller_sms"), \
         patch("src.tools.book_appointment.send_caller_recovery_sms"):
        yield {"validate": mock_validate, "atomic": mock_atomic}


@pytest.mark.asyncio
async def test_book_appointment_reuses_matching_cache_no_second_google_call(patched_book):
    from src.tools.book_appointment import create_book_appointment_tool

    sb = _make_book_supabase()
    deps = _make_book_deps(sb, _cache_entry())
    tool = create_book_appointment_tool(deps)
    patched_book["atomic"].return_value = {"success": True, "appointment_id": "a1"}

    result = await tool.__wrapped__(_book_args(), MagicMock())

    # No second Google call.
    assert patched_book["validate"].await_count == 0
    # Cached verdict + fields flowed into atomic_book_slot exactly as a live
    # validation would have (D-D3' overwrite included).
    kwargs = patched_book["atomic"].await_args.kwargs
    assert kwargs["address_validation_verdict"] == "confirmed"
    assert kwargs["formatted_address"] == FORMATTED
    assert kwargs["address"] == FORMATTED
    assert kwargs["place_id"] == "ChIJ-place-1"
    assert kwargs["latitude"] == 37.4224
    assert kwargs["longitude"] == -122.0840
    # Shortened cached-path directive: verdict token intact, no address re-read.
    assert result.startswith("BOOKED [verdict=validated]:")
    assert "do not re-read it" in result
    assert FORMATTED not in result


@pytest.mark.asyncio
async def test_book_appointment_falls_back_to_live_validation_on_mismatch(patched_book):
    from src.tools.book_appointment import create_book_appointment_tool

    sb = _make_book_supabase()
    # Cache holds a DIFFERENT street than the booking args → must re-validate.
    deps = _make_book_deps(sb, _cache_entry(street="456 Oak Avenue"))
    tool = create_book_appointment_tool(deps)
    patched_book["validate"].return_value = (_bounded_result("confirmed", FORMATTED), "US")
    patched_book["atomic"].return_value = {"success": True, "appointment_id": "a1"}

    result = await tool.__wrapped__(_book_args(), MagicMock())

    assert patched_book["validate"].await_count == 1
    # 2026-06-11 (findings.md P2): the fallback directive no longer re-reads
    # the normalized address — the caller already heard the address in the
    # mandatory pre-booking readback.
    assert result.startswith("BOOKED [verdict=validated]:")
    assert FORMATTED not in result
    assert "do not re-read" in result


@pytest.mark.asyncio
async def test_book_appointment_unit_difference_still_reuses_cache(patched_book):
    from src.tools.book_appointment import create_book_appointment_tool

    sb = _make_book_supabase()
    deps = _make_book_deps(sb, _cache_entry(unit="Apt 2"))
    tool = create_book_appointment_tool(deps)
    patched_book["atomic"].return_value = {"success": True, "appointment_id": "a1"}

    await tool.__wrapped__(_book_args(unit_number="Unit 5"), MagicMock())

    assert patched_book["validate"].await_count == 0


# ── Cache reuse in capture_lead ─────────────────────────────────────────────


def _make_lead_deps(cache: dict | None) -> dict:
    sb = MagicMock()
    update_chain = sb.table.return_value.update.return_value.eq.return_value
    update_chain.is_.return_value.execute.return_value = MagicMock(data=[])
    deps = {
        "supabase": sb,
        "tenant_id": "tenant-1",
        "call_id": "call-1",
        "call_uuid": "call-uuid-1",
        "country": "US",
        "from_number": "+15551234567",
        "to_number": "+15557654321",
        "start_timestamp": 0,
    }
    if cache is not None:
        deps["_validated_address"] = cache
    return deps


@pytest.fixture
def patched_lead():
    with patch("src.tools.capture_lead.validate_address_with_region_fallback", new_callable=AsyncMock) as mock_validate, \
         patch("src.tools.capture_lead.record_outcome", new_callable=AsyncMock) as mock_record:
        yield {"validate": mock_validate, "record": mock_record}


@pytest.mark.asyncio
async def test_capture_lead_reuses_matching_cache_no_second_google_call(patched_lead):
    from src.tools.capture_lead import create_capture_lead_tool

    deps = _make_lead_deps(_cache_entry())
    tool = create_capture_lead_tool(deps)
    patched_lead["record"].return_value = {"customer_id": "c", "inquiry_id": "i"}

    result = await tool.__wrapped__(
        MagicMock(),
        caller_name="Alice",
        street_name="1600 Amphitheatre Pkwy",
        postal_code="94043",
    )

    assert patched_lead["validate"].await_count == 0
    kwargs = patched_lead["record"].await_args.kwargs
    assert kwargs["address_validation_verdict"] == "confirmed"
    assert kwargs["formatted_address"] == FORMATTED
    assert kwargs["service_address"] == FORMATTED
    assert result.startswith("LEAD CAPTURED [verdict=validated]:")
    assert "do not re-read it" in result


@pytest.mark.asyncio
async def test_capture_lead_falls_back_to_live_validation_on_mismatch(patched_lead):
    from src.tools.capture_lead import create_capture_lead_tool

    deps = _make_lead_deps(_cache_entry(street="456 Oak Avenue"))
    tool = create_capture_lead_tool(deps)
    patched_lead["validate"].return_value = (_bounded_result("confirmed", FORMATTED), "US")
    patched_lead["record"].return_value = {"customer_id": "c", "inquiry_id": "i"}

    result = await tool.__wrapped__(
        MagicMock(),
        caller_name="Alice",
        street_name="1600 Amphitheatre Pkwy",
        postal_code="94043",
    )

    assert patched_lead["validate"].await_count == 1
    # 2026-06-11 (findings.md P2): fallback directive no longer re-reads the
    # normalized address (mirrors book_appointment).
    assert result.startswith("LEAD CAPTURED [verdict=validated]:")
    assert FORMATTED not in result
    assert "do not re-read" in result


# ── Caller-region derivation (src/lib/phone.py) ─────────────────────────────


from src.lib.phone import derive_caller_region  # noqa: E402


def test_derive_caller_region_us_number():
    assert derive_caller_region("+12125551234") == "US"


def test_derive_caller_region_canadian_area_code():
    # +1 is shared NANP — area code 604 (Vancouver) must resolve to CA, not US.
    assert derive_caller_region("+16045551234") == "CA"


def test_derive_caller_region_singapore():
    assert derive_caller_region("+6591234567") == "SG"


@pytest.mark.parametrize(
    "raw",
    [None, "", "anonymous", "Restricted", "garbage", "+", "+999999", 12345],
)
def test_derive_caller_region_unparseable_returns_none(raw):
    # Withheld/anonymous caller-ID, garbage, wrong types — all → None,
    # and none of them may raise (this would break session startup).
    assert derive_caller_region(raw) is None


def test_derive_caller_region_unassigned_nanp_returns_none():
    # Parses fine but maps to no region (non-existent +1 area code).
    assert derive_caller_region("+19995551234") is None


# ── Region-fallback orchestrator (google_maps) ──────────────────────────────


from src.integrations.google_maps import (  # noqa: E402
    validate_address_with_region_fallback,
)


@pytest.fixture
def patched_bounded():
    # Mock the BOUNDED validator underneath the orchestrator — the fallback
    # logic itself runs for real.
    with patch(
        "src.integrations.google_maps.validate_address_bounded",
        new_callable=AsyncMock,
    ) as mock_bounded:
        yield mock_bounded


async def _run_fallback(mock_bounded, *, region_code="US", caller_region=None):
    return await validate_address_with_region_fallback(
        "tenant-1",
        "call-1",
        region_code=region_code,
        caller_region=caller_region,
        address_lines=["1600 Amphitheatre Pkwy"],
        postal_code="94043",
        supabase=None,
    )


def _regions_called(mock_bounded) -> list:
    return [c.kwargs["region_code"] for c in mock_bounded.await_args_list]


@pytest.mark.asyncio
async def test_fallback_primary_unconfirmed_caller_ca_wins(patched_bounded):
    """Primary (US) unconfirmed + caller CA → second attempt with CA runs
    and its confirmed result wins. (Country guard: the second attempt's
    result is in the caller's country, so it passes the guard.)"""
    patched_bounded.side_effect = [
        _bounded_result("unconfirmed", None),
        _bounded_result("confirmed", FORMATTED, country_code="CA", country="Canada"),
    ]

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region="CA"
    )

    assert _regions_called(patched_bounded) == ["US", "CA"]
    assert result["verdict"] == "confirmed"
    assert result["formatted_address"] == FORMATTED
    assert region_used == "CA"


@pytest.mark.asyncio
async def test_fallback_no_second_attempt_when_primary_confirmed(patched_bounded):
    # Country guard note (2026-06-11): with caller CA present, CA is the
    # trusted region — the confirmed result must be in CA for the guard to
    # pass it (the primary US-region request found an address in the
    # caller's country). A US-country result here would now be downgraded
    # and retried — covered by test_country_guard_downgrades_and_retries.
    patched_bounded.return_value = _bounded_result(
        "confirmed", FORMATTED, country_code="CA", country="Canada"
    )

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region="CA"
    )

    assert patched_bounded.await_count == 1
    assert region_used == "US"
    assert result["verdict"] == "confirmed"


@pytest.mark.asyncio
async def test_fallback_no_second_attempt_when_caller_equals_primary(patched_bounded):
    patched_bounded.return_value = _bounded_result("unconfirmed", None)

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region="US"
    )

    assert patched_bounded.await_count == 1
    assert region_used == "US"


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [None, "", "GB"])
async def test_fallback_no_second_attempt_when_caller_missing_or_unsupported(
    patched_bounded, caller
):
    patched_bounded.return_value = _bounded_result("unconfirmed", None)

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region=caller
    )

    assert patched_bounded.await_count == 1
    assert region_used == "US"


@pytest.mark.asyncio
async def test_fallback_both_unconfirmed_primary_result_returned(patched_bounded):
    """Tie on verdict rank → attempt 1 (primary region) wins."""
    primary_result = _bounded_result("unconfirmed", None)
    primary_result["latency_ms"] = 111  # marker to identify which dict won
    second_result = _bounded_result("unconfirmed", None)
    second_result["latency_ms"] = 222
    patched_bounded.side_effect = [primary_result, second_result]

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region="CA"
    )

    assert patched_bounded.await_count == 2
    assert result is primary_result
    assert region_used == "US"


@pytest.mark.asyncio
async def test_fallback_unsupported_region_verdict_triggers_second_attempt(
    patched_bounded,
):
    patched_bounded.side_effect = [
        _bounded_result("unsupported_region", None),
        _bounded_result(
            "confirmed_with_changes", FORMATTED,
            country_code="SG", country="Singapore",
        ),
    ]

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region="SG"
    )

    assert _regions_called(patched_bounded) == ["US", "SG"]
    assert result["verdict"] == "confirmed_with_changes"
    assert region_used == "SG"


@pytest.mark.asyncio
async def test_fallback_unsupported_primary_uses_caller_region_first(patched_bounded):
    """Tenant region GB (not in SUPPORTED_REGION_CODES) + caller SG → SG is
    used for attempt 1 (don't waste a known-'skipped' attempt on GB)."""
    patched_bounded.return_value = _bounded_result(
        "confirmed", FORMATTED, country_code="SG", country="Singapore"
    )

    result, region_used = await _run_fallback(
        patched_bounded, region_code="GB", caller_region="SG"
    )

    assert _regions_called(patched_bounded) == ["SG"]
    assert patched_bounded.await_count == 1
    assert region_used == "SG"
    assert result["verdict"] == "confirmed"


@pytest.mark.asyncio
async def test_fallback_unsupported_primary_and_caller_keeps_primary(patched_bounded):
    """Neither region supported → primary used as before (bounded returns
    'skipped' for it), no second attempt with another unsupported region."""
    patched_bounded.return_value = _bounded_result("skipped", None)

    result, region_used = await _run_fallback(
        patched_bounded, region_code="GB", caller_region="FR"
    )

    assert _regions_called(patched_bounded) == ["GB"]
    assert region_used == "GB"


@pytest.mark.asyncio
async def test_fallback_keeps_primary_region_used_when_second_is_not_better(
    patched_bounded,
):
    """Second attempt strictly worse (error) → primary unconfirmed kept."""
    patched_bounded.side_effect = [
        _bounded_result("unconfirmed", None),
        _bounded_result("error", None),
    ]

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region="CA"
    )

    assert patched_bounded.await_count == 2
    assert result["verdict"] == "unconfirmed"
    assert region_used == "US"


@pytest.mark.asyncio
async def test_fallback_never_raises_when_second_attempt_raises(patched_bounded):
    """A raise during the second attempt degrades to attempt 1's result —
    never propagates (never-raises contract)."""
    primary_result = _bounded_result("unconfirmed", None)
    patched_bounded.side_effect = [primary_result, RuntimeError("boom")]

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region="CA"
    )

    assert result is primary_result
    assert region_used == "US"


@pytest.mark.asyncio
async def test_fallback_never_raises_when_first_attempt_raises(patched_bounded):
    """Even a (contract-breaking) raise from the bounded validator on attempt
    1 yields an error-shaped result, not an exception."""
    patched_bounded.side_effect = RuntimeError("boom")

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region=None
    )

    assert result["verdict"] == "error"
    assert region_used == "US"


@pytest.mark.asyncio
async def test_fallback_passes_supabase_to_both_attempts(patched_bounded):
    """Per-attempt telemetry lives inside validate_address_bounded — both
    attempts must receive the supabase client so each writes its own
    gmaps_validate_events row."""
    patched_bounded.side_effect = [
        _bounded_result("unconfirmed", None),
        _bounded_result("confirmed", FORMATTED),
    ]
    sb = MagicMock()

    await validate_address_with_region_fallback(
        "tenant-1",
        "call-1",
        region_code="US",
        caller_region="CA",
        address_lines=["1600 Amphitheatre Pkwy"],
        postal_code="94043",
        supabase=sb,
    )

    assert patched_bounded.await_count == 2
    for c in patched_bounded.await_args_list:
        assert c.kwargs["supabase"] is sb


# ── Country guard (2026-06-11, findings.md P2) ──────────────────────────────
#
# Incident: call eef9f785 (2026-06-09) — tenant.country misconfigured 'US'
# for a Singapore business; booking-time validation region=US let Google
# "correct" '40 Canberra Drive' (SG) into '40 East Canberra Drive, Lindon,
# Utah, USA' (confirmed_with_changes), which was adopted, spoken, and stored.
# The guard downgrades any confirmed* result whose country contradicts the
# trusted region (caller-ID region when supported, else tenant region) to
# 'unconfirmed' with Google fields stripped — and because 'unconfirmed' is a
# retry verdict, the caller-region second attempt fires and can recover the
# correct-country address.


@pytest.mark.asyncio
async def test_country_guard_downgrades_and_retries(patched_bounded):
    """The Utah incident shape, self-healing: primary (US, misconfigured
    tenant) returns a wrong-country confirmed_with_changes; the guard
    downgrades it, the caller-region (SG) retry fires and its SG-country
    confirmed result wins."""
    patched_bounded.side_effect = [
        _bounded_result("confirmed_with_changes", "40 East Canberra Drive, Lindon, UT 84042, USA"),
        _bounded_result(
            "confirmed", "40 Canberra Drive, Singapore 768433",
            country_code="SG", country="Singapore", postal="768433",
        ),
    ]

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region="SG"
    )

    assert _regions_called(patched_bounded) == ["US", "SG"]
    assert result["verdict"] == "confirmed"
    assert result["formatted_address"] == "40 Canberra Drive, Singapore 768433"
    assert region_used == "SG"


@pytest.mark.asyncio
async def test_country_guard_strips_google_fields_when_both_attempts_mismatch(patched_bounded):
    """Both attempts return wrong-country confirmations → final result is
    unconfirmed with every Google-derived field stripped, so a cross-country
    formatted address can never be adopted, spoken, or stored."""
    patched_bounded.side_effect = [
        _bounded_result("confirmed", FORMATTED),
        _bounded_result("confirmed", FORMATTED),
    ]

    result, region_used = await _run_fallback(
        patched_bounded, region_code="US", caller_region="SG"
    )

    assert patched_bounded.await_count == 2
    assert result["verdict"] == "unconfirmed"
    assert result["formatted_address"] is None
    assert result["place_id"] is None
    assert result["latitude"] is None
    assert result["longitude"] is None
    assert result["address_components"]["country_code"] is None


@pytest.mark.asyncio
async def test_country_guard_uses_tenant_region_when_no_caller_region(patched_bounded):
    """No caller-ID region → the tenant region is the trusted region. An
    SG tenant getting a US-country confirmation is downgraded (no retry
    possible without a caller region)."""
    patched_bounded.return_value = _bounded_result("confirmed", FORMATTED)

    result, region_used = await _run_fallback(
        patched_bounded, region_code="SG", caller_region=None
    )

    assert patched_bounded.await_count == 1
    assert result["verdict"] == "unconfirmed"
    assert result["formatted_address"] is None


@pytest.mark.asyncio
async def test_country_guard_passes_matching_country(patched_bounded):
    """Country matches the trusted region → result flows through untouched."""
    patched_bounded.return_value = _bounded_result(
        "confirmed", "40 Canberra Drive, Singapore 768433",
        country_code="SG", country="Singapore", postal="768433",
    )

    result, region_used = await _run_fallback(
        patched_bounded, region_code="SG", caller_region="SG"
    )

    assert patched_bounded.await_count == 1
    assert result["verdict"] == "confirmed"
    assert result["formatted_address"] == "40 Canberra Drive, Singapore 768433"


# ── Lookup-supplied postal confirmation (2026-06-11, findings.md P2) ────────
#
# Incident: call 31559053 (2026-06-11) — caller gave street + building, NO
# postal; the confirmed result carried a Google-inferred postal the agent
# asserted as fact, then defended against the caller's correction. When the
# caller never spoke a postal, the tool now returns a dedicated STATE that
# directs the agent to ask the postal as a question.


@pytest.mark.asyncio
async def test_confirmed_without_caller_postal_asks_postal_as_question(patched_validate):
    patched_validate.return_value = (_bounded_result("confirmed", FORMATTED), "US")
    deps = _make_deps()
    tool = create_validate_address_tool(deps)

    result = await tool.__wrapped__(_raw_args(postal_code=""), MagicMock())

    assert result.startswith("STATE:address_ok_confirm_postal")
    assert "postal=94043" in result
    assert "QUESTION" in result
    assert "theirs is correct" in result


@pytest.mark.asyncio
async def test_confirmed_with_caller_postal_stays_plain_address_ok(patched_validate):
    """Caller spoke the postal themselves → no extra confirmation question."""
    patched_validate.return_value = (_bounded_result("confirmed", FORMATTED), "US")
    deps = _make_deps()
    tool = create_validate_address_tool(deps)

    result = await tool.__wrapped__(_raw_args(), MagicMock())

    assert result.startswith("STATE:address_ok ")
    assert "address_ok_confirm_postal" not in result


@pytest.mark.asyncio
async def test_confirmed_without_postal_anywhere_stays_plain_address_ok(patched_validate):
    """No caller postal AND no postal in the result → nothing to confirm."""
    patched_validate.return_value = (
        _bounded_result("confirmed", FORMATTED, postal=None),
        "US",
    )
    deps = _make_deps()
    tool = create_validate_address_tool(deps)

    result = await tool.__wrapped__(_raw_args(postal_code=""), MagicMock())

    assert result.startswith("STATE:address_ok ")
    assert "address_ok_confirm_postal" not in result


# ── Cache postal tolerance (2026-06-11, findings.md P2) ─────────────────────


def test_cache_match_when_cached_postal_empty_and_request_matches_result_postal():
    """address_ok_confirm_postal flow: validation ran with no caller postal,
    the caller then confirmed the looked-up postal, and booking passes it —
    the cache must still match (no second Google call)."""
    deps = {"_validated_address": _cache_entry(postal="")}
    hit = get_cached_validation(deps, "1600 Amphitheatre Pkwy", "94043")
    assert hit is not None
    assert hit["verdict"] == "confirmed"


def test_cache_miss_when_cached_postal_empty_and_request_differs_from_result_postal():
    """Caller rejected the looked-up postal and gave a different one →
    booking-time validation must run fresh."""
    deps = {"_validated_address": _cache_entry(postal="")}
    assert get_cached_validation(deps, "1600 Amphitheatre Pkwy", "10001") is None


# ── Registry ────────────────────────────────────────────────────────────────


def test_validate_address_registered_always_on():
    """validate_address must be registered even when onboarding is
    incomplete (capture_lead needs addresses too and is always on)."""
    from src.tools import create_tools

    deps = {"supabase": MagicMock(), "onboarding_complete": False}
    tools = create_tools(deps)
    names = set()
    for t in tools:
        info = getattr(t, "info", None)
        name = getattr(info, "name", None) or getattr(t, "name", None) or getattr(
            getattr(t, "__wrapped__", None), "__name__", None
        )
        if name:
            names.add(name)
    assert "validate_address" in names
