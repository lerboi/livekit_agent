/**
 * Three-layer triage classifier orchestrator.
 * Adapted from src/lib/triage/classifier.js — accepts supabase client as parameter.
 * Pipeline: Layer 1 (regex) → Layer 2 (LLM, if ambiguous) → Layer 3 (owner rules).
 */

import type { SupabaseClient } from '@supabase/supabase-js';
import { runKeywordClassifier } from './layer1-keywords.js';
import { runLLMScorer } from './layer2-llm.js';
import { applyOwnerRules } from './layer3-rules.js';

export async function classifyCall(
  supabase: SupabaseClient,
  {
    transcript,
    tenant_id,
    detected_service = null,
  }: {
    transcript: string;
    tenant_id: string;
    detected_service?: string | null;
  },
): Promise<{ urgency: string; confidence: string; layer: string; reason?: string }> {
  if (!transcript || transcript.length < 10) {
    return { urgency: 'routine', confidence: 'low', layer: 'layer1' };
  }

  const layer1Result = runKeywordClassifier(transcript);

  if (layer1Result.confident) {
    const layer3Result = await applyOwnerRules(supabase, layer1Result.result, tenant_id, detected_service);
    const finalLayer = layer3Result.escalated ? 'layer3' : 'layer1';
    return { urgency: layer3Result.urgency, confidence: 'high', layer: finalLayer };
  }

  const layer2Result = await runLLMScorer(transcript);
  const layer3Result = await applyOwnerRules(supabase, layer2Result.urgency, tenant_id, detected_service);
  const finalLayer = layer3Result.escalated ? 'layer3' : 'layer2';

  return {
    urgency: layer3Result.urgency,
    confidence: layer2Result.confidence ?? 'low',
    layer: finalLayer,
    reason: layer2Result.reason,
  };
}
