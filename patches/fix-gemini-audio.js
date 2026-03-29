/**
 * Patch @livekit/agents-plugin-google to:
 * 1. Use new Gemini audio API (audio instead of deprecated media_chunks)
 * 2. Log the connect config for debugging
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

// Fix 1: sendRealtimeInput({ media: mediaChunk }) → sendRealtimeInput({ audio: mediaChunk })
content = content.replace(
  /sendRealtimeInput\(\{\s*media:\s*mediaChunk\s*\}\)/g,
  'sendRealtimeInput({ audio: mediaChunk })',
);

// Fix 2: Log the config sent to Gemini on connect
content = content.replace(
  'const config = this.buildConnectConfig();',
  'const config = this.buildConnectConfig(); console.log("[gemini-debug] Connect config:", JSON.stringify(config, null, 2));',
);

writeFileSync(filePath, content, 'utf8');
console.log('[patch] Applied Gemini fixes: audio format + debug logging');
