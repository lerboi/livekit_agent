import { format } from 'date-fns';
import { toZonedTime } from 'date-fns-tz';
import { calculateAvailableSlots } from './lib/slot-calculator.js';

/**
 * Format a UTC Date into natural speech for AI to read aloud.
 * Example: "Tuesday March 23rd at 10 AM"
 */
export function formatSlotForSpeech(date, timezone) {
  const zoned = toZonedTime(date, timezone || 'America/Chicago');
  return format(zoned, "EEEE MMMM do 'at' h:mm a");
}

/**
 * Format a Date object into a "YYYY-MM-DD" string in the given timezone.
 */
export function toLocalDateString(date, timezone) {
  const zoned = toZonedTime(date, timezone || 'America/Chicago');
  return format(zoned, 'yyyy-MM-dd');
}

/**
 * Format zone_travel_buffers array — pass through as-is.
 * calculateAvailableSlots handles { zone_a_id, zone_b_id, buffer_mins } objects.
 */
export function formatZonePairBuffers(buffers) {
  return buffers || [];
}

/**
 * Calculate initial slots for today + next 2 days (same logic as handleInbound).
 * Returns formatted numbered list string.
 */
export async function calculateInitialSlots(supabase, tenant) {
  const tenantTimezone = tenant.tenant_timezone || 'America/Chicago';

  // Fetch scheduling data in parallel
  const [appointmentsResult, eventsResult, zonesResult, buffersResult] = await Promise.all([
    supabase
      .from('appointments')
      .select('start_time, end_time, zone_id')
      .eq('tenant_id', tenant.id)
      .neq('status', 'cancelled')
      .gte('end_time', new Date().toISOString()),
    supabase
      .from('calendar_events')
      .select('start_time, end_time')
      .eq('tenant_id', tenant.id)
      .gte('end_time', new Date().toISOString()),
    supabase
      .from('service_zones')
      .select('id, name, postal_codes')
      .eq('tenant_id', tenant.id),
    supabase
      .from('zone_travel_buffers')
      .select('zone_a_id, zone_b_id, buffer_mins')
      .eq('tenant_id', tenant.id),
  ]);

  const allSlots = [];
  for (let dayOffset = 0; dayOffset < 3 && allSlots.length < 6; dayOffset++) {
    const targetDate = new Date();
    targetDate.setDate(targetDate.getDate() + dayOffset);
    const targetDateStr = toLocalDateString(targetDate, tenantTimezone);

    const daySlots = calculateAvailableSlots({
      workingHours: tenant.working_hours || {},
      slotDurationMins: tenant.slot_duration_mins || 60,
      existingBookings: appointmentsResult.data || [],
      externalBlocks: eventsResult.data || [],
      zones: zonesResult.data || [],
      zonePairBuffers: formatZonePairBuffers(buffersResult.data || []),
      targetDate: targetDateStr,
      tenantTimezone,
      maxSlots: 6 - allSlots.length,
    });
    allSlots.push(...daySlots);
  }

  if (allSlots.length === 0) return '';

  return allSlots
    .map((slot, i) => {
      const zonedStart = toZonedTime(new Date(slot.start), tenantTimezone);
      return `${i + 1}. ${format(zonedStart, "EEEE MMMM do 'at' h:mm a")}`;
    })
    .join('\n');
}
