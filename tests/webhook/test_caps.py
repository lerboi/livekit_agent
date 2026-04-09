"""Unit tests for check_outbound_cap() — Plan 39-04 fills these in."""
import pytest

pytestmark = pytest.mark.skipif(
    True,  # Flipped to False by Plan 39-04
    reason="check_outbound_cap not yet implemented (Plan 39-04)",
)


@pytest.mark.asyncio
async def test_under_cap_us_returns_true():
    """US tenant with 100000 seconds used (100000 < 300000) → True."""
    pass


@pytest.mark.asyncio
async def test_at_cap_us_returns_false():
    """US tenant with exactly 300000 seconds used → False (at cap)."""
    pass


@pytest.mark.asyncio
async def test_at_cap_ca_returns_false():
    """CA tenant at exactly the 5000-minute cap → False. Validates CA is in _LIMITS_SEC and shares the US limit."""
    pass


@pytest.mark.asyncio
async def test_over_cap_us_returns_false():
    """US tenant with 400000 seconds used → False."""
    pass


@pytest.mark.asyncio
async def test_under_cap_sg_returns_true():
    """SG tenant with 100000 seconds used (< 150000) → True."""
    pass


@pytest.mark.asyncio
async def test_at_cap_sg_returns_false():
    """SG tenant with exactly 150000 seconds used → False."""
    pass


@pytest.mark.asyncio
async def test_unknown_country_falls_back_to_us_limit():
    """country='XX' uses US limit (300000 seconds)."""
    pass


@pytest.mark.asyncio
async def test_zero_seconds_used_returns_true():
    """Fresh tenant with 0 seconds used → True."""
    pass
