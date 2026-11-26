// Checks on reads (GET by fetch or XHR), slow pass only:
//   read-duplicate  the same read (URL and body) again within 3 s in one page
//                   load or one action, when the first one did not fail;
//                   allowed per route in "duplicate_reads_ok"
//   loading-state   the first read of each route is held 1 s: within 400 ms
//                   something new says "loading" (aria-busy turned on, a
//                   role=status/aria-live text changed, a progress bar
//                   appeared); a status that was already there does not
//                   count; routes nobody waits for go in "no_loading_state"
//   debounce        after each test, every editable search field
//                   (type=search, role=searchbox or combobox) gets a 7-letter
//                   word typed; more than 2 reads carrying the typed text fail
//
// KIT_INTERACTION_READS=warn (default) records these as warnings: the panel
// shows them, nothing fails. "enforce" makes them count like the other checks.
import { routeKey, keyMatches } from './routes.mjs';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const WORD = 'workout';
const SEARCH = 'input[type=search],[role=searchbox],[role=combobox]';
const DUPLICATE_WINDOW_MS = 3000;
const READ_HOLD_MS = 1000;

function listed(list, key) {
  return (list || []).some((item) => keyMatches(key, typeof item === 'string' ? item : item.route));
}

// Values the read carries: query values and the body (or its string values).
function carried(request) {
  const out = [];
  try {
    for (const v of new URL(request.url()).searchParams.values()) out.push(v);
  } catch { /* not a URL */ }
  const body = request.postData();
  if (body) {
    try {
      JSON.stringify(JSON.parse(body), (_, v) => { if (typeof v === 'string') out.push(v); return v; });
    } catch { out.push(body); }
  }
  return out.map((v) => v.trim().toLowerCase());
}

export class Reads {
  constructor(owner) {
    this.o = owner; // the Interaction instance: page, cfg, opts, fail(), pass(), warn(), claim()
    this.epoch = 0;
    this.last = new Map(); // id -> { at, failed }
    this.flagged = new Set();
    this.typing = null;
    this.enforce = (process.env.KIT_INTERACTION_READS || 'warn') === 'enforce';
  }

  static isRead(request) {
    return request.method() === 'GET' && ['fetch', 'xhr'].includes(request.resourceType());
  }

  problem(key, check, message) {
    if (this.enforce) this.o.fail(key, check, message);
    else this.o.warn(key, check, message);
  }

  skipped(key) {
    return listed(this.o.cfg.exclude, key);
  }

  async install() {
    const { page } = this.o;
    // Each tap, submit, typed or chosen value starts a new "action" (probe.js).
    await page.exposeBinding('__kitAction', () => { this.epoch++; });
    page.on('framenavigated', (frame) => { if (frame === page.mainFrame()) this.epoch++; });
    page.on('request', (request) => this.onRequest(request));
    page.on('requestfinished', (request) => this.settled(request, false));
    page.on('requestfailed', (request) => this.settled(request, true));
  }

  id(request) {
    return `${this.epoch}|${request.url()}|${request.postData() || ''}`;
  }

  settled(request, failed) {
    if (!Reads.isRead(request)) return;
    const entry = this.last.get(request._kitId);
    if (!entry) return;
    if (failed) entry.failed = true;
    else request.response().then((r) => { if (r && r.status() >= 400) entry.failed = true; }).catch(() => {});
  }

  onRequest(request) {
    if (!Reads.isRead(request) || this.o.opts.mode !== 'slow') return;
    const key = routeKey('GET', request.url());
    if (this.typing) {
      if (carried(request).some((v) => v && WORD.startsWith(v))) {
        this.typing.count++;
        this.typing.key ||= key;
      }
      return; // reads while the kit types are judged by the debounce check
    }
    if (this.skipped(key)) return;
    const id = this.id(request);
    request._kitId = id;
    const before = this.last.get(id);
    const now = Date.now();
    this.last.set(id, { at: now, failed: false });
    // A retry after a failed read, or a refresh long after, is not a duplicate.
    if (!before || before.failed || now - before.at > DUPLICATE_WINDOW_MS) return;
    if (listed(this.o.cfg.duplicate_reads_ok, key) || this.flagged.has(key)) return;
    this.flagged.add(key);
    this.problem(key, 'read-duplicate', `the same read was sent again ${now - before.at} ms later for one page load or action (${new URL(request.url()).pathname}); fetch once and share the answer`);
  }

  // Called from the route handler for reads. Returns after proceed().
  async check(route, request, proceed) {
    const { page, cfg } = this.o;
    const key = routeKey('GET', request.url());
    if (this.o.opts.mode !== 'slow' || this.typing || listed(cfg.no_loading_state, key) || this.skipped(key) || !this.o.claim('read', key)) {
      return proceed();
    }
    const started = Date.now();
    let since = null;
    try { since = await page.evaluate(() => performance.now()); } catch { since = null; }
    let how = '';
    while (!how && Date.now() - started <= cfg.busyWithinMs) {
      try {
        how = since === null ? '' : await page.evaluate((t) => (window.__kit ? window.__kit.loadingSince(t) : ''), since);
      } catch { how = ''; }
      if (!how) await sleep(40);
    }
    if (how) this.o.pass(key, 'loading-state', how);
    else this.problem(key, 'loading-state', `nothing new showed the page was loading within ${cfg.busyWithinMs} ms (turn on aria-busy on the region, or put text in a role=status, or show a progress bar); a blank or frozen screen looks broken`);
    await sleep(Math.max(0, READ_HOLD_MS - (Date.now() - started)));
    return proceed();
  }

  // After the test body: type into each editable search field once per run.
  async debounce() {
    const { page } = this.o;
    if (this.o.opts.mode !== 'slow' || page.isClosed()) return;
    let fields = [];
    try { fields = await page.locator(SEARCH).filter({ visible: true }).all(); } catch { return; }
    for (const field of fields) {
      let name = '';
      try {
        if (!(await field.isEditable({ timeout: 500 }))) continue; // readonly or disabled
        name = await field.evaluate((el) => el.getAttribute('aria-label') || el.name || el.id || el.placeholder || 'search', null, { timeout: 500 });
      } catch { continue; }
      const url = new URL(page.url());
      const where = `${url.pathname}${url.search} ${name}`; // once per screen and field
      if (!this.o.claim('debounce', where)) continue;
      this.typing = { count: 0, key: '' };
      try {
        await field.fill('', { timeout: 1000 }); // reads from clearing are not counted
        await sleep(600);
        this.typing.count = 0;
        await field.pressSequentially(WORD, { delay: 60, timeout: 3000 });
        await sleep(1500);
      } catch {
        this.typing = null;
        continue;
      }
      const { count, key } = this.typing;
      this.typing = null;
      if (!count) continue; // filters locally: nothing to debounce
      if (this.skipped(key)) continue;
      if (count > 2) this.problem(key, 'debounce', `typing "${WORD}" in the search field "${name}" sent ${count} reads (limit 2): wait for a pause in the typing before searching`);
      else this.o.pass(key, 'debounce', `${count} read(s) for ${WORD.length} letters`);
    }
  }
}
