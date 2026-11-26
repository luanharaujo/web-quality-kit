// Time limits that are generous in CI: a slow or busy machine must not turn a
// test red (same floor as templates/ci-timeouts.mjs). On a laptop nothing changes.
const inCI = () => ['true', '1'].includes(String(process.env.CI ?? '').toLowerCase());

/** `local` on a laptop, never under `floor` in CI. 0 stays "no limit". */
export const ciTimeout = (local, floor = 30_000) => (inCI() && local > 0 ? Math.max(local, floor) : local);

/** Floors of a Playwright config (and of each project). */
export function ciLimits(config) {
  if (!inCI()) return config;
  // The failing-network pass breaks the test's own steps on purpose: raising
  // `expect` there would only make each broken step wait longer.
  const failing = process.env.KIT_INTERACTION_MODE === 'fail';
  const raise = (part, defaults) => {
    const out = { ...part };
    if (defaults || out.timeout !== undefined) out.timeout = ciTimeout(out.timeout ?? 30_000, failing ? 60_000 : 90_000);
    if (!failing && (defaults || out.expect?.timeout !== undefined)) {
      out.expect = { ...out.expect, timeout: ciTimeout(out.expect?.timeout ?? 5_000) };
    }
    if (out.use) {
      out.use = { ...out.use };
      for (const key of ['actionTimeout', 'navigationTimeout']) {
        if (out.use[key] > 0) out.use[key] = ciTimeout(out.use[key]);
      }
    }
    return out;
  };
  const out = raise(config, true);
  if (out.projects) out.projects = out.projects.map((p) => raise(p, false));
  if (out.webServer) {
    const wait = (s) => ({ ...s, timeout: ciTimeout(s.timeout ?? 60_000, 120_000) });
    out.webServer = Array.isArray(out.webServer) ? out.webServer.map(wait) : wait(out.webServer);
  }
  return out;
}
