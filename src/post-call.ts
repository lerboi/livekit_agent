/**
 * Post-call pipeline — runs when the AgentSession closes.
 * Combines the logic from processCallEnded() and processCallAnalyzed() in call-processor.js.
 * Both stages run in-process immediately (no webhook delay).
 */

import type { SupabaseClient } from '@supabase/supabase-js';
import { classifyCall } from './lib/triage/classifier.js';
import { createOrMergeLead } from './lib/leads.js';
import { sendOwnerSMS, sendOwnerEmail } from './lib/notifications.js';
import { calculateAvailableSlots } from './lib/slot-calculator.js';
import { toLocalDateString, formatZonePairBuffers } from './utils.js';
import type { TenantRow } from './utils.js';

// Supported languages — calls in other languages trigger language barrier tag
const SUPPORTED_LANGUAGES = new Set(['en', 'es']);

export interface PostCallParams {
  supabase: SupabaseClient;
  callId: string;
  callUuid: string | null;
  tenantId: string | null;
  tenant: TenantRow | null;
  fromNumber: string;
  toNumber: string;
  startTimestamp: number;
  endTimestamp: number;
  transcriptTurns: Array<{ role: string; content: string; timestamp: number }>;
  recordingStoragePath: string | null;
  isTestCall: boolean;
  disconnectionReason?: string;
}

