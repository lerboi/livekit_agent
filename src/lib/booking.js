/**
 * Atomically book a slot by calling the book_appointment_atomic Supabase RPC.
 * Adapted from src/lib/scheduling/booking.js — accepts supabase client as parameter.
 */
export async function atomicBookSlot(
  supabase,
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
  },
) {
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
