// BAD: accepts any override of the siteverify address, in production too.
export async function check(token, env) {
  const url = env.TURNSTILE_VERIFY_URL || "https://challenges.cloudflare.com/turnstile/v0/siteverify";
  const r = await fetch(url, { method: "POST", body: new URLSearchParams({ secret: env.TURNSTILE_SECRET, response: token }) });
  const { success, hostname, action } = await r.json();
  return success && hostname && action;
}
