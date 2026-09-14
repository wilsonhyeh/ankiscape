// server/supabase/functions/account-status/index.ts
// Public account-status probe for registration: given one validated email and
// an optional proposed username, returns only
//   { email_status: "new" | "unconfirmed" | "confirmed",
//     username_available: boolean }
// It creates nothing, sends nothing, and returns no ids, stored emails,
// existing usernames, tokens, metadata or profiles.
//
// Rate limited with atomic server-side buckets keyed by an HMAC digest of the
// normalized email (server-only secret); raw emails never reach the database.
// 10 requests/minute per email hash and 120/minute globally.
//
// Deploy (public endpoint; the function does its own validation and limits):
//   supabase functions deploy account-status --no-verify-jwt --project-ref <ref>
// Requires secret: ACCOUNT_STATUS_HMAC_SECRET
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const MAX_BODY_BYTES = 2048;
const EMAIL_MAX = 320;
const USERNAME_RE = /^[a-z0-9_]{3,20}$/;
const UPSTREAM_TIMEOUT_MS = 5000;

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
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
  const hmacSecret = Deno.env.get("ACCOUNT_STATUS_HMAC_SECRET") ?? "";
  if (!supabaseUrl || !serviceKey || !hmacSecret) {
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
  let body: { email?: unknown; username?: unknown };
  try {
    body = JSON.parse(raw || "{}");
  } catch {
    return json(400, { error: "invalid_request" });
  }
  const email = typeof body.email === "string" ? body.email.trim() : "";
  const username = typeof body.username === "string"
    ? body.username.trim().toLowerCase()
    : "";
  if (
    !email || email.length > EMAIL_MAX || !email.includes("@") ||
    email.includes(" ")
  ) {
    return json(400, { error: "invalid_email" });
  }
  if (username && !USERNAME_RE.test(username)) {
    return json(400, { error: "invalid_username" });
  }

  // Rate limit before touching any data. Keys are digests, never raw values.
  let key: CryptoKey;
  try {
    key = await hmacKey(hmacSecret);
  } catch {
    return json(503, { error: "service_unavailable" });
  }
  const emailDigest = await digestHex(key, `email:${email.toLowerCase()}`);
  const checks: Array<{ key: string; limit: number }> = [
    { key: `e:${emailDigest}`, limit: 10 },
    { key: "global", limit: 120 },
  ];
  for (const check of checks) {
    let limited = false;
    let resetS = 60;
    try {
      const resp = await fetchWithTimeout(
        `${supabaseUrl}/rest/v1/rpc/account_status_consume`,
        {
          method: "POST",
          headers: serviceHeaders(serviceKey),
          body: JSON.stringify({
            p_key: check.key,
            p_limit: check.limit,
            p_window_s: 60,
          }),
        },
        UPSTREAM_TIMEOUT_MS,
      );
      if (!resp.ok) {
        return json(503, { error: "service_unavailable" });
      }
      const out = await resp.json();
      if (!out || out.allowed !== true) {
        limited = true;
        resetS = Number(out?.reset_s ?? 60);
      }
    } catch {
      return json(503, { error: "service_unavailable" });
    }
    if (limited) {
      const retryAfter = Math.max(1, Math.min(Number(resetS) || 60, 3600));
      return json(
        429,
        { error: "rate_limited" },
        { "Retry-After": String(retryAfter) },
      );
    }
  }

  // The SQL helper is service-role-only and returns no identifiers.
  try {
    const rpc = await fetchWithTimeout(
      `${supabaseUrl}/rest/v1/rpc/account_lifecycle_status`,
      {
        method: "POST",
        headers: serviceHeaders(serviceKey),
        body: JSON.stringify(
          username
            ? { p_email: email, p_username: username }
            : { p_email: email },
        ),
      },
      UPSTREAM_TIMEOUT_MS,
    );
    if (rpc.status === 400) {
      // Bad email/username shape from the helper's own validation.
      return json(400, { error: "invalid_request" });
    }
    if (!rpc.ok) {
      return json(503, { error: "service_unavailable" });
    }
    const data = await rpc.json();
    const emailStatus = data?.email_status;
    if (!["new", "unconfirmed", "confirmed"].includes(emailStatus)) {
      return json(503, { error: "service_unavailable" });
    }
    return json(200, {
      email_status: emailStatus,
      username_available: data?.username_available === true,
    });
  } catch {
    return json(503, { error: "service_unavailable" });
  }
});
