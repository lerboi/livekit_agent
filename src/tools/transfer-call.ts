/**
 * transfer_call tool — SIP REFER transfer to business owner.
 * Ported from handleTransferCall in src/app/api/webhooks/retell/route.js.
 * Uses LiveKit SIP transfer instead of retell.call.transfer().
 */

import { llm } from '@livekit/agents';
import { z } from 'zod';
import { SipClient } from 'livekit-server-sdk';
import { buildWhisperMessage } from '../lib/whisper-message.js';
import type { ToolDeps } from './types.js';

export function createTransferCallTool(deps: ToolDeps) {
  return llm.tool({
    description:
      "Transfer the current call to the business owner's phone number. " +
      'Use when the caller explicitly requests a human, or after 3 failed clarification attempts. ' +
      'Always capture caller info (name, phone, issue) first, unless the caller explicitly requests immediate transfer.',
    parameters: z.object({
      caller_name: z.string().optional().describe('Caller full name if captured'),
      job_type: z.string().optional().describe('Type of job or service needed'),
      urgency: z
        .enum(['emergency', 'routine', 'high_ticket'])
        .optional()
        .describe('Urgency level detected from conversation'),
      summary: z.string().optional().describe('1-line summary of caller request for the receiving human'),
      reason: z
        .enum(['caller_requested', 'clarification_limit'])
        .optional()
        .describe('Why the transfer is happening'),
    }),
    execute: async ({ caller_name, job_type, urgency, summary, reason }) => {
      if (!deps.ownerPhone) {
        return 'transfer_unavailable';
      }

      // Write exception_reason to calls record
      const exceptionReason =
        reason ||
        (summary?.toLowerCase().includes('clarif') ? 'clarification_limit' : 'caller_requested');

      await deps.supabase
        .from('calls')
        .update({ exception_reason: exceptionReason })
        .eq('call_id', deps.callId);

      // Build whisper context (spoken by agent before transfer for context)
      const whisperContext = buildWhisperMessage({
        callerName: caller_name,
        jobType: job_type,
        urgency,
        summary,
      });
      console.log(`[agent] Transfer context: ${whisperContext}`);

      // Perform SIP REFER transfer via LiveKit
      try {
        const sipClient = new SipClient(
          process.env.LIVEKIT_URL!,
          process.env.LIVEKIT_API_KEY!,
          process.env.LIVEKIT_API_SECRET!,
        );

        await sipClient.transferSipParticipant(
          deps.roomName,
          deps.sipParticipantIdentity,
          `sip:${deps.ownerPhone}@pstn.twilio.com`,
        );

        return 'transfer_initiated';
      } catch (err) {
        console.error('[agent] Transfer failed:', err);
        return 'transfer_failed';
      }
    },
  });
}
