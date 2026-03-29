/**
 * Patch @livekit/agents-plugin-google for gemini-3.1-flash-live-preview:
 * 1. Use new Gemini audio API (audio instead of deprecated media_chunks)
 * 2. Clean config: remove unsupported fields
 * 3. Skip initial sendClientContent (3.1 rejects it)
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

// Fix 2: Clean config before returning from buildConnectConfig()
const before2 = content;
content = content.replace(
  /(\s+)return config;\s*\n(\s+)\}\s*\n(\s+)startNewGeneration/,
  `$1delete config.inputAudioTranscription;
$1delete config.outputAudioTranscription;
$1delete config.sessionResumption;
$1delete config.thinkingConfig;
$1Object.keys(config).forEach(k => { if (config[k] === undefined || config[k] === null) delete config[k]; });
$1console.log("[gemini-patch] Final config keys:", Object.keys(config).join(", "));
$1return config;
$2}
$3startNewGeneration`,
);
if (content !== before2) patchCount++;

// Fix 3: Skip initial sendClientContent after session open
// gemini-3.1-flash-live-preview rejects sendClientContent for initial turns
const before3 = content;
content = content.replace(
  /if \(turns\.length > 0\) \{\s*\n\s*await session\.sendClientContent\(\{\s*\n\s*turns,\s*\n\s*turnComplete: false\s*\n\s*\}\);\s*\n\s*\}/,
  `if (turns.length > 0) {
              console.log("[gemini-patch] Skipping initial sendClientContent (" + turns.length + " turns) — not supported by 3.1");
            }`,
);
if (content !== before3) patchCount++;

writeFileSync(filePath, content, 'utf8');
console.log(`[patch] Applied ${patchCount}/3 Gemini 3.1 fixes`);
if (patchCount < 3) {
  console.warn('[patch] WARNING: Only applied', patchCount, 'of 3 patches');
}