export async function runPostCallPipeline(params: PostCallParams): Promise<void> {
  const {
    supabase,
    callId,
    tenantId,
    tenant,
    fromNumber,
    toNumber,
    startTimestamp,
    endTimestamp,
    transcriptTurns,
    recordingStoragePath,
    isTestCall,
    disconnectionReason,
  } = params;

  const durationSeconds = Math.round((endTimestamp - startTimestamp) / 1000);

  // ── 1. Build transcript data ──
  const transcriptText = transcriptTurns
    .map((t) => `${t.role === 'user' ? 'Caller' : 'AI'}: ${t.content}`)
    .join('\n');

  const transcriptStructured = transcriptTurns.map((t) => ({
    role: t.role,
    content: t.content,
  }));

  // ── 2. Update call record with transcript + recording ──
  const { data: updatedCall } = await supabase
    .from('calls')
    .update({
      status: 'analyzed',
      end_timestamp: endTimestamp,
      recording_storage_path: recordingStoragePath,
      transcript_text: transcriptText || null,
      transcript_structured: transcriptStructured.length > 0 ? transcriptStructured : null,
      disconnection_reason: disconnectionReason || 'agent_hangup',
    })
    .eq('call_id', callId)
    .select('id, booking_outcome')
    .single();

  const callUuid = updatedCall?.id || params.callUuid;

  // ── 3. Test call auto-cancel ──
  if (isTestCall && tenantId) {
    try {
      const { data: testAppt } = await supabase
        .from('appointments')
        .select('id')
        .eq('call_id', callUuid)
        .eq('tenant_id', tenantId)
        .maybeSingle();

      if (testAppt) {
        await supabase.from('appointments').update({ status: 'cancelled' }).eq('id', testAppt.id);
        await supabase
          .from('leads')
          .update({ status: 'new', appointment_id: null })
          .eq('appointment_id', testAppt.id)
          .eq('tenant_id', tenantId);
      }
    } catch (err) {
      console.error('[post-call] Test call auto-cancel error:', err);
    }
  }

  // ── 4. Usage tracking ──
  if (!isTestCall && tenantId && durationSeconds >= 10) {
    try {
      const { data: usageResult, error: usageError } = await supabase.rpc('increment_calls_used', {
        p_tenant_id: tenantId,
        p_call_id: callId,
      });

      if (usageError) {
        console.error('[post-call] increment_calls_used RPC error:', usageError);
      } else if (usageResult?.[0]) {
        const { success, calls_used, calls_limit, limit_exceeded } = usageResult[0];
        console.log(
          `[post-call] usage: tenant=${tenantId} success=${success} used=${calls_used}/${calls_limit} exceeded=${limit_exceeded}`,
        );

        // Report overage to Stripe
        if (success && limit_exceeded) {
          try {
            const { data: sub } = await supabase
              .from('subscriptions')
              .select('overage_stripe_item_id')
              .eq('tenant_id', tenantId)
              .eq('is_current', true)
              .maybeSingle();

            if (sub?.overage_stripe_item_id) {
              const Stripe = (await import('stripe')).default;
              const stripe = new Stripe(process.env.STRIPE_SECRET_KEY!);
              await stripe.subscriptionItems.createUsageRecord(sub.overage_stripe_item_id, {
                quantity: 1,
                action: 'increment',
              });
              console.log(`[post-call] Overage reported to Stripe: tenant=${tenantId}`);
            }
          } catch (overageErr) {
            console.error('[post-call] Stripe overage report failed (non-fatal):', overageErr);
          }
        }
      }
    } catch (err) {
      console.error('[post-call] Usage tracking error (non-fatal):', err);
    }
  }

  // Skip remaining pipeline if no tenant
  if (!tenantId) {
    console.error(`[post-call] No tenant for ${toNumber} — skipping triage/lead/notification`);
    return;
  }

  // ── 5. Language barrier detection ──
  const detectedLanguage = detectLanguageFromTranscript(transcriptTurns);
  const languageBarrier = detectedLanguage != null && !SUPPORTED_LANGUAGES.has(detectedLanguage);

  // ── 6. Triage classification ──
  let triageResult = { urgency: 'routine', confidence: 'low', layer: 'layer1' };
  try {
    triageResult = await classifyCall(supabase, {
      transcript: transcriptText,
      tenant_id: tenantId,
    });
  } catch (err) {
    console.error('[post-call] Triage classification failed:', err);
  }

  // ── 7. Calculate suggested slots for unbooked calls ──
  let suggestedSlots = null;
  const bookingOutcome = updatedCall?.booking_outcome;

  if (!bookingOutcome || bookingOutcome === 'not_attempted') {
    try {
      suggestedSlots = await calculateSuggestedSlots(supabase, tenant);
    } catch (err) {
      console.error('[post-call] Suggested slots calculation failed:', err);
    }
  }

  // ── 8. Update call with triage + language data ──
  const notificationPriority =
    triageResult.urgency === 'emergency' || triageResult.urgency === 'high_ticket' ? 'high' : 'standard';

  await supabase
    .from('calls')
    .update({
      urgency_classification: triageResult.urgency,
      urgency_confidence: triageResult.confidence,
      triage_layer_used: triageResult.layer,
      detected_language: detectedLanguage,
      language_barrier: languageBarrier,
      barrier_language: languageBarrier ? detectedLanguage : null,
      suggested_slots: suggestedSlots,
      notification_priority: notificationPriority,
    })
    .eq('call_id', callId);

  // Set booking_outcome to not_attempted if still null
  await supabase
    .from('calls')
    .update({ booking_outcome: 'not_attempted' })
    .eq('call_id', callId)
    .is('booking_outcome', null);

  // ── 9. Create/merge lead ──
  if (callUuid && durationSeconds >= 15) {
    try {
      const callerName = extractFieldFromTranscript(transcriptTurns, 'name');
      const jobType = extractFieldFromTranscript(transcriptTurns, 'job');

      // Look up appointmentId if a booking was made
      let appointmentId: string | null = null;
      if (bookingOutcome === 'booked') {
        const { data: apptRow } = await supabase
          .from('appointments')
          .select('id')
          .eq('call_id', callUuid)
          .maybeSingle();
        appointmentId = apptRow?.id || null;
      }

      var lead = await createOrMergeLead(supabase, {
        tenantId,
        callId: callUuid,
        fromNumber,
        callerName,
        jobType,
        triageResult: { urgency: triageResult.urgency },
        appointmentId,
        callDuration: durationSeconds,
      });
    } catch (err) {
      console.error('[post-call] Lead creation error:', err);
    }
  }

  // ── 10. Send owner notifications ──
  if (tenantId && tenant) {
    try {
      const { data: tenantInfo } = await supabase
        .from('tenants')
        .select('business_name, owner_phone, owner_email, notification_preferences')
        .eq('id', tenantId)
        .single();

      if (tenantInfo && lead) {
        // Read the booking_outcome for this call
        const { data: callRow } = await supabase
          .from('calls')
          .select('booking_outcome')
          .eq('call_id', callId)
          .single();

        const finalOutcome = callRow?.booking_outcome || 'not_attempted';
        const isEmergency = triageResult.urgency === 'emergency';

        const prefs = tenantInfo.notification_preferences || {};
        const outcomePrefs = isEmergency
          ? { sms: true, email: true }
          : (prefs as Record<string, { sms: boolean; email: boolean }>)[finalOutcome] || {
              sms: true,
              email: true,
            };

        const callbackLink = `tel:${lead?.from_number || fromNumber}`;
        const dashboardLink = `${process.env.NEXT_PUBLIC_APP_URL || 'https://localhost:3000'}/dashboard/leads`;
        const businessName = tenantInfo.business_name || 'Your Business';

        const promises: Promise<any>[] = [];

        if (outcomePrefs.sms && tenantInfo.owner_phone) {
          promises.push(
            sendOwnerSMS({
              to: tenantInfo.owner_phone,
              businessName,
              callerName: (lead as any)?.caller_name,
              jobType: (lead as any)?.job_type,
              urgency: triageResult.urgency,
              address: (lead as any)?.service_address,
              callbackLink,
              dashboardLink,
            }),
          );
        }

        if (outcomePrefs.email && tenantInfo.owner_email) {
          promises.push(
            sendOwnerEmail({
              to: tenantInfo.owner_email,
              lead: lead as Record<string, any>,
              businessName,
              dashboardUrl: dashboardLink,
            }),
          );
        }

        if (promises.length > 0) {
          Promise.allSettled(promises).then((results) => {
            const statuses = results.map((r, i) => `${i === 0 ? 'first' : 'second'}=${r.status}`).join(', ');
            console.log(
              `[post-call] Owner notify: tenant=${tenantId} outcome=${finalOutcome} emergency=${isEmergency} ${statuses}`,
            );
          });
        }
      }
    } catch (err) {
      console.error('[post-call] Notification error:', err);
    }
  }

  console.log(
    `[post-call] Complete: callId=${callId} duration=${durationSeconds}s urgency=${triageResult.urgency} ` +
      `outcome=${bookingOutcome || 'not_attempted'} language=${detectedLanguage || 'unknown'}`,
  );
}

