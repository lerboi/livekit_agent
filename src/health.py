"""
Minimal health check HTTP server.
Runs alongside the LiveKit agent worker on a separate port.
Provides container health checks for Railway and external uptime monitors.
"""

import json
import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from .supabase_client import get_supabase_admin

PORT = int(os.environ.get("HEALTH_PORT", "8080"))
VERSION = "1.0.0"
_start_time = time.time()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {
                "status": "ok",
                "uptime": round(time.time() - _start_time),
                "version": VERSION,
            })
        elif self.path == "/health/db":
            self._handle_db_check()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_db_check(self):
        try:
            supabase = get_supabase_admin()
            response = supabase.table("tenants").select("id").limit(1).execute()
            self._json_response(200, {"status": "ok", "db": "connected"})
        except Exception as e:
            self._json_response(503, {"status": "error", "message": str(e)})

    def _json_response(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format, *args):
        pass  # Suppress default access logs


def start_health_server():
    """Start the health check server in a background thread.
    Non-blocking — if it fails to bind, the agent still runs.
    """
    def _run():
        try:
            server = HTTPServer(("", PORT), HealthHandler)
            print(f"[health] Health server listening on port {PORT}")
            server.serve_forever()
        except OSError as e:
            print(f"[health] Failed to start health server: {e}")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
