// server/supabase/functions/account-delete/index.ts
// Guarded self-service account deletion. The caller deletes ONLY itself:
// the target id always comes from the validated bearer token, never from the
// body. Ordered checks:
//   1. POST only; bounded JSON body {confirm:"DELETE", username, password}.
//   2. Bearer token → GET /auth/v1/user (anon key) proves a live caller.
//   3. Player row lookup; the supplied username must equal
//      players.username_display EXACTLY (no client normalization).
//   4. Demos and fixture-registered accounts are immutable (403).
//   5. Atomic server-side rate limit (HMAC key; no raw ids in SQL).
//   6. Password reauthentication; the returned user id must match.
//   7. Service-role hard delete; {deleted:true} only after a 2xx response.
//
// Never logs request bodies or credentials. Confirmations and responses carry
// closed error codes only. Ambiguous upstream deletes report delete_unknown
// instead of a confident refusal.
//
// Deploy (JWT verification stays ON so the platform also requires a token):
//   supabase functions deploy account-delete --project-ref <ref>
// Requires secrets: ACCOUNT_STATUS_HMAC_SECRET and service/anon keys.
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const MAX_BODY_BYTES = 4096;
const MAX_PASSWORD_BYTES = 256;
const MAX_USERNAME_LEN = 64;
const UPSTREAM_TIMEOUT_MS = 6000;
const DELETE_LIMIT = 5;
const DELETE_WINDOW_S = 3600;
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function json(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function serviceHeaders(serviceKey: string): Record<string, string> {
  return {
    apikey: serviceKey,
    Authorization: `Bearer ${serviceKey}`,
    "Content-Type": "application/json",
  };
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

async function hmacKey(secret: string): Promise<CryptoKey> {
  return await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

async function digestHex(key: CryptoKey, value: string): Promise<string> {
  const mac = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(mac))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

serve(async (req: Request) => {
  if (req.method !== "POST") {
    return json(405, { error: "method_not_allowed" });
  }
  const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
  const hmacSecret = Deno.env.get("ACCOUNT_STATUS_HMAC_SECRET") ?? "";
  if (!supabaseUrl || !serviceKey || !anonKey || !hmacSecret) {
    return json(503, { error: "service_unavailable" });
  }

  let raw = "";
  try {
    const buf = await req.arrayBuffer();
    if (buf.byteLength > MAX_BODY_BYTES) {
      return json(400, { error: "invalid_request" });
    }
    raw = new TextDecoder().decode(buf);
  } catch {
    return json(400, { error: "invalid_request" });
  }
  let body: { confirm?: unknown; username?: unknown; password?: unknown };
  try {
    body = JSON.parse(raw || "{}");
  } catch {
    return json(400, { error: "invalid_request" });
  }
  if (
    typeof body.confirm !== "string" || typeof body.username !== "string" ||
    typeof body.password !== "string"
  ) {
    return json(400, { error: "invalid_request" });
  }
  if (body.confirm !== "DELETE") {
    return json(400, { error: "invalid_confirmation" });
  }
  const username = body.username;
  const password = body.password;
  if (
    !username || username.length > MAX_USERNAME_LEN || !password ||
    new TextEncoder().encode(password).length > MAX_PASSWORD_BYTES
  ) {
    return json(400, { error: "invalid_request" });
  }

  // 1. Establish the caller from the bearer token alone.
  const authHeader = req.headers.get("Authorization") ?? "";
  const token = authHeader.startsWith("Bearer ")
    ? authHeader.slice(7).trim()
    : "";
  if (!token) {
    return json(401, { error: "invalid_session" });
  }
  let callerId = "";
  let callerEmail = "";
  try {
    const who = await fetchWithTimeout(`${supabaseUrl}/auth/v1/user`, {
      headers: { apikey: anonKey, Authorization: `Bearer ${token}` },
    }, UPSTREAM_TIMEOUT_MS);
    if (who.status === 429) {
      const retry = who.headers.get("Retry-After") ?? "5";
      return json(429, { error: "rate_limited" }, { "Retry-After": retry });
    }
    if (!who.ok) {
      return json(401, { error: "invalid_session" });
    }
    const user = await who.json();
    callerId = String(user?.id ?? "");
    callerEmail = String(user?.email ?? "");
  } catch {
    // Transport failure is not proof of a bad session.
    return json(503, { error: "service_unavailable" });
  }
  if (!UUID_RE.test(callerId) || !callerEmail) {
    return json(503, { error: "service_unavailable" });
  }

  // 2. Authoritative player row. The username compare is exact.
  const svcHeaders = serviceHeaders(serviceKey);
  let usernameDisplay = "";
  let isDemo = false;
  try {
    const lookup = await fetchWithTimeout(
      `${supabaseUrl}/rest/v1/players?user_id=eq.${callerId}` +
        `&select=username_display,is_demo&limit=1`,
      { headers: svcHeaders },
      UPSTREAM_TIMEOUT_MS,
    );
    if (lookup.status === 429) {
      const retry = lookup.headers.get("Retry-After") ?? "5";
      return json(429, { error: "rate_limited" }, { "Retry-After": retry });
    }
    if (!lookup.ok) {
      return json(503, { error: "service_unavailable" });
    }
    const rows = await lookup.json();
    if (!Array.isArray(rows) || rows.length !== 1) {
      return json(404, { error: "account_unavailable" });
    }
    usernameDisplay = String(rows[0]?.username_display ?? "");
    isDemo = rows[0]?.is_demo === true;
  } catch {
    return json(503, { error: "service_unavailable" });
  }
  if (!usernameDisplay) {
    return json(404, { error: "account_unavailable" });
  }
  if (username !== usernameDisplay) {
    return json(400, { error: "invalid_confirmation" });
  }

  // 3. Demos and fixture-registered identities are immutable. A failed
  //    protection query is a service failure, never permission to continue.
  if (isDemo) {
    return json(403, { error: "demo_immutable" });
  }
  try {
    const fixture = await fetchWithTimeout(
      `${supabaseUrl}/rest/v1/fixture_registry?user_id=eq.${callerId}` +
        `&select=username_norm&limit=1`,
      { headers: svcHeaders },
      UPSTREAM_TIMEOUT_MS,
    );
    if (!fixture.ok) {
      return json(503, { error: "service_unavailable" });
    }
    const rows = await fixture.json();
    if (!Array.isArray(rows)) {
      return json(503, { error: "service_unavailable" });
    }
    if (rows.length > 0) {
      return json(403, { error: "demo_immutable" });
    }
  } catch {
    return json(503, { error: "service_unavailable" });
  }

  // 4. One atomic rate-limit hit per caller (HMAC digest, no raw ids in SQL).
  try {
    const key = await hmacKey(hmacSecret);
    const digest = await digestHex(key, `user:${callerId}`);
    const resp = await fetchWithTimeout(
      `${supabaseUrl}/rest/v1/rpc/account_status_consume`,
      {
        method: "POST",
        headers: svcHeaders,
        body: JSON.stringify({
          p_key: `d:${digest}`,
          p_limit: DELETE_LIMIT,
          p_window_s: DELETE_WINDOW_S,
        }),
      },
      UPSTREAM_TIMEOUT_MS,
    );
    if (!resp.ok) {
      return json(503, { error: "service_unavailable" });
    }
    const out = await resp.json();
    if (!out || out.allowed !== true) {
      const retryAfter = Math.max(
        1,
        Math.min(Number(out?.reset_s ?? 60) || 60, 3600),
      );
      return json(
        429,
        { error: "rate_limited" },
        { "Retry-After": String(retryAfter) },
      );
    }
  } catch {
    return json(503, { error: "service_unavailable" });
  }

  // 5. Password reauthentication. Returned credentials are discarded.
  try {
    const reauth = await fetchWithTimeout(
      `${supabaseUrl}/auth/v1/token?grant_type=password`,
      {
        method: "POST",
        headers: { apikey: anonKey, "Content-Type": "application/json" },
        body: JSON.stringify({ email: callerEmail, password }),
      },
      UPSTREAM_TIMEOUT_MS,
    );
    if (reauth.status === 429) {
      const retry = reauth.headers.get("Retry-After") ?? "5";
      return json(429, { error: "rate_limited" }, { "Retry-After": retry });
    }
    if (!reauth.ok) {
      if (
        reauth.status === 400 || reauth.status === 401 ||
        reauth.status === 422
      ) {
        return json(401, { error: "invalid_credentials" });
      }
      return json(502, { error: "service_error" });
    }
    const session = await reauth.json();
    if (String(session?.user?.id ?? "") !== callerId) {
      // A different identity came back for this caller's password: fail
      // closed rather than delete either account.
      return json(401, { error: "invalid_session" });
    }
  } catch {
    return json(503, { error: "service_unavailable" });
  }

  // 6. Hard delete through the Auth admin API.
  try {
    const del = await fetchWithTimeout(
      `${supabaseUrl}/auth/v1/admin/users/${callerId}`,
      { method: "DELETE", headers: svcHeaders },
      UPSTREAM_TIMEOUT_MS,
    );
    if (del.status === 429) {
      const retry = del.headers.get("Retry-After") ?? "5";
      return json(429, { error: "rate_limited" }, { "Retry-After": retry });
    }
    if (del.status === 404) {
      // The row disappeared between reauth and delete; absence is likely but
      // not proven through this response.
      return json(504, { error: "delete_unknown" });
    }
    if (!del.ok) {
      return json(502, { error: "service_error" });
    }
  } catch {
    // Timeout or lost reply: the delete may have completed. Never report a
    // definite refusal.
    return json(504, { error: "delete_unknown" });
  }
  return json(200, { deleted: true });
});
