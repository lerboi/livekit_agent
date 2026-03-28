/**
 * Notification service for the LiveKit agent.
 * Adapted from src/lib/notifications.js — same logic, no React Email dependency
 * (email template is inline HTML instead of React component).
 */

import twilio from 'twilio';
import { Resend } from 'resend';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const en = JSON.parse(readFileSync(join(__dirname, '..', 'messages', 'en.json'), 'utf-8'));
const es = JSON.parse(readFileSync(join(__dirname, '..', 'messages', 'es.json'), 'utf-8'));

// ─── Lazy-instantiated clients ────────────────────────────────────────────────

let twilioClient: ReturnType<typeof twilio> | null = null;
function getTwilioClient() {
  if (!twilioClient) {
    twilioClient = twilio(
      process.env.TWILIO_ACCOUNT_SID,
      process.env.TWILIO_AUTH_TOKEN,
    );
  }
  return twilioClient;
}

let resendClient: Resend | null = null;
function getResendClient() {
  if (!resendClient) {
    resendClient = new Resend(process.env.RESEND_API_KEY);
  }
  return resendClient;
}

// ─── Interpolation helper ────────────────────────────────────────────────────

function interpolate(template: string, vars: Record<string, string>): string {
  return Object.entries(vars).reduce(
    (str, [key, val]) => str.replaceAll(`{${key}}`, val ?? ''),
    template,
  );
}

// ─── Owner SMS alert ──────────────────────────────────────────────────────────

export async function sendOwnerSMS({
  to,
  businessName,
  callerName,
  jobType,
  urgency,
  address,
  callbackLink,
  dashboardLink,
}: {
  to: string;
  businessName: string;
  callerName?: string;
  jobType?: string;
  urgency?: string;
  address?: string;
  callbackLink: string;
  dashboardLink: string;
}) {
  const isEmergency = urgency === 'emergency';
  const name = callerName || 'Unknown';
  const job = jobType || 'General inquiry';
  const addr = address || 'No address';

  const body = isEmergency
    ? `EMERGENCY: ${businessName} — ${name} needs urgent ${job} at ${addr}. Call NOW: ${callbackLink} | Dashboard: ${dashboardLink}`
    : `${businessName}: New booking — ${name}, ${job} at ${addr}. Callback: ${callbackLink} | Dashboard: ${dashboardLink}`;

  try {
    const result = await getTwilioClient().messages.create({
      body,
      from: process.env.TWILIO_FROM_NUMBER,
      to,
    });
    console.log('[notifications] Owner SMS sent:', result.sid);
    return result;
  } catch (err: any) {
    console.error('[notifications] Owner SMS failed:', err?.message || err);
  }
}

// ─── Owner email alert ────────────────────────────────────────────────────────

export async function sendOwnerEmail({
  to,
  lead,
  businessName,
  dashboardUrl,
}: {
  to: string;
  lead: Record<string, any>;
  businessName: string;
  dashboardUrl: string;
}) {
  const urgency = lead?.urgency_classification || lead?.urgency || 'routine';
  const isEmergency = urgency === 'emergency';
  const callerName = lead?.caller_name || 'Unknown caller';

  const subject = isEmergency
    ? `EMERGENCY: New booking — ${callerName}`
    : `New booking — ${callerName}`;

  // Plain HTML email (no React Email dependency in the agent)
  const html = `
    <h2>${isEmergency ? '🚨 EMERGENCY' : '📞 New Lead'}: ${callerName}</h2>
    <p><strong>Business:</strong> ${businessName}</p>
    <p><strong>Job Type:</strong> ${lead?.job_type || 'Not specified'}</p>
    <p><strong>Address:</strong> ${lead?.service_address || 'Not provided'}</p>
    <p><strong>Phone:</strong> ${lead?.from_number || 'Unknown'}</p>
    <p><strong>Urgency:</strong> ${urgency}</p>
    <p><a href="${dashboardUrl}">View in Dashboard</a></p>
  `;

  try {
    const result = await getResendClient().emails.send({
      from: process.env.RESEND_FROM_EMAIL || 'alerts@getvoco.ai',
      to,
      subject,
      html,
    });
    console.log('[notifications] Owner email sent:', result?.data?.id);
    return result;
  } catch (err: any) {
    console.error('[notifications] Owner email failed:', err?.message || err);
  }
}

// ─── Caller recovery SMS ──────────────────────────────────────────────────────

export async function sendCallerRecoverySMS({
  to,
  callerName,
  businessName,
  locale,
  urgency,
}: {
  to: string | null;
  callerName?: string | null;
  businessName: string;
  locale?: string;
  urgency?: string;
}): Promise<{ success: boolean; sid?: string; error?: { code: string | number; message: string } }> {
  if (!to) {
    console.warn('[notifications] sendCallerRecoverySMS skipped: no phone number');
    return { success: false, error: { code: 'NO_PHONE', message: 'No phone number provided' } };
  }

  const translations = locale === 'es' ? es : en;
  const isEmergency = urgency === 'emergency';
  const firstName = callerName?.split(' ')[0] || 'there';

  const templateKey = isEmergency
    ? 'recovery_sms_attempted_emergency'
    : 'recovery_sms_attempted_routine';

  const body = interpolate(translations.notifications[templateKey], {
    business_name: businessName || 'Your service provider',
    first_name: firstName,
  });

  try {
    const result = await getTwilioClient().messages.create({
      body,
      from: process.env.TWILIO_FROM_NUMBER,
      to,
    });
    console.log('[notifications] Caller recovery SMS sent:', result.sid);
    return { success: true, sid: result.sid };
  } catch (err: any) {
    const code = err?.code || 'UNKNOWN';
    const message = err?.message || String(err);
    console.error('[notifications] Caller recovery SMS failed:', message);
    return { success: false, error: { code, message } };
  }
}

// ─── Caller booking confirmation SMS ─────────────────────────────────────────

export async function sendCallerSMS({
  to,
  businessName,
  date,
  time,
  address,
  locale,
}: {
  to: string | null;
  businessName: string;
  date: string;
  time: string;
  address: string;
  locale: string;
}) {
  if (!to) {
    console.warn('[notifications] sendCallerSMS skipped: no phone number');
    return;
  }

  const translations = locale === 'es' ? es : en;
  const body = interpolate(translations.notifications.booking_confirmation, {
    business_name: businessName || 'Your service provider',
    date: date || '',
    time: time || '',
    address: address || '',
  });

  try {
    const result = await getTwilioClient().messages.create({
      body,
      from: process.env.TWILIO_FROM_NUMBER,
      to,
    });
    console.log('[notifications] Caller SMS sent:', result.sid);
    return result;
  } catch (err: any) {
    console.error('[notifications] Caller SMS failed:', err?.message || err);
  }
}
