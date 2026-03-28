import { createClient } from '@supabase/supabase-js';

let _supabase = null;

/**
 * Get a Supabase service-role client (bypasses RLS).
 * Same pattern as the Next.js app's src/lib/supabase.js.
 */
export function getSupabaseAdmin() {
  if (!_supabase) {
    const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
    const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
    if (!url || !key) {
      throw new Error('Missing NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY');
    }
    _supabase = createClient(url, key, {
      auth: { persistSession: false, autoRefreshToken: false },
    });
  }
  return _supabase;
}
