// GOOD example: cheap filter, then Turnstile, then D1; the same answer for a
// known and an unknown number; one login_attempt event without personal data.
const json = (body, status = 200, extra = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...extra } });

// TURNSTILE_VERIFY_URL is for tests: only a local http address is accepted, so
// the override can never point production's check somewhere else.
function siteverifyUrl(override) {
  if (override) {
    const u = new URL(override);
    if (u.protocol === "http:" && (u.hostname === "127.0.0.1" || u.hostname === "localhost")) return u.href;
  }
  return "https://challenges.cloudflare.com/turnstile/v0/siteverify";
}

async function verifyTurnstile(token, env, action, hostname) {
  if (!token) return false;
  const r = await fetch(siteverifyUrl(env.TURNSTILE_VERIFY_URL), { method: "POST", body: new URLSearchParams({ secret: env.TURNSTILE_SECRET, response: token }) });
  const j = await r.json();
  return j.success === true && j.hostname === hostname && j.action === action;
}

export async function handle(request, env) {
  const url = new URL(request.url);
  if (request.method === "POST" && url.pathname === "/login/start") return start(request, env);
  if (request.method === "POST" && url.pathname === "/signup") return signup(request, env);
  return json({ ok: false }, 404);
}

async function start(request, env) {
  const ip = request.headers.get("cf-connecting-ip") ?? "";
  const rl = await env.RATELIMIT.limit({ key: "start:" + ip });
  if (!rl.success) {
    env.obs.log.warn("login_attempt", { auth_method: "whatsapp_code", result: "limited", risk: "high", reason: "ratelimit" });
    return json({ ok: false }, 429, { "retry-after": "60" });
  }
  const body = await request.json().catch(() => ({}));
  if (!(await verifyTurnstile(body.token, env, "/login/start", "localhost"))) {
    env.obs.log.info("login_attempt", { auth_method: "whatsapp_code", result: "turnstile_failed", risk: "medium", reason: "challenge" });
    return json({ ok: false, reason_text: "challenge" }, 403);
  }
  await env.DB.prepare("SELECT 1 FROM people WHERE phone = ?").bind(String(body.phone)).first();
  env.obs.log.info("login_attempt", { auth_method: "whatsapp_code", result: "ok", risk: "low", reason: "sent" });
  return json({ ok: true, reason_text: "challenge" });
}

async function signup(request, env) {
  const ip = request.headers.get("cf-connecting-ip") ?? "";
  const rl = await env.RATELIMIT.limit({ key: "signup:" + ip });
  if (!rl.success) {
    env.obs.log.warn("login_attempt", { auth_method: "whatsapp_code", result: "limited", risk: "high", reason: "ratelimit" });
    return json({ ok: false }, 429, { "retry-after": "60" });
  }
  const body = await request.json().catch(() => ({}));
  if (!(await verifyTurnstile(body.token, env, "/signup", "localhost"))) {
    env.obs.log.info("login_attempt", { auth_method: "whatsapp_code", result: "turnstile_failed", risk: "medium", reason: "challenge" });
    return json({ ok: false, reason_text: "challenge" }, 403);
  }
  await env.DB.prepare("INSERT OR IGNORE INTO people (phone) VALUES (?)").bind(String(body.phone)).run();
  env.obs.log.info("login_attempt", { auth_method: "whatsapp_code", result: "ok", risk: "low", reason: "sent" });
  return json({ ok: true, reason_text: "challenge" });
}
