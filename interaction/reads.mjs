// Checks on reads (GET by fetch or XHR), slow pass only:
//   read-duplicate  the same read (URL and body) again within 3 s in one page
//                   load or one action, when the first one did not fail;
//                   allowed per route in "duplicate_reads_ok"
//   loading-state   for the first read of each route, within 400 ms
//                   something new says "loading" (aria-busy turned on, a
//                   role=status/aria-live text changed, a visible progress
//                   bar appeared or was shown); a status that was already
//                   there does not count; reads are never held or slowed;
//                   routes nobody waits for go in "no_loading_state"
//   debounce        after each passing test, every editable search field
//                   (type=search, role=searchbox or combobox) gets a 7-letter
//                   word typed; more than 2 reads carrying the typed text fail
//
// Only reads to the app itself count (same host, a subdomain of it, the
// loopback API in tests, or a host listed in "api_hosts"): analytics, maps and
// other third parties are ignored.
// Reads that start inside a tap's window keep that window open when they end,
// so a write that waits for them is still that tap's (see chained()).
// KIT_INTERACTION_READS=warn (default) records these as warnings: the panel
// shows them, nothing fails. "enforce" makes them count like the other checks.
import { routeKey, keyMatches } from './routes.mjs';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const WORD = 'workout';
const SEARCH = 'input[type=search],[role=searchbox],[role=combobox]';
const DUPLICATE_WINDOW_MS = 3000;
const CHAIN_MAX_MS = 5000; // reads extend a tap's window, never past this
const MAX_KEY_GAP_MS = 150; // slower typing than this: the count is not judged
const DEBOUNCE_NEEDS_MS = 8000; // time left in the test before typing in a field
const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);

