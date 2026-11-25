// Every list is a constant number of queries, whatever the number of rows,
// and returns one page at a time: ?limit=N (default 20, cap 50) and ?cursor.
const page = (url) => {
  const limit = Math.min(Number(url.searchParams.get('limit')) || 20, 50)
  return { limit, after: Number(url.searchParams.get('cursor')) || 0 }
}
const paged = (rows, limit) => ({
  items: rows.slice(0, limit),
  next: rows.length > limit ? String(rows[limit - 1].id) : null,
})

export async function handle(req, env) {
  const url = new URL(req.url)
  const { pathname } = url
  if (req.method === 'GET' && pathname === '/health') return Response.json({ ok: true })
  if (req.method === 'GET' && pathname === '/orders') {
    // One JOIN instead of one lookup per order; one row more tells if there is a next page.
    const { limit, after } = page(url)
    const { results } = await env.DB.prepare(
      'SELECT o.id, o.total, c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.id > ? ORDER BY o.id LIMIT ?',
    ).bind(after, limit + 1).all()
    return Response.json(paged(results, limit))
  }
  if (req.method === 'GET' && pathname === '/customers') {
    // Two queries: the page, then every order of the page in one IN (...).
    const { limit, after } = page(url)
    const { results: customers } = await env.DB.prepare('SELECT id, name FROM customers WHERE id > ? ORDER BY id LIMIT ?')
      .bind(after, limit + 1).all()
    const ids = customers.map((c) => c.id)
    const marks = ids.map(() => '?').join(',') || 'NULL'
    const { results: orders } = await env.DB.prepare(`SELECT customer_id, total FROM orders WHERE customer_id IN (${marks})`).bind(...ids).all()
    const withOrders = customers.map((c) => ({ ...c, orders: orders.filter((o) => o.customer_id === c.id) }))
    return Response.json(paged(withOrders, limit))
  }
  return new Response('not found', { status: 404 })
}
