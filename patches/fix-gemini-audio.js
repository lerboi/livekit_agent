/**
 * Patch @livekit/agents-plugin-google for gemini-3.1-flash-live-preview
 *
 * Fixes:
 * 1. Audio: media → audio in sendRealtimeInput
 * 2. Config: strip unsupported fields
 * 3. Content: sendClientContent → sendRealtimeInput (3.1 rejects sendClientContent mid-session)
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
$1return config;
$2}
$3startNewGeneration`,
);
if (content !== before2) patchCount++;

// Fix 3: Replace sendClientContent with sendRealtimeInput for mid-session content
// The "content" case in the sendTask sends turns via sendClientContent which 3.1 rejects
const before3 = content;
content = content.replace(
  /case "content":\s*\n\s*const \{ turns, turnComplete \} = msg\.value;\s*\n\s*if \(LK_GOOGLE_DEBUG\) \{\s*\n\s*this\.#logger\.debug\(`\(client\) -> \$\{JSON\.stringify\(this\.loggableClientEvent\(msg\)\)\}`\);\s*\n\s*\}\s*\n\s*await session\.sendClientContent\(\{\s*\n\s*turns,\s*\n\s*turnComplete: turnComplete \?\? true\s*\n\s*\}\);/,
  `case "content":
            const { turns, turnComplete } = msg.value;
            if (LK_GOOGLE_DEBUG) {
              this.#logger.debug(\`(client) -> \${JSON.stringify(this.loggableClientEvent(msg))}\`);
            }
            // Patched: use sendRealtimeInput for text (3.1 rejects sendClientContent mid-session)
            for (const turn of turns) {
              if (turn.parts) {
                for (const part of turn.parts) {
                  if (part.text) {
                    await session.sendRealtimeInput({ text: part.text });
                  }
                }
              }
            }`,
);
if (content !== before3) patchCount++;

// Fix 4: Skip initial sendClientContent after session open (initial history seeding)
const before4 = content;
content = content.replace(
  /if \(turns\.length > 0\) \{\s*\n\s*await session\.sendClientContent\(\{\s*\n\s*turns,\s*\n\s*turnComplete: false\s*\n\s*\}\);\s*\n\s*\}/,
  `if (turns.length > 0) {
              console.log("[gemini-patch] Skipping initial sendClientContent (" + turns.length + " turns)");
            }`,
);
if (content !== before4) patchCount++;

writeFileSync(filePath, content, 'utf8');
console.log(`[patch] Applied ${patchCount}/4 Gemini 3.1 fixes`);
if (patchCount < 4) {
  console.warn('[patch] WARNING: Only applied', patchCount, 'of 4 patches');
}
