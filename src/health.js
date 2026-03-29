/**
 * Minimal health check HTTP server.
 * Runs alongside the LiveKit agent worker on a separate port.
 * Provides container health checks for Railway and external uptime monitors.
 */

import http from 'node:http';
import { getSupabaseAdmin } from './supabase.js';

const PORT = parseInt(process.env.HEALTH_PORT || '8080', 10);
const VERSION = process.env.npm_package_version || '1.0.0';
const DB_TIMEOUT_MS = 3000;

/**
 * Start the health check server. Non-blocking — if it fails to bind,
 * the agent still runs (health checks are not critical to call handling).
 */
export function startHealthServer() {
  const server = http.createServer(async (req, res) => {
    if (req.method !== 'GET') {
      res.writeHead(405);
      res.end();
      return;
    }

    if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'ok',
        uptime: Math.round(process.uptime()),
        version: VERSION,
      }));
      return;
    }

    if (req.url === '/health/db') {
      try {
        const supabase = getSupabaseAdmin();
        const result = await Promise.race([
          supabase.from('tenants').select('id').limit(1),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error('DB health check timeout')), DB_TIMEOUT_MS),
          ),
        ]);

        if (result.error) {
          res.writeHead(503, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ status: 'error', message: result.error.message }));
          return;
        }

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', db: 'connected' }));
      } catch (err) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'error', message: err.message }));
      }
      return;
    }

    res.writeHead(404);
    res.end();
  });

  server.listen(PORT, () => {
    console.log(`[health] Health server listening on port ${PORT}`);
  });

  server.on('error', (err) => {
    console.error(`[health] Failed to start health server: ${err.message}`);
  });
}
