/**
 * Patch @livekit/agents-plugin-google to use the new Gemini audio API
 * instead of the deprecated media_chunks format.
 *
 * The plugin sends audio via `sendRealtimeInput({ media: ... })` which
 * generates deprecated `media_chunks`. Gemini now requires `audio` instead.
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

// Replace: sendRealtimeInput({ media: mediaChunk })
// With:    sendRealtimeInput({ audio: mediaChunk })
content = content.replace(
  /sendRealtimeInput\(\{\s*media:\s*mediaChunk\s*\}\)/g,
  'sendRealtimeInput({ audio: mediaChunk })',
);

writeFileSync(filePath, content, 'utf8');
console.log('[patch] Fixed Gemini audio format: media -> audio');
