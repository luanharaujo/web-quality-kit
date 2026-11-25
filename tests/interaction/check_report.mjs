// Reads the reports of both passes and checks that each bad page was caught
// by its rule and nothing else, and that the good pages passed everything.
// Usage: node check_report.mjs <slow-report-dir> <fail-report-dir>
import fs from 'node:fs';
import path from 'node:path';

const read = (dir) => fs.existsSync(dir)
  ? fs.readdirSync(dir).filter((f) => f.endsWith('.jsonl'))
    .flatMap((f) => fs.readFileSync(path.join(dir, f), 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)))
  : [];

const page = (route) => route.split('/')[2];
function failures(entries, type) {
  const out = {};
  for (const e of entries) {
    if (e.type !== type) continue;
    const p = page(e.route);
    out[p] ||= new Set();
    if (!e.ok) out[p].add(e.check || 'contract');
  }
  return out;
}

const EXPECTED = {
  slow: {
    good: [], 'bad-busy': ['busy'], 'bad-double': ['double'], 'bad-optimistic': ['optimistic'], 'bad-error': [],
    'upload-good': [], 'upload-bad': ['upload-confirm', 'upload-dropzone', 'upload-progress'],
    'list-good': [], 'list-dup': ['read-duplicate'], 'list-noload': ['loading-state'], 'list-search': ['debounce'],
  },
  fail: {
    good: [], 'bad-busy': [], 'bad-double': [], 'bad-optimistic': [], 'bad-error': ['error-keeps-input', 'error-retry'],
    'upload-good': [], 'upload-bad': ['error-alert', 'error-retry', 'upload-independent'],
  },
  contract: { good: [], 'bad-double': ['contract'] },
};

const [slowDir, failDir] = process.argv.slice(2);
const slow = read(slowDir);
const fail = read(failDir);
const got = { slow: failures(slow, 'check'), fail: failures(fail, 'check'), contract: failures(slow, 'contract') };

// The good pages must have been through every check (a check that never ran
// proves nothing).
const RAN = {
  slow: { good: ['busy', 'double', 'optimistic'], 'upload-good': ['upload-confirm', 'upload-dropzone', 'upload-progress'], 'list-good': ['debounce', 'loading-state'] },
  fail: { good: ['error-alert', 'error-keeps-input', 'error-retry', 'error-retry-sends'], 'upload-good': ['upload-independent'] },
};
const ran = (entries, p) => new Set(entries.filter((e) => e.type === 'check' && page(e.route) === p).map((e) => e.check));

let bad = 0;
for (const [pass, pages] of Object.entries(RAN)) {
  for (const [p, want] of Object.entries(pages)) {
    const have = ran(pass === 'slow' ? slow : fail, p);
    const missing = want.filter((c) => !have.has(c));
    console.log(`${missing.length ? 'FAIL' : 'ok  '} ${pass} ${p}: ran ${want.join(', ')}${missing.length ? `; never ran ${missing.join(', ')}` : ''}`);
    if (missing.length) bad++;
  }
}
for (const [pass, pages] of Object.entries(EXPECTED)) {
  for (const [p, want] of Object.entries(pages)) {
    if (!got[pass][p]) { console.log(`FAIL ${pass} ${p}: never checked`); bad++; continue; }
    const have = [...got[pass][p]].sort();
    const ok = JSON.stringify(have) === JSON.stringify([...want].sort());
    console.log(`${ok ? 'ok  ' : 'FAIL'} ${pass} ${p}: caught [${have.join(', ')}]${ok ? '' : `, expected [${want.join(', ')}]`}`);
    if (!ok) bad++;
  }
}
if (bad) {
  for (const e of [...slow, ...fail]) if (e.ok === false) console.log(`  ${e.route} ${e.check || e.type}: ${e.message}`);
  process.exit(1);
}
