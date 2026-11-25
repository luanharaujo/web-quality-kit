// Server contract: the same request sent twice with the same Idempotency-Key
// must create one record and get the same answer both times.
//
//   import { test, expectIdempotent } from 'web-quality-kit-interaction';
//   test('booking is idempotent', async ({ request }) => {
//     await expectIdempotent(request, {
//       method: 'POST', url: '/api/bookings', data: { slot: 42 },
//       count: async () => (await (await request.get('/api/test/count?table=bookings')).json()).n,
//     });
//   });
//
// `count` returns how many records the route creates or changes exist now
// (a test-only endpoint, a database query, anything). `ignore` lists answer
// fields that may differ between the two answers (e.g. a request id).
// `change` is how much count() must move after both requests: one request's
// worth (default 1 for POST, 0 for PUT/PATCH, -1 for DELETE).
// The result goes to the interaction report, so the gate knows which write
// routes have a contract test.
import { randomUUID } from 'node:crypto';
import { routeKey } from './routes.mjs';
import { settings, report } from './report.mjs';

async function body(res) {
  const text = await res.text();
  try { return JSON.parse(text); } catch { return text; }
}

function strip(value, ignore) {
  if (Array.isArray(value)) return value.map((v) => strip(v, ignore));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value)
      .filter(([k]) => !ignore.includes(k))
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => [k, strip(v, ignore)]));
  }
  return value;
}

export async function expectIdempotent(request, options) {
  const { method = 'POST', url, data, form, multipart, headers = {}, count, ignore = [], route, change } = options || {};
  if (typeof count !== 'function') throw new Error('expectIdempotent needs count(): how many records exist now');
  if (!url) throw new Error('expectIdempotent needs url');
  const opts = settings();
  if (opts.mode === 'fail') return; // the failure pass only exercises the page
  const key = route || routeKey(method, url);

  const problems = [];
  const before = Number(await count());
  const idem = randomUUID();
  const send = () => request.fetch(url, { method, data, form, multipart, headers: { ...headers, 'Idempotency-Key': idem } });
  const r1 = await send();
  const b1 = strip(await body(r1), ignore);
  const r2 = await send();
  const b2 = strip(await body(r2), ignore);
  const after = Number(await count());

  if (!r1.ok()) problems.push(`the first request failed (${r1.status()}), so nothing was proven`);
  if (r1.status() !== r2.status()) problems.push(`answers differ: ${r1.status()} then ${r2.status()}`);
  else if (JSON.stringify(b1) !== JSON.stringify(b2)) problems.push('same status but a different body the second time (list changing fields in `ignore`)');
  const want = change ?? { POST: 1, DELETE: -1 }[method.toUpperCase()] ?? 0;
  if (r1.ok() && after - before !== want) problems.push(`count() should move by ${want} (one action), it moved by ${after - before}`);

  const ok = problems.length === 0;
  report({ type: 'contract', route: key, ok, message: problems.join('; ') });
  if (!ok && opts.enforce === 'throw') {
    throw new Error(`Idempotency contract failed for ${key}: ${problems.join('; ')}`);
  }
}
