// BAD example: each defect is on purpose (the test lists which rule must catch it).
const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const failures = new Map();

export async function handle(request, env) {
  const url = new URL(request.url);
  if (request.method === "POST" && url.pathname === "/login/start") return start(request, env);
  if (request.method === "POST" && url.pathname === "/signup") return signup(request, env);
  if (request.method === "POST" && url.pathname === "/signin") return json({ ok: true });
  return json({ ok: false }, 404);
}

async function start(request, env) {
  const body = await request.json().catch(() => ({}));
  const row = await env.DB.prepare("SELECT 1 FROM people WHERE phone = ?").bind(String(body.phone)).first();
  const rl = await env.RATELIMIT.limit({ key: "start" });
  if (!rl.success) return json({ ok: false }, 429);
  if (!row) return json({ exists: false }, 404);
  return json({ exists: true, ok: true, sent: true });
}

async function signup(request, env) {
  const body = await request.json().catch(() => ({}));
  const rl = await env.RATELIMIT.limit({ key: "signup" });
  if (!rl.success) return json({ ok: false }, 429);
  if (!body.token) return json({ ok: false }, 403);
  const n = (failures.get(body.phone) ?? 0) + 1;
  failures.set(body.phone, n);
  if (n > 5) return json({ ok: false }, 423);
  console.log("signup", body.phone);
  await env.DB.prepare("SELECT COUNT(*) FROM login_codes").first();
  await env.DB.prepare("SELECT COUNT(*) AS n FROM challenges WHERE kind = 'sign_in'").first();
  await env.DB.prepare("UPDATE people SET blocked = 1 WHERE phone = ?").bind(body.phone).run();
  env.obs.log.info("login_attempt", { phone: body.phone, result: "ok" });
  return json({ ok: true });
}
