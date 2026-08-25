"""Test-call sandbox invariants (2026-08-25, admin web test console).

A test call (LiveKit room metadata `{test_call: true}` — set server-side by
the main repo's admin test console or the onboarding phone-test route) must be
CONFINED to itself: its only durable footprint is its own flagged calls row
(migration 079 `calls.is_test_call`), its recording file, and a transient
appointment that post_call auto-cancels. Specifically it must NEVER:

  - write CRM rows (record_call_outcome upserts customers by phone — a
    simulated caller number matching a real customer would merge the test
    into their real history),
  - send owner SMS/email,
  - text the (possibly simulated) caller number (confirmation or recovery SMS),
  - push events to the owner's real Google/Outlook calendar (auto-cancel
    cannot remove those).

These are source-grep invariants (same shape as the agent's other static
guards, e.g. test_greeting_not_interruptible.py) — behavioral confirmation is
a live web test call from the admin console.
"""
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


# ── agent.py — flag threading, gate bypass, MP4 recording ──────────────────


def test_deps_carries_is_test_call():
    assert '"is_test_call": is_test_call' in _read("agent.py"), (
        "deps must expose is_test_call so tools can gate their side effects"
    )


def test_calls_upsert_marks_test_calls_only():
    src = _read("agent.py")
    # The column is added to the row dict ONLY for test calls, so production
    # inserts never reference it (fail-open if migration 079 lags the deploy).
    assert 'if is_test_call:' in src
    assert '_call_row["is_test_call"] = True' in src


def test_metadata_from_number_only_on_test_calls():
    src = _read("agent.py")
    assert 'if is_test_call and not from_number and room_meta.get("from_number")' in src, (
        "simulated caller number must be honored only on test calls and only "
        "when no SIP caller-ID exists"
    )


def test_subscription_gate_bypassed_for_test_calls():
    src = _read("agent.py")
    assert "test call — bypassing gate" in src.replace("\n", " ").replace("  ", " ") or (
        "bypassing gate" in src
    ), "test calls must bypass the subscription-blocked disconnect"


def test_test_calls_record_mp4():
    src = _read("agent.py")
    assert 'api.EncodedFileType.MP4 if is_test_call' in src, (
        "test calls must record as audio-only MP4 for the admin console download"
    )
    assert '"mp4" if is_test_call else "ogg"' in src, (
        "recording path extension must match the egress file type"
    )


# ── book_appointment.py — no SMS / no calendar push on test calls ──────────


def test_recovery_sms_gated_on_test_call():
    src = _read("tools/book_appointment.py")
    assert 'not deps.get("is_test_call") and not deps.get("_recovery_sms_fired")' in src, (
        "slot-taken recovery SMS must never fire on a test call"
    )


def test_calendar_push_and_caller_sms_gated_on_test_call():
    src = _read("tools/book_appointment.py")
    skip_idx = src.find('if deps.get("is_test_call"):')
    assert skip_idx != -1, "success path must short-circuit before external side effects"
    # The skip return must come BEFORE the calendar push and the confirmation
    # SMS dispatch in source order, so neither can fire on a test call.
    assert skip_idx < src.find("create_background_task(_push_calendar_bg())"), (
        "test-call skip must precede the calendar push"
    )
    assert skip_idx < src.find("create_background_task(_send_confirmation_sms_bg())"), (
        "test-call skip must precede the caller confirmation SMS"
    )


# ── capture_lead.py — no CRM writes on test calls ──────────────────────────


def test_capture_lead_skips_record_outcome_on_test_call():
    src = _read("tools/capture_lead.py")
    gate_idx = src.find('if deps.get("is_test_call"):')
    rpc_idx = src.find("outcome = await record_outcome(")
    assert gate_idx != -1 and rpc_idx != -1
    assert gate_idx < rpc_idx, (
        "the is_test_call gate must guard the record_outcome RPC call"
    )
    assert "skipping record_outcome" in src


# ── post_call.py — no CRM writes, no owner notifications, full auto-cancel ─


def test_post_call_record_outcome_gated_on_test_call():
    src = _read("post_call.py")
    assert "if is_test_call:" in src
    # Section 6.5's duration-gated branch must be the elif of the test gate so
    # the RPC is structurally unreachable for test calls.
    assert "elif call_uuid and duration_seconds >= MIN_BILLABLE_DURATION_SEC:" in src


def test_post_call_owner_notifications_gated_on_test_call():
    src = _read("post_call.py")
    assert "skipping owner notifications" in src
    assert "elif tenant_id and tenant:" in src, (
        "the owner-notification block must hang off the test-call gate"
    )


def test_post_call_auto_cancel_covers_all_appointments():
    src = _read("post_call.py")
    assert '.in_("id", _ids)' in src, (
        "auto-cancel must cancel every appointment the test call created "
        "(the old limit(1) leaked the second booking)"
    )
    assert 'entry.get("name") == "book_appointment" and entry.get("appointment_id")' in src, (
        "auto-cancel must also collect appointment ids from the tool_call_log "
        "(covers rows whose call_id FK backfill lost the db_task race)"
    )


def test_usage_tracking_still_excludes_test_calls():
    # Pre-existing behavior locked down: test calls never bill.
    assert "if not is_test_call and tenant_id and duration_seconds" in _read("post_call.py")
