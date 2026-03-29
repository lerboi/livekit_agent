import os
from supabase import create_client, Client

_supabase: Client | None = None


def get_supabase_admin() -> Client:
    """Get a Supabase service-role client (bypasses RLS).
    Same pattern as the Next.js app's src/lib/supabase.js.
    """
    global _supabase
    if _supabase is None:
        url = os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError("Missing NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        _supabase = create_client(url, key)
    return _supabase
