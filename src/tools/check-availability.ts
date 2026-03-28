/**
 * check_availability tool — real-time slot query.
 * Ported from handleCheckAvailability in src/app/api/webhooks/retell/route.js.
 * Now executes in-process with direct Supabase access (zero network hops).
 */

import { llm } from '@livekit/agents';
import { z } from 'zod';
import { format } from 'date-fns';
import { toZonedTime } from 'date-fns-tz';
import { calculateAvailableSlots } from '../lib/slot-calculator.js';
import { toLocalDateString, formatZonePairBuffers } from '../utils.js';
import type { ToolDeps } from './types.js';

export function createCheckAvailabilityTool(deps: ToolDeps) {
  return llm.tool({
    description:
      'Check real-time appointment availability for specific dates. ' +
      'Use before offering slots to the caller, when the caller asks about a specific date or time, ' +
      'or when previously shown slots may be outdated.',
    parameters: z.object({
      date: z
        .string()
        .optional()
        .describe('Target date in YYYY-MM-DD format. If the caller says "next Tuesday", convert to the actual date. If omitted, checks today and the next 2 days.'),
      urgency: z
        .enum(['emergency', 'routine', 'high_ticket'])
        .optional()
        .describe('Urgency level — affects which slots are prioritized'),
    }),
    execute: async ({ date, urgency }) => {
      if (!deps.tenantId) {
        return 'I was unable to check availability right now. Let me take your information and someone will call you back to schedule.';
      }

      const { data: tenant } = await deps.supabase
        .from('tenants')
        .select('tenant_timezone, working_hours, slot_duration_mins, business_name')
        .eq('id', deps.tenantId)
        .single();

      const tenantTimezone = tenant?.tenant_timezone || 'America/Chicago';

      // Fetch live scheduling data (same parallel pattern as handleInbound)
      const [appointmentsResult, eventsResult, zonesResult, buffersResult] = await Promise.all([
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

      // Determine which dates to check
      let datesToCheck: string[] = [];
      if (date) {
        datesToCheck = [date];
      } else {
        for (let dayOffset = 0; dayOffset < 3; dayOffset++) {
          const d = new Date();
          d.setDate(d.getDate() + dayOffset);
          datesToCheck.push(toLocalDateString(d, tenantTimezone));
        }
      }

      // Calculate slots across requested dates (up to 6 total)
      const allSlots: Array<{ start: string; end: string }> = [];
      for (const dateStr of datesToCheck) {
        if (allSlots.length >= 6) break;

        const daySlots = calculateAvailableSlots({
          workingHours: tenant?.working_hours || {},
          slotDurationMins: tenant?.slot_duration_mins || 60,
          existingBookings: appointmentsResult.data || [],
          externalBlocks: eventsResult.data || [],
          zones: zonesResult.data || [],
          zonePairBuffers: formatZonePairBuffers(buffersResult.data || []),
          targetDate: dateStr,
          tenantTimezone,
          maxSlots: 6 - allSlots.length,
        });
        allSlots.push(...daySlots);
      }

      if (allSlots.length === 0) {
        const dateLabel = date
          ? format(toZonedTime(new Date(date + 'T12:00:00Z'), tenantTimezone), 'EEEE, MMMM do')
          : 'the next few days';
        return `No available slots for ${dateLabel}. Ask the caller if another date works, or capture their information so ${tenant?.business_name || 'the team'} can call back to schedule.`;
      }

      // Format slots as numbered list with ISO data for booking
      const slotsText = allSlots
        .map((slot, i) => {
          const zonedStart = toZonedTime(new Date(slot.start), tenantTimezone);
          return `${i + 1}. ${format(zonedStart, "EEEE MMMM do 'at' h:mm a")} (start: ${slot.start}, end: ${slot.end})`;
        })
        .join('\n');

      return `Available slots:\n${slotsText}\n\nPresent these to the caller naturally (without the ISO times). Use the start/end values when invoking book_appointment.`;
    },
  });
}
