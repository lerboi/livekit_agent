"""
Post-call pipeline — runs when the AgentSession closes.
Combines the logic from processCallEnded() and processCallAnalyzed() in call-processor.js.
Both stages run in-process immediately (no webhook delay).
"""

import os
import re
import asyncio
from datetime import datetime, timedelta, timezone

from .lib.triage.classifier import classify_call
from .lib.leads import create_or_merge_lead
from .lib.notifications import send_owner_sms, send_owner_email
from .lib.slot_calculator import calculate_available_slots
from .utils import to_local_date_string, format_zone_pair_buffers

SUPPORTED_LANGUAGES = {"en", "es"}


async def run_post_call_pipeline(params: dict):
    supabase = params["supabase"]
    call_id = params["call_id"]
    tenant_id = params["tenant_id"]
    tenant = params.get("tenant")
    from_number = params.get("from_number", "")
    to_number = params.get("to_number", "")
    start_timestamp = params["start_timestamp"]
    end_timestamp = params["end_timestamp"]
    transcript_turns = params.get("transcript_turns", [])
    recording_storage_path = params.get("recording_storage_path")
    is_test_call = params.get("is_test_call", False)
    disconnection_reason = params.get("disconnection_reason")
    call_uuid = params.get("call_uuid")

    duration_seconds = round((end_timestamp - start_timestamp) / 1000)

    # ── 1. Build transcript data ──
    transcript_text = "\n".join(
        f"{'Caller' if t['role'] == 'user' else 'AI'}: {t['content']}"
        for t in transcript_turns
    )

    transcript_structured = [
        {"role": t["role"], "content": t["content"]}
        for t in transcript_turns
    ]

    # ── 2. Update call record with transcript + recording ──
    try:
        update_response = (
            supabase.table("calls")
            .update({
                "status": "analyzed",
                "end_timestamp": end_timestamp,
                "recording_storage_path": recording_storage_path,
                "transcript_text": transcript_text or None,
                "transcript_structured": transcript_structured if transcript_structured else None,
                "disconnection_reason": disconnection_reason or "agent_hangup",
            })
            .eq("call_id", call_id)
            .select("id, booking_outcome")
            .execute()
        )
        updated_call = update_response.data[0] if update_response.data else None
    except Exception as e:
        print(f"[post-call] Call record update error: {e}")
        updated_call = None

    call_uuid = (updated_call.get("id") if updated_call else None) or call_uuid

    # ── 3. Test call auto-cancel ──
    if is_test_call and tenant_id:
        try:
            test_appt_resp = (
                supabase.table("appointments")
                .select("id")
                .eq("call_id", call_uuid)
                .eq("tenant_id", tenant_id)
                .limit(1)
                .execute()
            )
            test_appt = test_appt_resp.data[0] if test_appt_resp.data else None

            if test_appt:
                supabase.table("appointments").update({"status": "cancelled"}).eq("id", test_appt["id"]).execute()
                supabase.table("leads").update({"status": "new", "appointment_id": None}).eq("appointment_id", test_appt["id"]).eq("tenant_id", tenant_id).execute()
        except Exception as e:
            print(f"[post-call] Test call auto-cancel error: {e}")

    # ── 4. Usage tracking ──
    if not is_test_call and tenant_id and duration_seconds >= 10:
        try:
            usage_resp = supabase.rpc("increment_calls_used", {
                "p_tenant_id": tenant_id,
                "p_call_id": call_id,
            }).execute()

            usage_data = usage_resp.data
            if usage_data and len(usage_data) > 0:
                row = usage_data[0]
                success = row.get("success")
                calls_used = row.get("calls_used")
                calls_limit = row.get("calls_limit")
                limit_exceeded = row.get("limit_exceeded")
                print(f"[post-call] usage: tenant={tenant_id} success={success} used={calls_used}/{calls_limit} exceeded={limit_exceeded}")

                if success and limit_exceeded:
                    try:
                        sub_resp = (
                            supabase.table("subscriptions")
                            .select("overage_stripe_item_id")
                            .eq("tenant_id", tenant_id)
                            .eq("is_current", True)
                            .limit(1)
                            .execute()
                        )
                        sub = sub_resp.data[0] if sub_resp.data else None

                        if sub and sub.get("overage_stripe_item_id"):
                            import stripe
                            stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
                            stripe.SubscriptionItem.create_usage_record(
                                sub["overage_stripe_item_id"],
                                quantity=1,
                                action="increment",
                            )
                            print(f"[post-call] Overage reported to Stripe: tenant={tenant_id}")
                    except Exception as overage_err:
                        print(f"[post-call] Stripe overage report failed (non-fatal): {overage_err}")
        except Exception as e:
            print(f"[post-call] Usage tracking error (non-fatal): {e}")

    # Skip remaining pipeline if no tenant
    if not tenant_id:
        print(f"[post-call] No tenant for {to_number} — skipping triage/lead/notification")
        return

    # ── 5. Language barrier detection ──
    detected_language = _detect_language_from_transcript(transcript_turns)
    language_barrier = detected_language is not None and detected_language not in SUPPORTED_LANGUAGES

    # ── 6. Triage classification ──
    triage_result = {"urgency": "routine", "confidence": "low", "layer": "layer1"}
    try:
        triage_result = await classify_call(supabase, transcript=transcript_text, tenant_id=tenant_id)
    except Exception as e:
        print(f"[post-call] Triage classification failed: {e}")

    # ── 7. Calculate suggested slots for unbooked calls ──
    suggested_slots = None
    booking_outcome = updated_call.get("booking_outcome") if updated_call else None

    if not booking_outcome or booking_outcome == "not_attempted":
        try:
            suggested_slots = _calculate_suggested_slots(supabase, tenant)
        except Exception as e:
            print(f"[post-call] Suggested slots calculation failed: {e}")

    # ── 8. Update call with triage + language data ──
    notification_priority = (
        "high" if triage_result["urgency"] in ("emergency", "high_ticket") else "standard"
    )

    supabase.table("calls").update({
        "urgency_classification": triage_result["urgency"],
        "urgency_confidence": triage_result.get("confidence"),
        "triage_layer_used": triage_result.get("layer"),
        "detected_language": detected_language,
        "language_barrier": language_barrier,
        "barrier_language": detected_language if language_barrier else None,
        "suggested_slots": suggested_slots,
        "notification_priority": notification_priority,
    }).eq("call_id", call_id).execute()

    # Set booking_outcome to not_attempted if still null
    supabase.table("calls").update({"booking_outcome": "not_attempted"}).eq("call_id", call_id).is_("booking_outcome", "null").execute()

    # ── 9. Create/merge lead ──
    lead = None
    if call_uuid and duration_seconds >= 15:
        try:
            caller_name = _extract_field_from_transcript(transcript_turns, "name")
            job_type = _extract_field_from_transcript(transcript_turns, "job")

            appointment_id = None
            if booking_outcome == "booked":
                appt_resp = (
                    supabase.table("appointments")
                    .select("id")
                    .eq("call_id", call_uuid)
                    .limit(1)
                    .execute()
                )
                appt_row = appt_resp.data[0] if appt_resp.data else None
                appointment_id = appt_row.get("id") if appt_row else None

            lead = await create_or_merge_lead(supabase, {
                "tenant_id": tenant_id,
                "call_id": call_uuid,
                "from_number": from_number,
                "caller_name": caller_name,
                "job_type": job_type,
                "triage_result": {"urgency": triage_result["urgency"]},
                "appointment_id": appointment_id,
                "call_duration": duration_seconds,
            })
        except Exception as e:
            print(f"[post-call] Lead creation error: {e}")

    # ── 10. Send owner notifications ──
    if tenant_id and tenant:
        try:
            tenant_info_resp = (
                supabase.table("tenants")
                .select("business_name, owner_phone, owner_email, notification_preferences")
                .eq("id", tenant_id)
                .single()
                .execute()
            )
            tenant_info = tenant_info_resp.data

            if tenant_info and lead:
                call_row_resp = (
                    supabase.table("calls")
                    .select("booking_outcome")
                    .eq("call_id", call_id)
                    .single()
                    .execute()
                )
                call_row = call_row_resp.data
                final_outcome = (call_row.get("booking_outcome") if call_row else None) or "not_attempted"
                is_emergency = triage_result["urgency"] == "emergency"

                prefs = tenant_info.get("notification_preferences") or {}
                if is_emergency:
                    outcome_prefs = {"sms": True, "email": True}
                else:
                    outcome_prefs = prefs.get(final_outcome, {"sms": True, "email": True})

                callback_link = f"tel:{lead.get('from_number', '') or from_number}"
                dashboard_link = f"{os.environ.get('NEXT_PUBLIC_APP_URL', 'https://localhost:3000')}/dashboard/leads"
                business_name = tenant_info.get("business_name", "Your Business")

                tasks = []

                if outcome_prefs.get("sms") and tenant_info.get("owner_phone"):
                    tasks.append(send_owner_sms(
                        to=tenant_info["owner_phone"],
                        business_name=business_name,
                        caller_name=lead.get("caller_name"),
                        job_type=lead.get("job_type"),
                        urgency=triage_result["urgency"],
                        address=lead.get("service_address"),
                        callback_link=callback_link,
                        dashboard_link=dashboard_link,
                    ))

                if outcome_prefs.get("email") and tenant_info.get("owner_email"):
                    tasks.append(send_owner_email(
                        to=tenant_info["owner_email"],
                        lead=lead,
                        business_name=business_name,
                        dashboard_url=dashboard_link,
                    ))

                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    statuses = ", ".join(
                        f"{'first' if i == 0 else 'second'}={'fulfilled' if not isinstance(r, Exception) else 'rejected'}"
                        for i, r in enumerate(results)
                    )
                    print(f"[post-call] Owner notify: tenant={tenant_id} outcome={final_outcome} emergency={is_emergency} {statuses}")
        except Exception as e:
            print(f"[post-call] Notification error: {e}")

    print(
        f"[post-call] Complete: callId={call_id} duration={duration_seconds}s "
        f"urgency={triage_result['urgency']} outcome={booking_outcome or 'not_attempted'} "
        f"language={detected_language or 'unknown'}"
    )


