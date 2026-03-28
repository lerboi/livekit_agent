/**
 * Build a structured whisper message for warm transfer.
 * Per D-08: "[Name] calling about [job type]. [Emergency/Routine]. [1-line summary]."
 * Ported from src/lib/whisper-message.js — identical logic.
 */
export function buildWhisperMessage({
  callerName,
  jobType,
  urgency,
  summary,
}: {
  callerName?: string;
  jobType?: string;
  urgency?: string;
  summary?: string;
} = {}): string {
  const name = callerName || 'Unknown caller';
  const job = jobType || 'unspecified job';
  const tier = urgency === 'emergency' ? 'Emergency' : 'Routine';
  return `${name} calling about ${job}. ${tier}. ${summary || ''}`.trim();
}
