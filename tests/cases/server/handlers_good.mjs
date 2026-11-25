// Every list is a constant number of queries, whatever the number of rows.
export async function handle(req, env) {
  const { pathname } = new URL(req.url)
  if (req.method === 'GET' && pathname === '/health') return Response.json({ ok: true })
  if (req.method === 'GET' && pathname === '/orders') {
    // One JOIN instead of one lookup per order.
    const { results } = await env.DB.prepare(
      'SELECT o.id, o.total, c.name FROM orders o JOIN customers c ON c.id = o.customer_id ORDER BY o.id',
    ).all()
    return Response.json(results)
  }
  if (req.method === 'GET' && pathname === '/customers') {
    // Two queries: the list, then every order of the page in one IN (...).
    const { results: customers } = await env.DB.prepare('SELECT id, name FROM customers ORDER BY id').all()
    const ids = customers.map((c) => c.id)
    const marks = ids.map(() => '?').join(',') || 'NULL'
    const { results: orders } = await env.DB.prepare(`SELECT customer_id, total FROM orders WHERE customer_id IN (${marks})`).bind(...ids).all()
    return Response.json(customers.map((c) => ({ ...c, orders: orders.filter((o) => o.customer_id === c.id) })))
  }
  return new Response('not found', { status: 404 })
}
