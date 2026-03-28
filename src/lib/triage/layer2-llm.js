/**
 * Layer 2: LLM-based urgency scorer for home service call triage.
 * Called only when Layer 1 is not confident (ambiguous transcripts).
 * Uses Groq (Llama 4 Scout) for fast, low-cost structured JSON output.
 */

import OpenAI from 'openai';

let _client = null;
function getClient() {
  if (!_client) {
    _client = new OpenAI({
      apiKey: process.env.GROQ_API_KEY,
      baseURL: 'https://api.groq.com/openai/v1',
    });
  }
  return _client;
}

/**
 * Score the urgency of a call transcript using Groq (Llama 4 Scout).
 *
 * @param {string} transcript - The call transcript text.
 * @returns {Promise<{ urgency: 'emergency'|'routine'|'high_ticket', confidence: 'high'|'medium'|'low', reason: string }>}
 */
export async function runLLMScorer(transcript) {
  const response = await getClient().chat.completions.create({
    model: 'meta-llama/llama-4-scout-17b-16e-instruct',
    messages: [
      {
        role: 'system',
        content: `You classify home service calls. Return ONLY a JSON object: {"urgency": "emergency"|"routine"|"high_ticket", "confidence": "high"|"medium"|"low", "reason": "one sentence"}
Emergency: immediate safety risk, happening right now, property damage ongoing.
High-ticket: job likely > $500, complex install/replacement (not repair).
Routine: future scheduling, quote requests, non-urgent repairs.`,
      },
      { role: 'user', content: `Call transcript:\n${transcript}` },
    ],
    response_format: { type: 'json_object' },
    max_tokens: 100,
    temperature: 0,
  });

  try {
    return JSON.parse(response.choices[0].message.content);
  } catch {
    return { urgency: 'routine', confidence: 'low', reason: 'parse error' };
  }
}
