/**
 * Patch @livekit/agents-plugin-google for gemini-3.1-flash-live-preview:
 * 1. Use new Gemini audio API (audio instead of deprecated media_chunks)
 * 2. Remove unsupported transcription/session fields from connect config
 * 3. Log config for debugging
 */

import { readFileSync, writeFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const filePath = resolve(
  __dirname,
  '../node_modules/@livekit/agents-plugin-google/dist/beta/realtime/realtime_api.js',
);

let content = readFileSync(filePath, 'utf8');
let patchCount = 0;

// Fix 1: sendRealtimeInput({ media: mediaChunk }) → sendRealtimeInput({ audio: mediaChunk })
const before1 = content;
content = content.replace(
  /sendRealtimeInput\(\{\s*media:\s*mediaChunk\s*\}\)/g,
  'sendRealtimeInput({ audio: mediaChunk })',
);
if (content !== before1) patchCount++;

// Fix 2: Before "return config;" in buildConnectConfig(), delete unsupported fields + log
const before2 = content;
content = content.replace(
  /(\s+)return config;\s*\n(\s+)\}\s*\n(\s+)startNewGeneration/,
  '$1delete config.inputAudioTranscription;\n$1delete config.outputAudioTranscription;\n$1delete config.sessionResumption;\n$1console.log("[gemini-patch] Config keys:", Object.keys(config).join(", "));\n$1return config;\n$2}\n$3startNewGeneration',
);
if (content !== before2) patchCount++;

writeFileSync(filePath, content, 'utf8');
console.log(`[patch] Applied ${patchCount}/2 Gemini 3.1 fixes`);
if (patchCount < 2) {
  console.warn('[patch] WARNING: Not all patches applied!');
}
