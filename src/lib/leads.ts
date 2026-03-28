import type { SupabaseClient } from '@supabase/supabase-js';

/**
 * createOrMergeLead — creates a new lead or attaches a repeat caller to an existing open lead.
 * Adapted from src/lib/leads.js — accepts supabase client as parameter.
 *
 * Pipeline rules:
 *  - Calls under 15 seconds are ignored (return null)
 *  - If an open lead (status: new|booked) exists for this caller, attach the call to it
 *  - If no open lead exists, create a new lead
 *  - New lead status is 'booked' when an appointmentId is provided, otherwise 'new'
 *  - Every new lead creation logs an activity_log entry
 */
export async function createOrMergeLead(
  supabase: SupabaseClient,
  {
    tenantId,
    callId,
    fromNumber,
    callerName,
    jobType,
    serviceAddress,
    triageResult,
    appointmentId,
    callDuration,
  }: {
    tenantId: string;
    callId: string;
    fromNumber: string;
    callerName?: string | null;
    jobType?: string | null;
    serviceAddress?: string | null;
    triageResult?: { urgency?: string };
    appointmentId?: string | null;
    callDuration: number;
  },
): Promise<Record<string, unknown> | null> {
  // 1. Short call filter
  if (callDuration < 15) {
    return null;
  }

  // 2. Look for existing open lead
  const { data: existingLead } = await supabase
    .from('leads')
    .select('id, status')
    .eq('tenant_id', tenantId)
    .eq('from_number', fromNumber)
    .in('status', ['new', 'booked'])
    .order('created_at', { ascending: false })
    .limit(1)
    .maybeSingle();

  if (existingLead) {
    // 3. Repeat caller — attach call to existing lead
    await supabase.from('lead_calls').insert({
      lead_id: existingLead.id,
      call_id: callId,
    });
    return existingLead;
  }

  // 4. New lead
  const newLeadStatus = appointmentId ? 'booked' : 'new';
  const urgency = triageResult?.urgency || 'routine';

  const { data: insertedLeads, error } = await supabase
    .from('leads')
    .insert([
      {
        tenant_id: tenantId,
        from_number: fromNumber,
        caller_name: callerName || null,
        job_type: jobType || null,
        service_address: serviceAddress || null,
        urgency,
        status: newLeadStatus,
        primary_call_id: callId,
        appointment_id: appointmentId || null,
      },
    ])
    .select('id, status, from_number, urgency, caller_name, job_type');

  if (error) {
    console.error('createOrMergeLead: insert error', error);
    throw error;
  }

  const newLead = insertedLeads?.[0];

  // 5. Insert into lead_calls junction
  await supabase.from('lead_calls').insert({
    lead_id: newLead.id,
    call_id: callId,
  });

  // 6. Log activity
  await supabase.from('activity_log').insert({
    tenant_id: tenantId,
    event_type: 'lead_created',
    lead_id: newLead.id,
    metadata: {
      caller_name: callerName || null,
      job_type: jobType || null,
      urgency,
    },
  });

  return newLead;
}
