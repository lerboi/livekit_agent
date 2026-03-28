import type { SupabaseClient } from '@supabase/supabase-js';
import type { JobContext } from '@livekit/agents';
import type { TenantRow } from '../utils.js';

export interface ToolDeps {
  supabase: SupabaseClient;
  tenant: TenantRow | null;
  tenantId: string | null;
  callId: string;
  callUuid: string | null;
  fromNumber: string;
  toNumber: string;
  ownerPhone: string | null;
  startTimestamp: number;
  onboardingComplete: boolean;
  tenantTimezone: string;
  roomName: string;
  sipParticipantIdentity: string;
  ctx: JobContext;
}
