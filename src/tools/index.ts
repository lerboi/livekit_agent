/**
 * Tool registry — conditionally registers tools based on onboarding state.
 * Same pattern as getTools(onboardingComplete) in server.js.
 */

import { createCheckAvailabilityTool } from './check-availability.js';
import { createBookAppointmentTool } from './book-appointment.js';
import { createCaptureLeadTool } from './capture-lead.js';
import { createCheckCallerHistoryTool } from './check-caller-history.js';
import { createTransferCallTool } from './transfer-call.js';
import { createEndCallTool } from './end-call.js';
import type { ToolDeps } from './types.js';

/**
 * Create all tools for the voice agent session.
 *
 * Tool ordering matches the current system:
 * - transfer_call, capture_lead, check_caller_history, end_call — always available
 * - check_availability, book_appointment — only when onboarding_complete
 */
export function createTools(deps: ToolDeps) {
  const tools: Record<string, ReturnType<typeof createEndCallTool>> = {
    transfer_call: createTransferCallTool(deps),
    capture_lead: createCaptureLeadTool(deps),
    check_caller_history: createCheckCallerHistoryTool(deps),
    end_call: createEndCallTool(deps),
  };

  if (deps.onboardingComplete) {
    tools.check_availability = createCheckAvailabilityTool(deps);
    tools.book_appointment = createBookAppointmentTool(deps);
  }

  return tools;
}

export type { ToolDeps } from './types.js';
