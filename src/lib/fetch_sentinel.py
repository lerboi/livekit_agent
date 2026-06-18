"""Shared sentinel for integration context fetches (2026-06-12 audit LOW-14).

`FETCH_UNAVAILABLE` lets the Jobber/Xero customer-context fetchers and their
bounded wrapper distinguish a *failed* fetch (timeout, transport/HTTP/auth
error, exception) from a genuine *no-match* (the query succeeded but the caller
isn't on file) and from *not connected* (the tenant never linked the provider).

- no-match / not-connected -> None  (treat caller as new/walk-in — correct)
- failed fetch             -> FETCH_UNAVAILABLE  (records temporarily unavailable)

This module imports nothing project-internal so it can be shared by
src/integrations/{jobber,xero}.py and src/lib/customer_context.py without a
circular import (customer_context already imports the two fetchers).
"""
from __future__ import annotations

# Identity sentinel — compare with `is`, never truthiness (it is a truthy object).
FETCH_UNAVAILABLE = object()
