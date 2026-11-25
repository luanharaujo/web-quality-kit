// The N+1 shapes the check must catch.
export async function handle(req, env) {
  const { pathname } = new URL(req.url)
  if (req.method === 'GET' && pathname === '/health') return Response.json({ ok: true })
  if (req.method === 'GET' && pathname === '/orders') {
    // 1 query for the list + 1 per order for its customer.
    const { results } = await env.DB.prepare('SELECT id, customer_id, total FROM orders ORDER BY id').all()
    for (const o of results) {
      o.name = await env.DB.prepare('SELECT name FROM customers WHERE id = ?').bind(o.customer_id).first('name')
    }
    return Response.json(results)
  }
  if (req.method === 'GET' && pathname === '/customers') {
    // A batch built in a loop is still one query per row.
    const { results: customers } = await env.DB.prepare('SELECT id, name FROM customers ORDER BY id').all()
    const per = await env.DB.batch(customers.map((c) => env.DB.prepare('SELECT total FROM orders WHERE customer_id = ?').bind(c.id)))
    return Response.json(customers.map((c, i) => ({ ...c, orders: per[i].results })))
  }
  if (req.method === 'GET' && pathname === '/products') {
    // A list with no 3-vs-30 test: the check must flag the route itself.
    return Response.json((await env.DB.prepare('SELECT id FROM orders').all()).results)
  }
  return new Response('not found', { status: 404 })
}