// ─── Helper functions ────────────────────────────────────────────────────────

/**
 * Detect the language from transcript turns.
 * Simple heuristic: check for Spanish markers in caller speech.
 */
function detectLanguageFromTranscript(
  turns: Array<{ role: string; content: string }>,
): string | null {
  const callerText = turns
    .filter((t) => t.role === 'user')
    .map((t) => t.content)
    .join(' ')
    .toLowerCase();

  if (!callerText || callerText.length < 5) return null;

  // Spanish markers
  const spanishMarkers = [
    /\bhola\b/,
    /\bgracias\b/,
    /\bpor favor\b/,
    /\bbuenos?\s*d[ií]as?\b/,
    /\bbuenas?\s*tardes?\b/,
    /\bnecesito\b/,
    /\btengo\b/,
    /\bquiero\b/,
    /\bpuede\b/,
    /\bayuda\b/,
  ];

  const spanishMatches = spanishMarkers.filter((p) => p.test(callerText)).length;
  if (spanishMatches >= 2) return 'es';

  // Default to English if no strong markers
  return 'en';
}

/**
 * Extract a field from transcript turns (best-effort).
 * This is a simple heuristic — the AI tool calls capture this data more accurately.
 */
function extractFieldFromTranscript(
  turns: Array<{ role: string; content: string }>,
  field: 'name' | 'job',
): string | null {
  // The AI should have already captured this via tool calls.
  // This is a fallback for the post-call pipeline.
  // In practice, the lead is usually already created mid-call via capture_lead or book_appointment.
  return null;
}

/**
 * Calculate suggested slots for unbooked calls (same as processCallAnalyzed).
 */
async function calculateSuggestedSlots(
  supabase: SupabaseClient,
  tenant: TenantRow | null,
): Promise<Array<{ start: string; end: string }> | null> {
  if (!tenant?.working_hours) return null;

  const tenantTimezone = tenant.tenant_timezone || 'America/Chicago';

  const [appointments, events, zones, buffers] = await Promise.all([
    supabase
      .from('appointments')
      .select('start_time, end_time, zone_id')
      .eq('tenant_id', tenant.id)
      .neq('status', 'cancelled'),
    supabase.from('calendar_events').select('start_time, end_time').eq('tenant_id', tenant.id),
    supabase.from('service_zones').select('id, name, postal_codes').eq('tenant_id', tenant.id),
    supabase.from('zone_travel_buffers').select('zone_a_id, zone_b_id, buffer_mins').eq('tenant_id', tenant.id),
  ]);

  const collectedSlots: Array<{ start: string; end: string }> = [];
  for (let d = 0; d < 3 && collectedSlots.length < 3; d++) {
    const targetDate = new Date();
    targetDate.setDate(targetDate.getDate() + d + 1);
    const targetDateStr = toLocalDateString(targetDate, tenantTimezone);

    const daySlots = calculateAvailableSlots({
      workingHours: tenant.working_hours,
      slotDurationMins: tenant.slot_duration_mins || 60,
      existingBookings: appointments.data || [],
      externalBlocks: events.data || [],
      zones: zones.data || [],
      zonePairBuffers: formatZonePairBuffers(buffers.data || []),
      targetDate: targetDateStr,
      tenantTimezone,
      maxSlots: 3 - collectedSlots.length,
    });
    collectedSlots.push(...daySlots);
  }

  return collectedSlots.length > 0 ? collectedSlots : null;
}
