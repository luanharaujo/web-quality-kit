// web-quality-kit interaction checks for Playwright.
//
// A repository's end-to-end tests import `test` and `expect` from here
// instead of from '@playwright/test'. Nothing else changes: every request
// that writes data (POST, PUT, PATCH, DELETE) made right after a click, tap,
// Enter or submit is checked automatically, once per route per run:
//
//   slow mode (default): the request is held for 2 s and
//     1. within 400 ms of the tap the control shows a busy state
//        (disabled or aria-disabled, plus aria-busy) or a live region
//        (role=status, aria-live) gets text;
//     2. a second tap right away reaches the server at most once, or both
//        requests carry the same Idempotency-Key;
//     3. no success words ("booked", "paid", "saved"...) show up before
//        the server answers, unless the route is listed as optimistic;
//     uploads also: a progress bar with a value (not only a spinner), one
//     per file, the drop zone reacting to dragenter, and after the answer a
//     thumbnail or the file type and size.
//   fail mode: the request fails (network error) and the page must show
//     role=alert with a retry button, keep what the person typed or chose,
//     keep the other files of a batch going, and the retry must send again.
//
// KIT_INTERACTION_MODE   slow | fail | off            (default slow)
// KIT_INTERACTION_ENFORCE throw | record              (default throw: a
//   failed check fails the test; the CI action uses record and lets
//   scripts/interaction_gate.py compare with main)
// KIT_INTERACTION_REPORT_DIR  where results go (default test-results/kit-interaction)
// KIT_INTERACTION_CONFIG  path of interaction.json (default ./interaction.json)
import { test as base, expect, defineConfig as playwrightConfig } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { WRITE_METHODS, routeKey, keyMatches } from './routes.mjs';
import { settings, report } from './report.mjs';
import { Reads } from './reads.mjs';
import { ciTimeout, ciTestTimeout, ciExtra, ciLimits } from './ci.mjs';

export { expect };
export { devices } from '@playwright/test';
export { ciTimeout } from './ci.mjs';
/** Playwright's defineConfig with the CI floors on the time limits (ci.mjs). */
export const defineConfig = (...configs) => ciLimits(playwrightConfig(...configs));
export { expectIdempotent } from './contract.mjs';
export { routeKey, keyMatches, normalizePath } from './routes.mjs';
export { settings, report } from './report.mjs';

// Same rule as the kit's server helper (templates/server/observe.ts validId):
// 8 to 64 of A-Z a-z 0-9 _ -, a letter, and no run of 8+ digits outside a UUID
// (counting across - and _).
// An id the server refuses is replaced there, and the code shown would not be in the log.
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
const validRequestId = (id) =>
  /^[A-Za-z0-9_-]{8,64}$/.test(id) && /[A-Za-z]/.test(id) && !/\d{8}/.test(id.replace(UUID, '').replace(/[-_]/g, ''));
const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROBE = fs.readFileSync(path.join(HERE, 'probe.js'), 'utf8');

export const DEFAULTS = {
  holdMs: 2000,
  busyWithinMs: 400,
  userWindowMs: 1500, // a write up to 1.5 s after a tap, or after a read that tap started, is that tap's
  // English by default. An app in another language sets "success" and "retry"
  // in its interaction.json (each replaces the whole pattern, so list English too).
  success: '\\b(booked|confirmed|reserved|scheduled|paid|saved|sent|success(fully)?|done)\\b',
  retry: '(retry|try again)',
  optimistic: [],
  exclude: [],
  duplicate_reads_ok: [],
  no_loading_state: [],
  api_hosts: [], // other hosts of the app's own API (reads to them count)
};

export function loadConfig() {
  const file = process.env.KIT_INTERACTION_CONFIG || path.resolve('interaction.json');
  let user = {};
  if (fs.existsSync(file)) user = JSON.parse(fs.readFileSync(file, 'utf8'));
  return { ...DEFAULTS, ...user };
}

