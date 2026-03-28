/**
 * Google Calendar integration for the LiveKit agent.
 * Lightweight adapter — delegates to the same calendar logic as the Next.js app.
 * Uses google-auth-library and googleapis directly (same deps as the main app).
 */

import { getSupabaseAdmin } from '../supabase.js';

/**
 * Push a booking to Google Calendar.
 * Same logic as src/lib/scheduling/google-calendar.js pushBookingToCalendar().
 * This is a best-effort operation — failures are logged but never thrown.
 */
export async function pushBookingToCalendar(
  tenantId: string,
  appointmentId: string,
): Promise<void> {
  const supabase = getSupabaseAdmin();

  try {
    // Check if tenant has calendar credentials
    const { data: creds } = await supabase
      .from('calendar_credentials')
      .select('access_token, refresh_token, expiry_date, calendar_id')
      .eq('tenant_id', tenantId)
      .eq('provider', 'google')
      .maybeSingle();

    if (!creds) {
      // No calendar configured — silently skip
      return;
    }

    // Fetch the appointment details
    const { data: appointment } = await supabase
      .from('appointments')
      .select('start_time, end_time, service_address, caller_name, urgency, notes')
      .eq('id', appointmentId)
      .single();

    if (!appointment) return;

    // Fetch tenant business name
    const { data: tenant } = await supabase
      .from('tenants')
      .select('business_name')
      .eq('id', tenantId)
      .single();

    const isUrgent = appointment.urgency === 'emergency';
    const titlePrefix = isUrgent ? '[URGENT] ' : '';
    const summary = `${titlePrefix}${appointment.caller_name || 'Customer'} - ${tenant?.business_name || 'Appointment'}`;

    // Use googleapis to create the event
    const { google } = await import('googleapis');
    const { OAuth2Client } = await import('google-auth-library');

    const oauth2Client = new OAuth2Client(
      process.env.GOOGLE_CLIENT_ID,
      process.env.GOOGLE_CLIENT_SECRET,
    );

    oauth2Client.setCredentials({
      access_token: creds.access_token,
      refresh_token: creds.refresh_token,
      expiry_date: creds.expiry_date ? Number(creds.expiry_date) : undefined,
    });

    const calendar = google.calendar({ version: 'v3', auth: oauth2Client });

    const event = await calendar.events.insert({
      calendarId: creds.calendar_id || 'primary',
      requestBody: {
        summary,
        description: [
          `Service Address: ${appointment.service_address || 'TBD'}`,
          `Urgency: ${appointment.urgency}`,
          appointment.notes ? `Notes: ${appointment.notes}` : '',
        ]
          .filter(Boolean)
          .join('\n'),
        start: { dateTime: appointment.start_time },
        end: { dateTime: appointment.end_time },
      },
    });

    // Store the Google event ID
    if (event.data.id) {
      await supabase
        .from('appointments')
        .update({ google_event_id: event.data.id })
        .eq('id', appointmentId);
    }

    console.log(`[agent] Calendar event created: ${event.data.id}`);
  } catch (err: any) {
    console.error('[agent] Calendar push failed (non-fatal):', err?.message || err);
  }
}
