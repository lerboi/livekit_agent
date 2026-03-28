/**
 * book_appointment tool — atomic slot booking.
 * Ported from handleBookAppointment in src/app/api/webhooks/retell/route.js.
 * All side effects (calendar sync, SMS, recovery SMS) run in-process.
 */

import { llm } from '@livekit/agents';
import { z } from 'zod';
import { format } from 'date-fns';
import { toZonedTime } from 'date-fns-tz';
import { atomicBookSlot } from '../lib/booking.js';
import { calculateAvailableSlots } from '../lib/slot-calculator.js';
import { sendCallerSMS, sendCallerRecoverySMS } from '../lib/notifications.js';
import { formatSlotForSpeech, toLocalDateString, formatZonePairBuffers } from '../utils.js';
import type { ToolDeps } from './types.js';

// Google Calendar push — lazy import to avoid circular dependency
let pushBookingToCalendar: ((tenantId: string, appointmentId: string) => Promise<void>) | null = null;

export function createBookAppointmentTool(deps: ToolDeps) {
  return llm.tool({
    description:
      'Book a confirmed appointment slot. Only use after: ' +
      '(1) collecting caller name and service address, ' +
      '(2) reading back the address and receiving verbal confirmation, ' +
      '(3) the caller has selected a slot from the availability results. ' +
      'Do NOT ask the caller about urgency — infer it from the conversation.',
    parameters: z.object({
      slot_start: z.string().describe('ISO 8601 datetime of appointment start'),
      slot_end: z.string().describe('ISO 8601 datetime of appointment end'),
      service_address: z.string().describe('Verbally confirmed service address'),
      caller_name: z.string().describe('Caller full name'),
      urgency: z
        .enum(['emergency', 'routine', 'high_ticket'])
        .describe('Inferred from conversation — emergency if active leak/flood/no heat/gas/sparks, otherwise routine'),
    }),
    execute: async ({ slot_start, slot_end, service_address, caller_name, urgency }) => {
      if (!slot_start || !slot_end) {
        return 'I need a bit more information to complete the booking. Could you confirm the time you would like?';
      }

      if (!deps.tenantId) {
        return 'I was unable to confirm the booking. Please call back and we will try again.';
      }

      // Fetch tenant timezone and config
      const { data: tenant } = await deps.supabase
        .from('tenants')
        .select('tenant_timezone, working_hours, slot_duration_mins, business_name, default_locale')
        .eq('id', deps.tenantId)
        .single();

      const tenantTimezone = tenant?.tenant_timezone || 'America/Chicago';
      const startTime = new Date(slot_start);
      const endTime = new Date(slot_end);

      // Attempt atomic slot booking
      let result: { success: boolean; appointment_id?: string; reason?: string };
      try {
        result = await atomicBookSlot(deps.supabase, {
          tenantId: deps.tenantId,
          callId: deps.callUuid,
          startTime,
          endTime,
          address: service_address || 'Address to be confirmed',
          callerName: caller_name || 'Caller',
          callerPhone: deps.fromNumber,
          urgency: urgency || 'routine',
          zoneId: null,
        });
      } catch (bookingErr) {
        console.error('[agent] atomicBookSlot error:', bookingErr);
        return 'I was unable to confirm the booking right now. Let me take your information and someone will call you back to schedule.';
      }

      if (!result.success) {
        // Slot was taken — recalculate next available
        const [currentBookings, currentEvents, currentZones, currentBuffers] = await Promise.all([
          deps.supabase
            .from('appointments')
            .select('start_time, end_time, zone_id')
            .eq('tenant_id', deps.tenantId)
            .neq('status', 'cancelled')
            .gte('end_time', new Date().toISOString()),
          deps.supabase
            .from('calendar_events')
            .select('start_time, end_time')
            .eq('tenant_id', deps.tenantId)
            .gte('end_time', new Date().toISOString()),
          deps.supabase
            .from('service_zones')
            .select('id, name, postal_codes')
            .eq('tenant_id', deps.tenantId),
          deps.supabase
            .from('zone_travel_buffers')
            .select('zone_a_id, zone_b_id, buffer_mins')
            .eq('tenant_id', deps.tenantId),
        ]);

        const endDateStr = toLocalDateString(endTime, tenantTimezone);
        const nextSlots = calculateAvailableSlots({
          workingHours: tenant?.working_hours || {},
          slotDurationMins: tenant?.slot_duration_mins || 60,
          existingBookings: currentBookings.data || [],
          externalBlocks: currentEvents.data || [],
          zones: currentZones.data || [],
          zonePairBuffers: formatZonePairBuffers(currentBuffers.data || []),
          targetDate: endDateStr,
          tenantTimezone,
          maxSlots: 1,
        });

        const nextSlotText =
          nextSlots.length > 0
            ? formatSlotForSpeech(new Date(nextSlots[0].start), tenantTimezone)
            : 'tomorrow morning';

        // Write booking_outcome: 'attempted'
        await deps.supabase
          .from('calls')
          .update({ booking_outcome: 'attempted' })
          .eq('call_id', deps.callId)
          .is('booking_outcome', null);

        // Send recovery SMS (non-blocking)
        sendRecoverySMS(deps, tenant, urgency, caller_name).catch((err) =>
          console.error('[agent] Recovery SMS failed:', err),
        );

        return `That slot was just taken. The next available time is ${nextSlotText}. Would you like me to book that instead?`;
      }

      // Success — async side effects (non-blocking)

      // Calendar sync
      try {
        // Dynamic import to avoid circular dependency issues
        const { pushBookingToCalendar: pushFn } = await import('../lib/google-calendar.js');
        pushFn(deps.tenantId!, result.appointment_id!).catch((err: Error) =>
          console.error('[agent] Calendar push failed:', err),
        );
      } catch {
        // google-calendar module not available — skip
      }

      // Write booking_outcome: 'booked'
      await deps.supabase
        .from('calls')
        .update({ booking_outcome: 'booked' })
        .eq('call_id', deps.callId);

      // Caller SMS confirmation (non-blocking)
      const smsLocale = tenant?.default_locale || 'en';
      sendCallerSMS({
        to: deps.fromNumber,
        businessName: tenant?.business_name || 'Your service provider',
        date: format(toZonedTime(startTime, tenantTimezone), 'EEEE, MMMM do'),
        time: format(toZonedTime(startTime, tenantTimezone), 'h:mm a'),
        address: service_address || '',
        locale: smsLocale,
      }).catch((err) => console.error('[agent] Caller SMS failed:', err));

      const formattedTime = formatSlotForSpeech(startTime, tenantTimezone);
      return `Your appointment is confirmed for ${formattedTime}. You will receive a confirmation. Is there anything else I can help you with?`;
    },
  });
}

