// Checks on reads (GET by fetch or XHR), slow pass only:
//   read-duplicate  the same read (URL and body) twice in one page load or
//                   one action; allowed per route in "duplicate_reads_ok"
//   loading-state   the first read of each route is held 2 s: within 400 ms
//                   the page shows aria-busy, or a visible role=status /
//                   progress bar (a skeleton marked aria-busy counts), never
//                   a blank screen; routes with no screen waiting go in
//                   "no_loading_state"
//   debounce        after each test, every search field (type=search,
//                   role=searchbox or combobox) gets a 7-letter word typed;
//                   more than 2 reads from that typing fails
import { routeKey, keyMatches } from './routes.mjs';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const WORD = 'workout';
const SEARCH = 'input[type=search],[role=searchbox],[role=combobox]';

function listed(list, key) {
  return (list || []).some((item) => keyMatches(key, typeof item === 'string' ? item : item.route));
}

export class Reads {
  constructor(owner) {
    this.o = owner; // the Interaction instance: page, cfg, fail(), pass(), claim
    this.epoch = 0;
    this.counts = new Map();
    this.flagged = new Set();
    this.typing = null;
  }

  static isRead(request) {
    return request.method() === 'GET' && ['fetch', 'xhr'].includes(request.resourceType());
  }

  async install() {
    const { page } = this.o;
    // Each tap or submit starts a new "action" (probe.js calls this).
    await page.exposeBinding('__kitAction', () => { this.epoch++; });
    page.on('framenavigated', (frame) => { if (frame === page.mainFrame()) this.epoch++; });
    page.on('request', (request) => this.onRequest(request));
  }

  onRequest(request) {
    if (!Reads.isRead(request) || this.o.opts.mode !== 'slow') return;
    const key = routeKey('GET', request.url());
    if (this.typing) {
      this.typing.count++;
      this.typing.key ||= key;
      return; // typing reads are judged by the debounce check
    }
    const id = `${this.epoch}|${request.url()}|${request.postData() || ''}`;
    const n = (this.counts.get(id) || 0) + 1;
    this.counts.set(id, n);
    if (n === 2 && !listed(this.o.cfg.duplicate_reads_ok, key) && !this.flagged.has(key)) {
      this.flagged.add(key);
      this.o.fail(key, 'read-duplicate', `the same read was sent twice for one page load or action (${new URL(request.url()).pathname}); fetch once and share the answer`);
    }
  }

  // Called from the route handler for reads. Returns after proceed().
  async check(route, request, proceed) {
    const { page, cfg } = this.o;
    const key = routeKey('GET', request.url());
    if (this.o.opts.mode !== 'slow' || listed(cfg.no_loading_state, key) || listed(cfg.exclude, key) || !this.o.claim('read', key)) {
      return proceed();
    }
    const started = Date.now();
    let how = '';
    while (!how && Date.now() - started <= cfg.busyWithinMs) {
      try { how = await page.evaluate(() => (window.__kit ? window.__kit.loading() : '')); } catch { how = ''; }
      if (!how) await sleep(40);
    }
    if (how) this.o.pass(key, 'loading-state', how);
    else this.o.fail(key, 'loading-state', `nothing showed the page was loading within ${cfg.busyWithinMs} ms (aria-busy on the region, or a visible role=status or progress bar); a blank or frozen screen looks broken`);
    await sleep(Math.max(0, cfg.holdMs - (Date.now() - started)));
    return proceed();
  }

  // After the test body: type into each search field once per run.
  async debounce() {
    const { page } = this.o;
    if (this.o.opts.mode !== 'slow' || page.isClosed()) return;
    let fields = [];
    try { fields = await page.locator(SEARCH).filter({ visible: true }).all(); } catch { return; }
    for (const field of fields) {
      let name = '';
      try {
        name = await field.evaluate((el) => el.getAttribute('aria-label') || el.name || el.id || el.placeholder || 'search');
      } catch { continue; }
      const url = new URL(page.url());
      const where = `${url.pathname}${url.search} ${name}`; // once per screen and field
      if (!this.o.claim('debounce', where)) continue;
      try {
        await field.fill('');
        await sleep(600);
        this.typing = { count: 0, key: '' };
        await field.pressSequentially(WORD, { delay: 60 });
        await sleep(1500);
      } catch {
        this.typing = null;
        continue;
      }
      const { count, key } = this.typing;
      this.typing = null;
      if (!count) continue; // filters locally: nothing to debounce
      if (count > 2) this.o.fail(key, 'debounce', `typing "${WORD}" in the search field "${name}" sent ${count} reads (limit 2): wait for a pause in the typing before searching`);
      else this.o.pass(key, 'debounce', `${count} read(s) for ${WORD.length} letters`);
    }
  }
}
