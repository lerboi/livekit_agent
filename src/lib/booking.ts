import type { SupabaseClient } from '@supabase/supabase-js';

/**
 * Atomically book a slot by calling the book_appointment_atomic Supabase RPC.
 * Adapted from src/lib/scheduling/booking.js — accepts supabase client as parameter.
 */
export async function atomicBookSlot(
  supabase: SupabaseClient,
  {
    tenantId,
    callId,
    startTime,
    endTime,
    address,
    callerName,
    callerPhone,
    urgency,
    zoneId,
  }: {
    tenantId: string;
    callId: string | null;
    startTime: Date;
    endTime: Date;
    address: string;
    callerName: string;
    callerPhone: string | null;
    urgency: string;
    zoneId: string | null;
  },
): Promise<{ success: boolean; appointment_id?: string; reason?: string }> {
  const { data, error } = await supabase.rpc('book_appointment_atomic', {
    p_tenant_id: tenantId,
    p_call_id: callId,
    p_start_time: startTime.toISOString(),
    p_end_time: endTime.toISOString(),
    p_service_address: address,
    p_caller_name: callerName,
    p_caller_phone: callerPhone,
    p_urgency: urgency,
    p_zone_id: zoneId || null,
  });

  if (error) {
    throw error;
  }

  return data;
}
