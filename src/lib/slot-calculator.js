import { addMinutes, parseISO } from 'date-fns';
import { toZonedTime, fromZonedTime } from 'date-fns-tz';

/**
 * Day-of-week key names matching the workingHours config shape.
 */
const DAY_KEYS = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];

/**
 * Parse a "HH:MM" time string into { hours, minutes }.
 */
function parseTime(timeStr) {
  const [hours, minutes] = timeStr.split(':').map(Number);
  return { hours, minutes };
}

/**
 * Build a UTC Date from a local date string ("YYYY-MM-DD") and a local time string ("HH:MM")
 * in the given IANA timezone.
 */
function localTimeToUTC(dateStr, timeStr, timezone) {
  const { hours, minutes } = parseTime(timeStr);
  // Construct a local datetime string and convert to UTC via fromZonedTime
  const localDatetime = new Date(`${dateStr}T${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:00`);
  return fromZonedTime(localDatetime, timezone);
}

/**
 * Check whether two intervals [aStart, aEnd) and [bStart, bEnd) overlap.
 * Uses exclusive end: start < other.end && end > other.start
 */
function intervalsOverlap(aStart, aEnd, bStart, bEnd) {
  return aStart < bEnd && aEnd > bStart;
}

/**
 * Resolve the travel buffer in minutes between the last booking and a candidate slot.
 *
 * Logic:
 * - If zones array is empty (no zones configured): flat 30-min buffer
 * - If last booking has no zone_id or candidate has no zone: 30-min buffer (cross-zone default)
 * - If last booking's zone_id === candidateZoneId: 0-min buffer (same zone)
 * - If different zones: look up zonePairBuffers; default 30-min if no entry
 *
 * @param {string|null} lastBookingZoneId
 * @param {string|null} candidateZoneId
 * @param {Array}  zones            Array of { id, name } zone objects
 * @param {Array}  zonePairBuffers  Array of { zone_a_id, zone_b_id, buffer_mins }
 * @returns {number} buffer in minutes
 */
function getTravelBufferMins(lastBookingZoneId, candidateZoneId, zones, zonePairBuffers) {
  // No zones configured at all — flat 30-min buffer
  if (!zones || zones.length === 0) {
    return 30;
  }

  // No zone info on one or both sides — treat as cross-zone (30min default)
  if (!lastBookingZoneId || !candidateZoneId) {
    return 30;
  }

  // Same zone — no buffer
  if (lastBookingZoneId === candidateZoneId) {
    return 0;
  }

  // Different zones — look for a custom buffer entry
  if (zonePairBuffers && zonePairBuffers.length > 0) {
    const pair = zonePairBuffers.find(
      (p) =>
        (p.zone_a_id === lastBookingZoneId && p.zone_b_id === candidateZoneId) ||
        (p.zone_a_id === candidateZoneId && p.zone_b_id === lastBookingZoneId)
    );
    if (pair) {
      return pair.buffer_mins;
    }
  }

  // Default cross-zone buffer
  return 30;
}

/**
 * Calculate available booking slots for a given date.
 *
 * @param {object} config
 * @param {object} config.workingHours      - Day-keyed working hours config
 * @param {number} config.slotDurationMins  - Slot length in minutes (e.g., 60)
 * @param {Array}  config.existingBookings  - Array of { start_time, end_time, zone_id? } (ISO strings)
 * @param {Array}  config.externalBlocks    - Array of { start_time, end_time } (ISO strings)
 * @param {Array}  config.zones             - Array of { id, name } zone objects
 * @param {Array}  config.zonePairBuffers   - Array of { zone_a_id, zone_b_id, buffer_mins }
 * @param {string} config.targetDate        - "YYYY-MM-DD" date string
 * @param {string} config.tenantTimezone    - IANA timezone (e.g., "America/Chicago")
 * @param {number} config.maxSlots          - Maximum slots to return
 * @param {string} [config.candidateZoneId] - Zone ID for the candidate booking (for buffer calc)
 * @returns {Array<{ start: string, end: string }>} Available slots as ISO strings
 */
