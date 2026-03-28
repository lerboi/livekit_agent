/**
 * capture_lead tool — saves caller info as a lead when they decline booking.
 * Ported from handleCaptureLead in src/app/api/webhooks/retell/route.js.
 */

import { llm } from '@livekit/agents';
import { z } from 'zod';
import { createOrMergeLead } from '../lib/leads.js';
import type { ToolDeps } from './types.js';

export function createCaptureLeadTool(deps: ToolDeps) {
  return llm.tool({
    description:
      'Capture caller information as a lead when they decline booking. ' +
      'Use after the second explicit decline. Must be used before ending the call.',
    parameters: z.object({
      caller_name: z.string().describe('Caller full name — always ask for and include this'),
      phone: z.string().optional().describe('Caller phone number if provided'),
      address: z.string().optional().describe('Service address if provided'),
      job_type: z.string().optional().describe('Type of job or service needed'),
      notes: z.string().optional().describe('Any additional context from the conversation'),
    }),
    execute: async ({ caller_name, phone, address, job_type, notes }) => {
      if (!deps.tenantId) {
        return "I've noted your details and someone will follow up.";
      }

      // Compute mid-call duration from startTimestamp (avoids 15s filter issue)
      const durationSeconds = Math.round((Date.now() - deps.startTimestamp) / 1000);

      try {
        await createOrMergeLead(deps.supabase, {
          tenantId: deps.tenantId,
          callId: deps.callUuid || deps.callId,
          fromNumber: deps.fromNumber || phone || '',
          callerName: caller_name || null,
          jobType: job_type || null,
          serviceAddress: address || null,
          triageResult: { urgency: 'routine' },
          appointmentId: null,
          callDuration: durationSeconds,
        });

        // Write booking_outcome: 'declined' (conditional — don't overwrite 'booked')
        await deps.supabase
          .from('calls')
          .update({ booking_outcome: 'declined' })
          .eq('call_id', deps.callId)
          .is('booking_outcome', null);

        // Look up business name for confirmation message
        const { data: tenant } = await deps.supabase
          .from('tenants')
          .select('business_name')
          .eq('id', deps.tenantId)
          .single();

        const bizName = tenant?.business_name || 'our team';
        return `I've saved your information. ${bizName} will reach out soon.`;
      } catch (err) {
        console.error('[agent] capture_lead error:', err);
        return "I've noted your details and someone will follow up.";
      }
    },
  });
}
