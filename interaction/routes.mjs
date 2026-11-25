// Route keys shared by the fixture, the contract helper and the gate
// (scripts/interaction_gate.py has the same rules in Python; tests prove both).
//
// A key is "METHOD /path" with every id-like segment turned into ":id", so
// "POST /api/classes/42/bookings" and the server's "POST /classes/:id/bookings"
// meet. Matching is by suffix: the app may call the API under a prefix.

export const WRITE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

const ID_SEGMENT = /^(\d+|[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{16,}|[A-Za-z0-9_-]{24,})$/i;
const PARAM_SEGMENT = /^(:.*|\{.*\}|\[.*\]|#.*|\*.*|<.*>)$/;

export function normalizePath(path) {
  const clean = String(path).split('?', 1)[0]; // "#id" is a route param, not a fragment
  const parts = clean.split('/').filter(Boolean).map((seg) => {
    let s = seg;
    try { s = decodeURIComponent(seg); } catch { /* keep the raw segment */ }
    return ID_SEGMENT.test(s) || PARAM_SEGMENT.test(s) ? ':id' : s;
  });
  return '/' + parts.join('/');
}

export function routeKey(method, url) {
  let path = url;
  try { path = new URL(url, 'http://x').pathname; } catch { /* already a path */ }
  return `${String(method).toUpperCase()} ${normalizePath(path)}`;
}

// Does an observed key (from a real request) match a declared one (from the
// server code)? Same method, and the declared segments are the tail of the
// observed ones; ":id" on either side matches anything.
export function keyMatches(observed, declared) {
  const [om, op] = observed.split(' ');
  const [dm, dp] = declared.split(' ');
  if (om !== dm) return false;
  const o = op.split('/').filter(Boolean);
  const d = dp.split('/').filter(Boolean);
  if (d.length > o.length) return false;
  const tail = o.slice(o.length - d.length);
  return d.every((seg, i) => seg === ':id' || tail[i] === ':id' || seg === tail[i]);
}
