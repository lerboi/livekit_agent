/**
 * Layer 3: Owner rules — service-tag lookup for urgency override.
 * Adapted from src/lib/triage/layer3-rules.js — accepts supabase client as parameter.
 * Never downgrades, only escalates.
 */

import type { SupabaseClient } from '@supabase/supabase-js';

const SEVERITY: Record<string, number> = {
  emergency: 3,
  high_ticket: 2,
  routine: 1,
};

export async function applyOwnerRules(
  supabase: SupabaseClient,
  baseUrgency: string,
  tenant_id: string,
  detected_service: string | null = null,
): Promise<{ urgency: string; escalated: boolean }> {
  const { data: services, error } = await supabase
    .from('services')
    .select('name, urgency_tag')
    .eq('tenant_id', tenant_id)
    .eq('is_active', true);

  if (error || !services?.length) {
    return { urgency: baseUrgency, escalated: false };
  }

  let matchedTag: string | null = null;
  if (detected_service) {
    const normalizedDetected = detected_service.toLowerCase();
    const match = services.find(
      (s: any) =>
        s.name.toLowerCase().includes(normalizedDetected) ||
        normalizedDetected.includes(s.name.toLowerCase()),
    );
    if (match) {
      matchedTag = match.urgency_tag;
    }
  }

  if (!matchedTag) {
    if (services.length === 1) {
      matchedTag = services[0].urgency_tag;
    } else {
      matchedTag = baseUrgency;
    }
  }

  const baseSeverity = SEVERITY[baseUrgency] || 1;
  const tagSeverity = SEVERITY[matchedTag!] || 1;

  if (tagSeverity > baseSeverity) {
    return { urgency: matchedTag!, escalated: true };
  }

  return { urgency: baseUrgency, escalated: false };
}
