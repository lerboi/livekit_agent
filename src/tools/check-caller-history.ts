/**
 * check_caller_history tool — repeat caller awareness.
 * Ported from handleCheckCallerHistory in src/app/api/webhooks/retell/route.js.
 * Read-only — no database writes.
 */

import { llm } from '@livekit/agents';
import { z } from 'zod';
import { formatSlotForSpeech } from '../utils.js';
import type { ToolDeps } from './types.js';

export function createCheckCallerHistoryTool(deps: ToolDeps) {
  return llm.tool({
    description:
      'Check caller history for repeat caller awareness. No parameters needed. ' +
      'Invoke after greeting, before first question.',
    parameters: z.object({}),
    execute: async () => {
      if (!deps.tenantId || !deps.fromNumber) {
        return 'No caller history available.';
      }

      // Look up tenant timezone for formatting
      const { data: tenant } = await deps.supabase
        .from('tenants')
        .select('tenant_timezone')
        .eq('id', deps.tenantId)
        .single();

      const tenantTimezone = tenant?.tenant_timezone || 'America/Chicago';

      // Parallel lookup: leads + appointments for this caller
      const [leadsResult, appointmentsResult] = await Promise.all([
        deps.supabase
          .from('leads')
          .select('id, caller_name, job_type, service_address, status, created_at')
          .eq('tenant_id', deps.tenantId)
          .eq('from_number', deps.fromNumber)
          .order('created_at', { ascending: false })
          .limit(3),
        deps.supabase
          .from('appointments')
          .select('start_time, end_time, service_address, status, caller_name')
          .eq('tenant_id', deps.tenantId)
          .eq('caller_phone', deps.fromNumber)
          .neq('status', 'cancelled')
          .gte('end_time', new Date().toISOString())
          .order('start_time', { ascending: true })
          .limit(3),
      ]);

      const leads = leadsResult.data || [];
      const appointments = appointmentsResult.data || [];

      if (leads.length === 0 && appointments.length === 0) {
        return 'First-time caller. No prior history found.';
      }

      // Build natural-language summary for the AI
      let summary = '';

      if (appointments.length > 0) {
        const apptLines = appointments.map((a) => {
          const dateStr = formatSlotForSpeech(new Date(a.start_time), tenantTimezone);
          return `- ${dateStr} at ${a.service_address || 'address on file'} (${a.status})`;
        });
        summary += `Upcoming appointments:\n${apptLines.join('\n')}\n\n`;
      }

      if (leads.length > 0) {
        const leadLines = leads.map((l) => {
          const name = l.caller_name || 'Unknown';
          const job = l.job_type || 'unspecified';
          return `- ${name}: ${job} (status: ${l.status})`;
        });
        summary += `Previous interactions:\n${leadLines.join('\n')}`;
      }

      return `Returning caller. ${summary}\n\nAcknowledge their history naturally. If they have an upcoming appointment, ask if this call is about that appointment or something new. If they have both an appointment AND an open lead, mention the appointment first, then ask if this is about that or a new issue.`;
    },
  });
}
