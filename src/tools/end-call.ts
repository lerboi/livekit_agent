/**
 * end_call tool — graceful call termination.
 * Ported from the end_call handler in server.js.
 * Gemini generates the farewell, then we disconnect the SIP participant.
 */

import { llm } from '@livekit/agents';
import { z } from 'zod';
import { RoomServiceClient } from 'livekit-server-sdk';
import type { ToolDeps } from './types.js';

export function createEndCallTool(deps: ToolDeps) {
  return llm.tool({
    description:
      'End the call gracefully after all actions are complete. ' +
      'Always capture caller information before using this if no booking was made.',
    parameters: z.object({}),
    execute: async () => {
      // Gemini will generate the farewell from prompt instructions.
      // After farewell is spoken, disconnect the SIP participant.
      // Use a short delay to let the farewell audio play out.
      setTimeout(async () => {
        try {
          const roomService = new RoomServiceClient(
            process.env.LIVEKIT_URL!,
            process.env.LIVEKIT_API_KEY!,
            process.env.LIVEKIT_API_SECRET!,
          );
          await roomService.removeParticipant(deps.roomName, deps.sipParticipantIdentity);
        } catch (err) {
          console.error('[agent] Failed to disconnect SIP participant:', err);
        }
      }, 3000); // 3s delay for farewell to play

      return 'Call ending.';
    },
  });
}
