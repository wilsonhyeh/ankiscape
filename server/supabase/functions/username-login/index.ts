// server/supabase/functions/username-login/index.ts
// Narrowly scoped username -> managed password-auth adapter.
// Normalizes the username, resolves its private Auth identity server-side,
// calls managed password authentication, returns generic failure on missing
// user/bad password. Never exposes username-to-email lookup.
//
// Upstream behavior is preserved honestly: rate limits stay 429 with
// Retry-After, timeouts/connection failures are 504/503, and only an actual
// credential rejection is 401. A valid-but-unconfirmed identity returns
// 401 {"error":"verification_required"} (no email echoed) so the client can
// ask the user for the email on the verification page.
//
// Deploy (public login endpoint — must skip JWT verification):
//   supabase functions deploy username-login --no-verify-jwt --project-ref <ref>
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const NORM = /^[a-z0-9_]{3,20}$/;
const UPSTREAM_TIMEOUT_MS = 6000;

function json(status: number, body: unknown,
              headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function generic(status: number): Response {
  return json(status, { error: "invalid username or password" });
}

async function fetchWithTimeout(
  input: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
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
    // Misconfiguration is a service problem, not wrong credentials.
    return json(503, { error: "service_unavailable" });
  }
  const svcHeaders = { apikey: serviceKey, Authorization: `Bearer ${serviceKey}` };
  // 1. username -> user_id (private; never returned to the caller).
  let userId = "";
  try {
    const lookup = await fetchWithTimeout(
      `${supabaseUrl}/rest/v1/players?username_norm=eq.${username}&select=user_id`,
      { headers: svcHeaders },
      UPSTREAM_TIMEOUT_MS,
    );
    if (lookup.status === 429) {
      const retry = lookup.headers.get("Retry-After") ?? "5";
      return json(429, { error: "rate_limited" }, { "Retry-After": retry });
    }
    if (!lookup.ok) {
      return json(502, { error: "service_unavailable" });
    }
    const rows = await lookup.json();
    if (!Array.isArray(rows) || rows.length !== 1 || !rows[0].user_id) {
      return generic(401);
    }
    userId = rows[0].user_id;
  } catch {
    return json(504, { error: "upstream_timeout" });
  }
  // 2. user_id -> email + confirmation state via Auth Admin (service role).
  let email = "";
  let confirmed = true;
  try {
    const admin = await fetchWithTimeout(
      `${supabaseUrl}/auth/v1/admin/users/${userId}`,
      { headers: svcHeaders },
      UPSTREAM_TIMEOUT_MS,
    );
    if (admin.status === 429) {
      const retry = admin.headers.get("Retry-After") ?? "5";
      return json(429, { error: "rate_limited" }, { "Retry-After": retry });
    }
    if (!admin.ok) {
      return json(502, { error: "service_unavailable" });
    }
    const user = await admin.json();
    email = user.email ?? "";
    confirmed = Boolean(user.email_confirmed_at || user.confirmed_at);
    if (!email) return generic(401);
  } catch {
    return json(504, { error: "upstream_timeout" });
  }
  // 3. Managed password grant with the resolved email. Only an actual
  // credential rejection is a 401; an unconfirmed identity is distinguished
  // without echoing the email.
  try {
    const token = await fetchWithTimeout(
      `${supabaseUrl}/auth/v1/token?grant_type=password`,
      {
        method: "POST",
        headers: { apikey: anonKey, "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      },
      UPSTREAM_TIMEOUT_MS,
    );
    if (token.status === 429) {
      const retry = token.headers.get("Retry-After") ?? "5";
      return json(429, { error: "rate_limited" }, { "Retry-After": retry });
    }
    if (!token.ok) {
      let code = "";
      try {
        const errBody = await token.json();
        code = String(errBody?.error_code ?? errBody?.code ??
                     errBody?.error ?? "").toLowerCase();
      } catch {
        code = "";
      }
      if (code.includes("email_not_confirmed") || code.includes("unverified")) {
        return json(401, { error: "verification_required" });
      }
      return generic(401);
    }
    const session = await token.json();
    if (!session.access_token || !session.refresh_token || !session.user?.id) {
      return json(502, { error: "service_unavailable" });
    }
    if (!confirmed) {
      // Session issued despite an unconfirmed address (provider-config
      // dependent): report the verification need instead of normal success.
      return json(401, { error: "verification_required" });
    }
    return json(200, session);
  } catch {
    return json(504, { error: "upstream_timeout" });
  }
});
