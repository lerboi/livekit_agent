/**
 * Layer 1: Keyword/regex classifier for home service call triage.
 * Runs synchronously. Returns confident:true when a keyword match is found.
 * Patterns hoisted to module level per Vercel best practice (avoids re-compilation per call).
 *
 * Note: Routine patterns are checked BEFORE emergency patterns to prevent
 * "not urgent" from matching the emergency "urgent" pattern.
 */

const EMERGENCY_PATTERNS = [
  /\b(flooding|flooded|flood)\b/i,
  /\bgas\s*(smell|leak|line)\b/i,
  /\bno\s*(heat|hot\s*water)\b/i,
  /\bsewer\s*(backup|overflow)\b/i,
  /\bpipe\s*(burst|broke|broken)\b/i,
  /\belectrical\s*(fire|sparks?|smoke)\b/i,
  /\bcarbon\s*monoxide\b/i,
  /\b(right\s*now|happening\s*now|emergency|urgent)\b/i,
];

const ROUTINE_PATTERNS = [
  /\b(quote|estimate|next\s*(week|month)|sometime|schedule)\b/i,
  /\b(not\s*urgent|whenever|no\s*rush)\b/i,
];

/**
 * Classify a call transcript using keyword matching.
 *
 * @param {string|null} transcript - The call transcript text.
 * @returns {{ result: 'emergency'|'routine', confident: boolean, matched?: string }}
 */
export function runKeywordClassifier(transcript) {
  if (!transcript || transcript.length < 10) {
    return { result: 'routine', confident: false };
  }

  // Check routine patterns first — prevents "not urgent" from triggering emergency "urgent" match
  for (const pattern of ROUTINE_PATTERNS) {
    if (pattern.test(transcript)) {
      return { result: 'routine', confident: true, matched: pattern.source };
    }
  }

  for (const pattern of EMERGENCY_PATTERNS) {
    if (pattern.test(transcript)) {
      return { result: 'emergency', confident: true, matched: pattern.source };
    }
  }

  return { result: 'routine', confident: false };
}
