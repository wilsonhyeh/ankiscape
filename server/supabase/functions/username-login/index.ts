// server/supabase/functions/username-login/index.ts
// Narrowly scoped username -> managed password-auth adapter.
// Normalizes the username, resolves its private Auth identity server-side,
// calls managed password authentication, returns generic failure on missing
// user/bad password. Never exposes username-to-email lookup.
//
// Deploy (public login endpoint — must skip JWT verification):
//   supabase functions deploy username-login --no-verify-jwt --project-ref <ref>
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const NORM = /^[a-z0-9_]{3,20}$/;

function generic(status: number): Response {
  return new Response(JSON.stringify({ error: "invalid username or password" }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

serve(async (req: Request) => {
  if (req.method !== "POST") {
    return generic(400);
  }
  let body: { username?: string; password?: string };
  try {
    body = await req.json();
  } catch {
    return generic(400);
  }
  const username = (body.username ?? "").trim().toLowerCase();
  const password = body.password ?? "";
  if (!NORM.test(username) || !password || password.length > 256) {
    // Generic failure: identical shape for missing user vs bad password.
    return generic(401);
  }
  // Resolve + authenticate via service role INSIDE this function only.
  // The add-on never sees emails or the service key.
  const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
  if (!supabaseUrl || !serviceKey || !anonKey) {
    return generic(401);
  }
  const svcHeaders = { apikey: serviceKey, Authorization: `Bearer ${serviceKey}` };
  // 1. username -> user_id (private; never returned to the caller).
  let userId = "";
  try {
    const lookup = await fetch(
      `${supabaseUrl}/rest/v1/players?username_norm=eq.${username}&select=user_id`,
      { headers: svcHeaders },
    );
    if (!lookup.ok) return generic(401);
    const rows = await lookup.json();
    if (!Array.isArray(rows) || rows.length !== 1 || !rows[0].user_id) {
      return generic(401);
    }
    userId = rows[0].user_id;
  } catch {
    return generic(401);
  }
  // 2. user_id -> email via Auth Admin (service role only).
  let email = "";
  try {
    const admin = await fetch(`${supabaseUrl}/auth/v1/admin/users/${userId}`, {
      headers: svcHeaders,
    });
    if (!admin.ok) return generic(401);
    const user = await admin.json();
    email = user.email ?? "";
    if (!email) return generic(401);
  } catch {
    return generic(401);
  }
  // 3. Managed password grant with the resolved email. Unconfirmed emails,
  // wrong passwords, and banned users all land here -> generic 401.
  try {
    const token = await fetch(`${supabaseUrl}/auth/v1/token?grant_type=password`, {
      method: "POST",
      headers: { apikey: anonKey, "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!token.ok) return generic(401);
    const session = await token.json();
    if (!session.access_token || !session.refresh_token || !session.user?.id) {
      return generic(401);
    }
    return new Response(JSON.stringify(session), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return generic(401);
  }
});