# ─── Helper functions ────────────────────────────────────────────────────────

_SPANISH_MARKERS = [
    re.compile(r"\bhola\b"),
    re.compile(r"\bgracias\b"),
    re.compile(r"\bpor favor\b"),
    re.compile(r"\bbuenos?\s*d[ií]as?\b"),
    re.compile(r"\bbuenas?\s*tardes?\b"),
    re.compile(r"\bnecesito\b"),
    re.compile(r"\btengo\b"),
    re.compile(r"\bquiero\b"),
    re.compile(r"\bpuede\b"),
    re.compile(r"\bayuda\b"),
]


def _detect_language_from_transcript(turns):
    caller_text = " ".join(
        t["content"] for t in turns if t.get("role") == "user"
    ).lower()

    if not caller_text or len(caller_text) < 5:
        return None

    spanish_matches = sum(1 for p in _SPANISH_MARKERS if p.search(caller_text))
    if spanish_matches >= 2:
        return "es"

    return "en"


def _extract_field_from_transcript(turns, field):
    """Best-effort field extraction from transcript.
    The AI tool calls capture this data more accurately during the call.
    """
    return None


def _calculate_suggested_slots(supabase, tenant):
    if not tenant or not tenant.get("working_hours"):
        return None

    tenant_timezone = tenant.get("tenant_timezone", "America/Chicago")

    appointments_resp = (
        supabase.table("appointments")
        .select("start_time, end_time, zone_id")
        .eq("tenant_id", tenant["id"])
        .neq("status", "cancelled")
        .execute()
    )
    events_resp = (
        supabase.table("calendar_events")
        .select("start_time, end_time")
        .eq("tenant_id", tenant["id"])
        .execute()
    )
    zones_resp = (
        supabase.table("service_zones")
        .select("id, name, postal_codes")
        .eq("tenant_id", tenant["id"])
        .execute()
    )
    buffers_resp = (
        supabase.table("zone_travel_buffers")
        .select("zone_a_id, zone_b_id, buffer_mins")
        .eq("tenant_id", tenant["id"])
        .execute()
    )

    collected_slots = []
    for d in range(3):
        if len(collected_slots) >= 3:
            break
        target_date = datetime.now(timezone.utc) + timedelta(days=d + 1)
        target_date_str = to_local_date_string(target_date, tenant_timezone)

        day_slots = calculate_available_slots(
            working_hours=tenant["working_hours"],
            slot_duration_mins=tenant.get("slot_duration_mins", 60),
            existing_bookings=appointments_resp.data or [],
            external_blocks=events_resp.data or [],
            zones=zones_resp.data or [],
            zone_pair_buffers=format_zone_pair_buffers(buffers_resp.data or []),
            target_date=target_date_str,
            tenant_timezone=tenant_timezone,
            max_slots=3 - len(collected_slots),
        )
        collected_slots.extend(day_slots)

    return collected_slots if collected_slots else None