export function calculateAvailableSlots({
  workingHours,
  slotDurationMins,
  existingBookings = [],
  externalBlocks = [],
  zones = [],
  zonePairBuffers = [],
  targetDate,
  tenantTimezone,
  maxSlots = 10,
  candidateZoneId = null,
}) {
  // Determine the day of week from the target date
  // Parse as local midnight to get correct day-of-week
  const [year, month, day] = targetDate.split('-').map(Number);
  // Create a date object representing midnight in the tenant timezone to get the correct weekday
  const localMidnight = fromZonedTime(new Date(year, month - 1, day, 0, 0, 0), tenantTimezone);
  // Get the weekday in the tenant's local timezone
  const zonedMidnight = toZonedTime(localMidnight, tenantTimezone);
  const dayKey = DAY_KEYS[zonedMidnight.getDay()];

  const dayConfig = workingHours?.[dayKey];

  // Day off or missing config — no slots
  if (!dayConfig || !dayConfig.enabled) {
    return [];
  }

  const { open, close, lunchStart, lunchEnd } = dayConfig;

  // Convert working hours to UTC Date objects
  const windowStart = localTimeToUTC(targetDate, open, tenantTimezone);
  const windowEnd = localTimeToUTC(targetDate, close, tenantTimezone);

  // Lunch block in UTC (if configured)
  const lunchStartUTC = lunchStart ? localTimeToUTC(targetDate, lunchStart, tenantTimezone) : null;
  const lunchEndUTC = lunchEnd ? localTimeToUTC(targetDate, lunchEnd, tenantTimezone) : null;

  // Parse existing bookings to Date objects
  const parsedBookings = existingBookings.map((b) => ({
    start: new Date(b.start_time),
    end: new Date(b.end_time),
    zone_id: b.zone_id || null,
  }));

  // Parse external blocks to Date objects
  const parsedBlocks = externalBlocks.map((b) => ({
    start: new Date(b.start_time),
    end: new Date(b.end_time),
  }));

  const available = [];
  let cursor = new Date(windowStart);

  // Skip past slots when calculating for today — don't offer times that have already passed
  const now = new Date();
  if (cursor < now && now < windowEnd) {
    // Only advance if we're within today's working window
    // Check if targetDate is actually today in the tenant timezone
    const zonedNow = toZonedTime(now, tenantTimezone);
    const todayStr = `${zonedNow.getFullYear()}-${String(zonedNow.getMonth() + 1).padStart(2, '0')}-${String(zonedNow.getDate()).padStart(2, '0')}`;
    if (targetDate === todayStr) {
      cursor = new Date(now);
    }
  }

  while (cursor < windowEnd && available.length < maxSlots) {
    const slotStart = new Date(cursor);
    const slotEnd = addMinutes(slotStart, slotDurationMins);

    // Slot must fit within the working window
    if (slotEnd > windowEnd) {
      break;
    }

    // Skip slots that overlap with the lunch break
    if (lunchStartUTC && lunchEndUTC) {
      if (intervalsOverlap(slotStart, slotEnd, lunchStartUTC, lunchEndUTC)) {
        cursor = addMinutes(cursor, slotDurationMins);
        continue;
      }
    }

    // Check overlap with existing bookings
    const bookedOverlap = parsedBookings.some((b) =>
      intervalsOverlap(slotStart, slotEnd, b.start, b.end)
    );
    if (bookedOverlap) {
      cursor = addMinutes(cursor, slotDurationMins);
      continue;
    }

    // Check overlap with external calendar blocks
    const externalOverlap = parsedBlocks.some((b) =>
      intervalsOverlap(slotStart, slotEnd, b.start, b.end)
    );
    if (externalOverlap) {
      cursor = addMinutes(cursor, slotDurationMins);
      continue;
    }

    // Travel buffer check: find the last booking that ends before this slot starts
    const bookingsBefore = parsedBookings.filter((b) => b.end <= slotStart);
    if (bookingsBefore.length > 0) {
      // Find the one that ends latest
      const lastBooking = bookingsBefore.reduce((latest, b) =>
        b.end > latest.end ? b : latest
      );

      const bufferMins = getTravelBufferMins(
        lastBooking.zone_id,
        candidateZoneId,
        zones,
        zonePairBuffers
      );

      if (bufferMins > 0) {
        const earliestStart = addMinutes(lastBooking.end, bufferMins);
        if (slotStart < earliestStart) {
          cursor = addMinutes(cursor, slotDurationMins);
          continue;
        }
      }
    }

    // Slot passes all checks
    available.push({
      start: slotStart.toISOString(),
      end: slotEnd.toISOString(),
    });

    cursor = addMinutes(cursor, slotDurationMins);
  }

  return available;
}
