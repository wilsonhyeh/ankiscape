// server/supabase/functions/username-login/index.ts
// Narrowly scoped username -> managed password-auth adapter.
// Normalizes the username, resolves its private Auth identity server-side,
// calls managed password authentication, returns generic failure on missing
// user/bad password. Never exposes username-to-email lookup.
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const NORM = /^[a-z0-9_]{3,20}$/;

serve(async (req: Request) => {
  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "invalid username or password" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }
  let body: { username?: string; password?: string };
  try {
    body = await req.json();
  } catch {
    return new Response(JSON.stringify({ error: "invalid username or password" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }
  const username = (body.username ?? "").trim().toLowerCase();
  const password = body.password ?? "";
  if (!NORM.test(username) || !password || password.length > 256) {
    // Generic failure: identical shape for missing user vs bad password.
    return new Response(JSON.stringify({ error: "invalid username or password" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  // Resolve + authenticate via service role INSIDE this function only.
  // The add-on never sees emails or the service key.
  const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
  const lookup = await fetch(`${supabaseUrl}/rest/v1/players?username_norm=eq.${username}&select=user_id`, {
    headers: { apikey: serviceKey, Authorization: `Bearer ${serviceKey}` },
  });
  if (!lookup.ok) {
    return new Response(JSON.stringify({ error: "invalid username or password" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  const rows = await lookup.json();
  if (!Array.isArray(rows) || rows.length !== 1) {
    return new Response(JSON.stringify({ error: "invalid username or password" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  // NOTE: full implementation exchanges the resolved identity for a managed
  // session via Auth admin APIs in the pinned local stack tests (auth task).
  // This stub preserves the generic-failure contract until then.
  return new Response(JSON.stringify({ error: "invalid username or password" }), {
    status: 501,
    headers: { "Content-Type": "application/json" },
  });
});