// Page evaluate with a time limit: a synchronous XHR blocks the page.
async function evaluate(page, fn, arg, ms = 300) {
  try {
    return await Promise.race([page.evaluate(fn, arg), sleep(ms).then(() => null)]);
  } catch {
    return null;
  }
}

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
    this.actionAt = 0; // last tap or submit (Node clock)
    this.activeUntil = 0; // end of the last read that tap started
    this.inflight = new Set(); // reads that tap started, not answered yet
    this.out = new Set(); // keys of reads whose answer has not come back yet
    this.finished = false;
    this.started = Date.now();
    this.enforce = (process.env.KIT_INTERACTION_READS || 'warn') === 'enforce';
  }

  static isRead(request) {
    return request.method() === 'GET' && ['fetch', 'xhr'].includes(request.resourceType());
  }

  problem(key, check, message) {
    if (this.enforce) this.o.fail(key, check, message);
    else this.o.warn(key, check, message);
  }

  ours(request) {
    try {
      const page = new URL(this.o.page.url()).hostname;
      const host = new URL(request.url()).hostname;
      if (host === page) return true;
      if (LOOPBACK.has(host) && LOOPBACK.has(page)) return true;
      if ((this.o.cfg.api_hosts || []).includes(host)) return true;
      // a subdomain of the page (api.app.example.com.br for app.example.com.br),
      // or of the site behind www (api.example.com for www.example.com)
      const base = page.startsWith('www.') ? page.slice(4) : page;
      return base.includes('.') && (host === base || host.endsWith(`.${base}`));
    } catch {
      return false;
    }
  }

  // True while a write still belongs to the last tap: within the window of
  // the tap itself, or of the end of a read that tap started.
  chained() {
    const { userWindowMs } = this.o.cfg;
    if (!this.actionAt || Date.now() - this.actionAt > CHAIN_MAX_MS) return false;
    // A write right as a read ends can reach us before that read's end does.
    if (this.inflight.size) return true;
    return Date.now() - Math.max(this.actionAt, this.activeUntil) <= userWindowMs; // a select chosen with no click counts too
  }


  skipped(key) {
    return listed(this.o.cfg.exclude, key);
  }

  async install() {
    const { page } = this.o;
    // Each tap, submit, typed or chosen value starts a new "action" (probe.js).
    // Only a tap or submit opens a write window that its reads can extend; a
    // typed or chosen value is a new action for the read checks only.
    await page.exposeBinding('__kitAction', (_, kind) => {
      this.newAction();
      if (kind === 'tap') {
        this.actionAt = Date.now();
      } else if ((kind === 'change' || kind === 'control') && Date.now() - this.actionAt < 500) {
        return; // part of the tap: a field's change on blur, a checkbox's input and change
      } else {
        this.actionAt = 0;
      }
      this.activeUntil = 0;
      this.inflight.clear();
    });
    page.on('framenavigated', (frame) => { if (frame === page.mainFrame()) this.epoch++; });
    page.on('request', (request) => this.onRequest(request));
    page.on('requestfinished', (request) => this.settled(request, false));
    page.on('requestfailed', (request) => this.settled(request, true));
  }

  newAction() {
    this.epoch++;
  }

  id(request) {
    return `${this.epoch}|${request.url()}|${request.postData() || ''}`;
  }

  settled(request, failed) {
    if (!Reads.isRead(request)) return;
    if (this.inflight.delete(request)) this.activeUntil = Date.now(); // the tap is still working: its write may follow
    const entry = this.last.get(request._kitId);
    if (!entry) return;
    if (failed) entry.failed = true;
    else request.response().then((r) => { if (r && r.status() >= 400) entry.failed = true; }).catch(() => {});
  }

  onRequest(request) {
    if (!Reads.isRead(request) || !this.ours(request)) return;
    const now = Date.now();
    // Started by the last tap: at most 100 ms before the tap reached us (its
    // notice and the request race), and within the window of the tap, of the
    // end of a read it started, or while one is still out.
    const from = this.actionAt;
    if (from && now >= from - 100 && now - from <= CHAIN_MAX_MS
        && (this.inflight.size || now - Math.max(from, this.activeUntil) <= this.o.cfg.userWindowMs)) {
      this.inflight.add(request);
    }
    if (this.o.opts.mode !== 'slow') return;
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
    this.last.set(id, { at: now, failed: false });
    // A retry after a failed read, or a refresh long after, is not a duplicate.
    if (!before || before.failed || now - before.at > DUPLICATE_WINDOW_MS) return;
    if (listed(this.o.cfg.duplicate_reads_ok, key) || this.flagged.has(key)) return;
    this.flagged.add(key);
    this.problem(key, 'read-duplicate', `the same read was sent again ${now - before.at} ms later for one page load or action (${new URL(request.url()).pathname}); fetch once and share the answer`);
  }

  // Called from the route handler for reads. The read goes on at once (never
  // held); the page is then watched for a loading signal around it.
  async check(route, request, proceed) {
    const key = routeKey('GET', request.url());
    // The page clock when the read left, taken before the test's own route
    // handler runs (it may wait before answering).
    const left = Date.now();
    const judged = this.judged(request, key);
    if (judged) this.out.add(key);
    try { await proceed(); } finally { this.out.delete(key); }
    if (!judged) return;
    // Once the read is answered, the time-limited watch below is waited for
    // at the end of the test (a mock that never answers is not).
    const watch = this.watch(request, key, left);
    this.o.pending.add(watch);
    try { await watch; } finally { this.o.pending.delete(watch); }
  }

  // Whether the loading check looks at this read at all.
  judged(request, key) {
    return this.o.opts.mode === 'slow' && !this.typing && this.ours(request)
      && !listed(this.o.cfg.no_loading_state, key) && !this.skipped(key);
  }

  async watch(request, key, left) {
    const { page, cfg } = this.o;
    if (this.finished) return; // answered after the test ended: noted in end()
    if (!this.o.claim('read', key)) return;
    // Look at least once: a slow route handler may already be past the window,
    // and the signals are kept with their times.
    let how = '';
    for (;;) {
      how = (await evaluate(page, ([t, w]) => (window.__kit ? window.__kit.loadingSince(t, w) : ''), [left, cfg.busyWithinMs])) || '';
      // The test ended and closed the page: nothing to judge, and another
      // test may check this route.
      if (!how && page.isClosed()) { this.o.unclaim('read', key); this.o.note(key, 'loading-state', 'not judged: the page closed before the check ended'); return; }
      // Keep looking a little longer when the page is slow to answer us.
      if (how || Date.now() - left > cfg.busyWithinMs + 1000) break;
      await sleep(40);
    }
    if (how) this.o.pass(key, 'loading-state', how);
    else this.problem(key, 'loading-state', `nothing new showed the page was loading within ${cfg.busyWithinMs} ms (turn on aria-busy on the region, or put text in a role=status, or show a progress bar); a blank or frozen screen looks broken`);
  }

  // When the test ends: reads still unanswered (a slow or hung mock, a page
  // closed first) cannot be judged; say so, and judge nothing after this.
  end() {
    this.finished = true;
    if (this.o.opts.mode !== 'slow') return;
    for (const key of this.out) this.o.note(key, 'loading-state', 'not judged: the read was not answered before the test ended');
  }

  // After the test body: type into each editable search field once per run.
  async debounce() {
    const { page } = this.o;
    if (this.o.opts.mode !== 'slow' || page.isClosed()) return;
    // Typing takes about 3 s a field and counts in the test's time: never let
    // it be what makes a test time out.
    const timeLeft = () => (this.o.testInfo.timeout ? this.o.testInfo.timeout - (Date.now() - this.started) : Infinity);
    let fields = [];
    try { fields = await page.locator(SEARCH).filter({ visible: true }).all(); } catch { return; }
    for (const field of fields) {
      if (timeLeft() < DEBOUNCE_NEEDS_MS) return;
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
        await evaluate(page, () => { if (window.__kit) window.__kit.keyTimes = []; }, null, 2000);
        await field.pressSequentially(WORD, { delay: 60, timeout: 3000 });
        // The longest pause between keys as the page saw them: on a loaded
        // machine it can grow past a page's debounce, and then the count
        // proves nothing.
        const times = await evaluate(page, () => (window.__kit ? window.__kit.keyTimes : []), null, 2000);
        this.typing.gap = !times ? Infinity : times.reduce((max, t, i, all) => (i ? Math.max(max, t - all[i - 1]) : 0), 0);
        await sleep(1500);
      } catch {
        this.typing = null;
        continue;
      }
      const { count, key, gap } = this.typing;
      this.typing = null;
      if (!count) continue; // filters locally: nothing to debounce
      if (gap > MAX_KEY_GAP_MS) { // typing too slow to judge: another test may
        this.o.unclaim('debounce', where);
        this.o.note(key, 'debounce', `not judged: the keys reached the page up to ${Math.round(gap)} ms apart (limit ${MAX_KEY_GAP_MS})`);
        continue;
      }
      if (this.skipped(key)) continue;
      if (count > 2) this.problem(key, 'debounce', `typing "${WORD}" in the search field "${name}" sent ${count} reads (limit 2): wait for a pause in the typing before searching`);
      else this.o.pass(key, 'debounce', `${count} read(s) for ${WORD.length} letters`);
    }
  }
}