// One full check per route per run and mode, across workers.
function claim(mode, key) {
  const { dir } = settings();
  const claims = path.join(dir, 'claims', mode);
  fs.mkdirSync(claims, { recursive: true });
  const name = Buffer.from(key).toString('base64url');
  try {
    fs.closeSync(fs.openSync(path.join(claims, name), 'wx'));
    return true;
  } catch {
    return false;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// How many 100 ms looks the checks give the page to show something: they stop
// as soon as it does, and in CI a slow machine gets 10 s instead of 3.
const looks = (local) => ciTimeout(local * 100, 10_000) / 100;

function isUpload(request) {
  const type = (request.headers()['content-type'] || '').toLowerCase();
  if (type.startsWith('multipart/form-data')) return true;
  return /^(image|video|audio)\/|^application\/(octet-stream|pdf|zip)/.test(type);
}

function listed(list, key) {
  return (list || []).some((item) => keyMatches(key, typeof item === 'string' ? item : item.route));
}

class Interaction {
  constructor(page, testInfo) {
    this.page = page;
    this.testInfo = testInfo;
    this.cfg = loadConfig();
    this.opts = settings();
    this.failures = [];
    this.handled = new WeakSet();
    this.holding = new Map(); // key -> requests seen while the first one is held
    this.seen = []; // { key, at }
    this.pending = new Set();
    this.retry = new RegExp(this.cfg.retry, 'i');
    this.reads = new Reads(this);
  }

  claim(kind, key) {
    return claim(kind, key);
  }

  async install() {
    const { page } = this;
    const probeCfg = { success: this.cfg.success };
    await page.addInitScript({ content: `(${PROBE})(${JSON.stringify(probeCfg)})` });
    if (this.opts.mode === 'off') return;
    await this.reads.install();

    const origRoute = page.route.bind(page);
    const origUnroute = page.unroute.bind(page);
    const wrapped = new Map();
    // The test's own page.route handlers run first (Playwright: last
    // registered wins); wrapping them keeps mocked writes under the checks.
    page.route = (url, handler, options) => {
      const w = this.wrap(handler);
      wrapped.set(handler, w);
      return origRoute(url, w, options);
    };
    page.unroute = (url, handler) => origUnroute(url, handler ? wrapped.get(handler) || handler : undefined);
    await origRoute('**/*', this.wrap((route) => route.fallback()));
  }

  wrap(handler) {
    return async (route, request) => {
      if (this.handled.has(request)) return handler(route, request);
      const read = Reads.isRead(request);
      if (!read && !WRITE_METHODS.has(request.method())) return handler(route, request);
      // Writes while the kit types in a search field (autosave) are not a tap's.
      if (!read && this.reads.typing) return handler(route, request);
      this.handled.add(request);
      const proceed = () => handler(route, request);
      const run = read ? this.reads.check(route, request, proceed) : this.check(route, request, proceed);
      this.pending.add(run);
      try { await run; } finally { this.pending.delete(run); }
    };
  }

  fail(key, check, message) {
    this.failures.push(`${key}: ${message}`);
    report({ type: 'check', route: key, check, ok: false, message, test: this.testInfo.titlePath.join(' > ') });
  }

  // A problem that does not fail yet (checks being rolled out): the panel
  // lists it, the test and the gate do not fail.
  warn(key, check, message) {
    report({ type: 'warning', route: key, check, ok: false, message, test: this.testInfo.titlePath.join(' > ') });
  }

  pass(key, check, note = '') {
    report({ type: 'check', route: key, check, ok: true, message: note, test: this.testInfo.titlePath.join(' > ') });
  }

  async state() {
    try {
      return await Promise.race([
        this.page.evaluate(() => (window.__kit ? window.__kit.state() : null)),
        sleep(800).then(() => null),
      ]);
    } catch {
      return null;
    }
  }

  async check(route, request, proceed) {
    const key = routeKey(request.method(), request.url());
    const kind = request.resourceType();
    this.seen.push({ key, at: Date.now(), request });

    // Requests to the same route while the first is held: a double tap, or
    // the other files of a batch. They wait with it, so all are in flight.
    const hold = this.holding.get(key);
    if (hold) { hold.push(request); await hold.released; return proceed(); }
    if (!['fetch', 'xhr', 'document'].includes(kind)) return proceed();

    const state = await this.state();
    const userAction = (state && state.sinceAction >= 0 && state.sinceAction <= this.cfg.userWindowMs) || this.reads.chained();
    if (!userAction && kind !== 'document') return proceed(); // background write, not a tap
    report({ type: 'seen', route: key, test: this.testInfo.titlePath.join(' > ') });
    if (listed(this.cfg.exclude, key)) return proceed();
    if (!claim(this.opts.mode, key)) {
      const late = this.holding.get(key); // arrived while the first was being looked at
      if (late) { late.push(request); await late.released; }
      return proceed();
    }

    if (this.opts.mode === 'fail') return this.failChecks(route, request, key);
    let proceeded = false;
    const once = async () => { if (!proceeded) { proceeded = true; await proceed(); } };
    try {
      return await this.slowChecks(request, key, once);
    } finally {
      // never leave the request or its siblings hanging if a check breaks
      const h = this.holding.get(key);
      if (h) { this.holding.delete(key); h.release(); }
      if (!proceeded) await once().catch(() => {});
    }
  }

  async slowChecks(request, key, proceed) {
    const { page, cfg } = this;
    const upload = isUpload(request);
    const started = Date.now();
    const hold = [request];
    let release;
    hold.released = new Promise((r) => { release = r; });
    hold.release = release;
    this.holding.set(key, hold);

    // Second tap, like a person who thinks the first one did not take.
    await sleep(150);
    try {
      const point = await page.evaluate(() => window.__kit && window.__kit.retapPoint());
      if (point) {
        await page.evaluate(() => { window.__kit.suppress = true; });
        this.reads.newAction(); // reads the second tap repeats are not duplicates
        if (point.key) await page.keyboard.press('Enter');
        else await page.mouse.click(point.x, point.y);
        await page.evaluate(() => { window.__kit.suppress = false; });
      }
    } catch { /* the control is gone: nothing to tap twice */ }

    let dropzone = null;
    if (upload) {
      try { dropzone = await page.evaluate(() => window.__kit.dropzone()); } catch { dropzone = null; }
    }

    await sleep(Math.max(0, cfg.holdMs - (Date.now() - started) - 100));
    const state = await this.state();
    let progress = null;
    if (upload) {
      try { progress = await page.evaluate(() => window.__kit.progress()); } catch { progress = null; }
    }

    // 1. busy state within 400 ms
    if (!state || state.busyAfter < 0) {
      this.fail(key, 'busy', `no busy state while the server worked (${cfg.holdMs} ms): the control should become disabled/aria-disabled with aria-busy, or a role=status/aria-live region should say what is happening`);
    } else if (state.busyAfter > cfg.busyWithinMs) {
      this.fail(key, 'busy', `busy state came ${Math.round(state.busyAfter)} ms after the tap (limit ${cfg.busyWithinMs} ms)`);
    } else {
      this.pass(key, 'busy', `${Math.round(state.busyAfter)} ms, ${state.busyHow}`);
    }

    // 2. double tap: one request, or the same Idempotency-Key on all
    await sleep(100);
    const expected = upload && state && state.files > 1 ? state.files : 1; // one request per file is fine
    if (hold.length > expected) {
      const keys = hold.map((r) => r.headers()['idempotency-key'] || '');
      if (keys.every((k) => k && k === keys[0])) this.pass(key, 'double', `${hold.length} requests, same Idempotency-Key`);
      else this.fail(key, 'double', `a quick second tap sent ${hold.length - expected + 1} requests without one shared Idempotency-Key (the server would act twice)`);
    } else {
      this.pass(key, 'double', 'second tap sent nothing');
    }

    // 4. request id: every write carries x-request-id, so the error the person
    // sees can be found in the server's log
    // A native form post (no script) cannot set headers: the server makes the id.
    const noId = hold.filter((r) => r.resourceType() !== 'document' && !validRequestId(r.headers()['x-request-id'] || ''));
    if (noId.length) this.fail(key, 'request-id', `the request has no valid x-request-id (8 to 64 of A-Z a-z 0-9 _ -, with a letter and no run of 8+ digits; crypto.randomUUID() is fine): make one in the page for each write and send it, so the error message can show it`);
    else this.pass(key, 'request-id', request.resourceType() === 'document' ? 'native form post: the server makes the id' : undefined);

    // 3. no success before the server answers
    if (state && state.newSuccess > 0 && !listed(cfg.optimistic, key)) {
      this.fail(key, 'optimistic', `success shown before the server answered ("${state.successSample}"); only routes listed in interaction.json "optimistic" may do that`);
    } else {
      this.pass(key, 'optimistic');
    }

    // D. uploads: progress with a value, one per file, drop zone
    if (upload) {
      if (!progress || progress.withValue === 0) {
        this.fail(key, 'upload-progress', 'upload without a progress bar that has a value (role=progressbar with aria-valuenow, or <progress value>): a spinner hides how long it takes');
      } else if (state && state.files > 1 && progress.withValue < state.files) {
        this.fail(key, 'upload-progress', `${state.files} files but ${progress.withValue} progress bar(s): each file needs its own`);
      } else {
        this.pass(key, 'upload-progress');
      }
      if (dropzone && dropzone.found && !dropzone.changed) {
        this.fail(key, 'upload-dropzone', 'the drop zone did not change when a file was dragged over it (dragenter)');
      } else if (dropzone && dropzone.found) {
        this.pass(key, 'upload-dropzone');
      }
    }

    this.holding.delete(key);
    release();
    await proceed();

    if (upload) {
      this.room();
      await Promise.race([request.response().catch(() => null), sleep(ciTimeout(5000, 10_000))]);
      let confirmed = null;
      for (let i = 0; i < looks(30); i++) {
        try { confirmed = await page.evaluate(() => window.__kit.confirmation()); } catch { confirmed = null; }
        if (confirmed && (confirmed.newImage || (confirmed.size && confirmed.type))) break;
        await sleep(100);
      }
      if (!confirmed || !(confirmed.newImage || (confirmed.size && confirmed.type))) {
        this.fail(key, 'upload-confirm', 'after the upload the page shows neither a thumbnail nor the file type and size');
      } else {
        this.pass(key, 'upload-confirm');
      }
    }
  }

  /** In CI the waits for this action's checks must not eat the test's time. */
  room() {
    if (this.testInfo.timeout) this.testInfo.setTimeout(this.testInfo.timeout + ciExtra());
  }

  async failChecks(route, request, key) {
    this.room();
    const { page } = this;
    const upload = isUpload(request);
    const before = await this.state();
    const abortedAt = Date.now();
    await route.abort('failed');

    const alert = page.locator('[role=alert],[role=alertdialog]').filter({ hasText: /\S/ });
    const retry = page.getByRole('button', { name: this.retry });
    let alertOk = false;
    let retryOk = false;
    for (let i = 0; i < looks(30) && !(alertOk && retryOk); i++) {
      await sleep(100);
      alertOk = await alert.first().isVisible().catch(() => false);
      retryOk = await retry.first().isVisible().catch(() => false);
    }
    if (!alertOk) this.fail(key, 'error-alert', 'when the network failed no error showed in role=alert');
    else this.pass(key, 'error-alert');
    // the error shows the request's id, for support to find it in the log
    const rid = request.headers()['x-request-id'] || '';
    if (request.resourceType() === 'document') {
      this.pass(key, 'error-code', 'native form post: no page id to show when the network fails');
    } else if (!validRequestId(rid)) {
      this.fail(key, 'error-code', 'the request had no x-request-id, so the error cannot show its code');
    } else {
      const texts = alertOk ? await alert.allInnerTexts().catch(() => []) : [];
      // innerText applies text-transform: an uppercase alert still shows the id.
      if (texts.some((t) => t.toLowerCase().includes(rid.toLowerCase()))) this.pass(key, 'error-code');
      else this.fail(key, 'error-code', `the error in role=alert does not show the request's id (${rid}): add "error code: <id>" so support can find it in the log`);
    }
    if (!retryOk) this.fail(key, 'error-retry', 'when the network failed there was no "try again" button');
    else this.pass(key, 'error-retry');

    let lost = [];
    try { lost = await page.evaluate(() => window.__kit.lostFields()); } catch { lost = ['(page changed)']; }
    if (lost.length) this.fail(key, 'error-keeps-input', `after the error the person's input was lost: ${lost.join(', ')}`);
    else this.pass(key, 'error-keeps-input');

    if (upload && before && before.files > 1) {
      const mine = this.seen.find((s) => s.request === request);
      const since = mine ? mine.at : abortedAt;
      const others = () => this.seen.filter((s) => s.key === key && s.request !== request && s.at >= since).length;
      for (let i = 0; i < looks(50) && others() < before.files - 1; i++) await sleep(100);
      if (others() < before.files - 1) this.fail(key, 'upload-independent', `one failed file stopped the others (${others()} of ${before.files - 1} went on)`);
      else this.pass(key, 'upload-independent');
    }

    if (retryOk) {
      const sentBefore = this.seen.filter((s) => s.key === key).length;
      const lastRetry = upload ? retry.first() : retry.last();
      await lastRetry.click({ timeout: ciTimeout(2000, 10_000) }).catch(() => {});
      let again = false;
      for (let i = 0; i < looks(30) && !again; i++) {
        await sleep(100);
        again = this.seen.filter((s) => s.key === key).length > sentBefore;
      }
      if (!again) this.fail(key, 'error-retry-sends', '"try again" did not send the request again' + (upload ? ' (the file must stay loaded: no new file choice)' : ''));
      else this.pass(key, 'error-retry-sends');
    }
  }

  async finish() {
    await Promise.allSettled([...this.pending]);
    // Typing into the search of a failed test would change the screen before
    // its failure screenshot: only after a test that went as expected.
    if (this.opts.mode !== 'off' && this.testInfo.status === this.testInfo.expectedStatus) await this.reads.debounce();
    await Promise.allSettled([...this.pending]);
    if (!this.failures.length) return;
    for (const f of this.failures) this.testInfo.annotations.push({ type: 'kit-interaction', description: f });
    if (this.opts.enforce === 'throw') {
      throw new Error('Interaction checks failed (web-quality-kit):\n- ' + this.failures.join('\n- '));
    }
  }
}

export const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    // In CI the checks' own waits are longer: the test gets room for them even
    // when its config does not come from the kit's defineConfig.
    if (testInfo.timeout) testInfo.setTimeout(ciTestTimeout(testInfo.timeout));
    const checks = new Interaction(page, testInfo);
    await checks.install();
    await use(page);
    await checks.finish();
  },
});