/**
 * Send recovery SMS on failed booking — same logic as handleBookAppointment after() blocks.
 */
async function sendRecoverySMS(
  deps: ToolDeps,
  tenant: Record<string, any> | null,
  urgency: string,
  callerName: string | null,
) {
  try {
    const locale = tenant?.default_locale || 'en';

    // Write pending status
    await deps.supabase
      .from('calls')
      .update({
        recovery_sms_status: 'pending',
        recovery_sms_last_attempt_at: new Date().toISOString(),
      })
      .eq('call_id', deps.callId);

    const deliveryResult = await sendCallerRecoverySMS({
      to: deps.fromNumber,
      callerName,
      businessName: tenant?.business_name || 'Your service provider',
      locale,
      urgency: urgency || 'routine',
    });

    // Write delivery result
    await deps.supabase
      .from('calls')
      .update({
        recovery_sms_status: deliveryResult.success ? 'sent' : 'retrying',
        recovery_sms_retry_count: deliveryResult.success ? 0 : 1,
        recovery_sms_last_error: deliveryResult.success
          ? null
          : `${deliveryResult.error!.code}: ${deliveryResult.error!.message}`,
        recovery_sms_last_attempt_at: new Date().toISOString(),
        recovery_sms_sent_at: deliveryResult.success ? new Date().toISOString() : null,
      })
      .eq('call_id', deps.callId);
  } catch (err: any) {
    console.error('[agent] Recovery SMS pipeline failed:', err?.message || err);
    // Write error state for cron retry pickup
    await deps.supabase
      .from('calls')
      .update({
        recovery_sms_status: 'retrying',
        recovery_sms_retry_count: 1,
        recovery_sms_last_error: `AGENT_ERROR: ${err?.message || String(err)}`,
        recovery_sms_last_attempt_at: new Date().toISOString(),
      })
      .eq('call_id', deps.callId)
      .catch(() => {}); // last-resort swallow
  }
}
