// Test time limits that are generous in CI (copy of web-quality-kit
// templates/ci-timeouts.mjs; keep ci-timeouts.d.mts beside it for TypeScript).
// A CI job may run on a slow or busy machine: with CI=true no test limit is
// under 30 s, so a red test means a real error. On a laptop nothing changes.

export const CI_FLOOR_MS = 30_000;

export const inCI = () => ['true', '1'].includes(String(process.env.CI ?? '').toLowerCase());

/** A time limit in ms: `local` on a laptop, never under `floor` in CI. 0 stays "no limit". */
export const ciTimeout = (local, floor = CI_FLOOR_MS) => (inCI() && local > 0 ? Math.max(local, floor) : local);

/** vitest: `test: { ...vitestTimeouts() }` */
export const vitestTimeouts = () => ({
  testTimeout: ciTimeout(5_000),
  hookTimeout: ciTimeout(10_000),
  teardownTimeout: ciTimeout(10_000),
});

/** Playwright: `defineConfig({ ...playwrightTimeouts(), ... })` */
export const playwrightTimeouts = () => ({
  timeout: ciTimeout(30_000, 90_000), // several waits fit in one test
  expect: { timeout: ciTimeout(5_000) },
});
