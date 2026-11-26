// Pagination shapes the check must catch or let through with a reason:
//  - /orders pages with ?limit but never caps it: ?limit=100000 returns every row;
//  - /totals is a grouped summary (one row per status): it does not page by nature;
//  - /strict refuses a limit above its cap with 400: that keeps the cap too;
//  - /exact pages and caps right but refuses any unknown query parameter.
export async function handle(req, env) {
  const url = new URL(req.url)
  const { pathname } = url
  if (req.method === 'GET' && pathname === '/health') return Response.json({ ok: true })
  if (req.method === 'GET' && pathname === '/orders') {
    const limit = Number(url.searchParams.get('limit')) || 20
    const after = Number(url.searchParams.get('cursor')) || 0
    const { results } = await env.DB.prepare(
      'SELECT o.id, o.total, c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.id > ? ORDER BY o.id LIMIT ?',
    ).bind(after, limit + 1).all()
    return Response.json({ items: results.slice(0, limit), next: results.length > limit ? String(results[limit - 1].id) : null })
  }
  if (req.method === 'GET' && pathname === '/totals') {
    const { results } = await env.DB.prepare('SELECT customer_id % 3 AS bucket, SUM(total) AS total FROM orders GROUP BY bucket').all()
    return Response.json({ items: results })
  }
  if (req.method === 'GET' && pathname === '/exact') {
    if ([...url.searchParams.keys()].some((k) => k !== 'limit' && k !== 'cursor')) return Response.json({ error: 'unknown parameter' }, { status: 400 })
    const limit = Math.min(Number(url.searchParams.get('limit')) || 20, 50)
    const after = Number(url.searchParams.get('cursor')) || 0
    const { results } = await env.DB.prepare('SELECT id, total FROM orders WHERE id > ? ORDER BY id LIMIT ?').bind(after, limit + 1).all()
    return Response.json({ items: results.slice(0, limit), next: results.length > limit ? String(results[limit - 1].id) : null })
  }
  if (req.method === 'GET' && pathname === '/strict') {
    const asked = Number(url.searchParams.get('limit')) || 20
    if (asked > 50) return Response.json({ error: 'limit above 50' }, { status: 400 })
    const after = Number(url.searchParams.get('cursor')) || 0
    const { results } = await env.DB.prepare('SELECT id, total FROM orders WHERE id > ? ORDER BY id LIMIT ?').bind(after, asked + 1).all()
    return Response.json({ items: results.slice(0, asked), next: results.length > asked ? String(results[asked - 1].id) : null })
  }
  return new Response('not found', { status: 404 })
}
