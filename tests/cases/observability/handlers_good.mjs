// Every log goes through obs.log; personal data only in the database.
export async function handle(req, env, obs) {
  const { pathname } = new URL(req.url)
  if (req.method === 'GET' && pathname === '/health') return Response.json({ ok: true })
  if (req.method === 'GET' && pathname === '/orders') {
    const { results } = await env.DB.prepare('SELECT id, total FROM orders ORDER BY id').all()
    return Response.json(results)
  }
  const m = pathname.match(/^\/orders\/(\d+)$/)
  if (req.method === 'POST' && pathname === '/orders') {
    const body = await req.json()
    await env.DB.prepare('INSERT INTO orders (total, email) VALUES (?, ?)').bind(body.total, body.email).run()
    obs.log.info('order_created', { total: body.total, email: body.email })
    return Response.json({ ok: true }, { status: 201 })
  }
  if (req.method === 'GET' && m) {
    const row = await env.DB.prepare('SELECT id, total FROM orders WHERE id = ?').bind(Number(m[1])).first()
    if (!row) obs.log.warn('order_missing', { order: Number(m[1]) })
    return row ? Response.json(row) : new Response('not found', { status: 404 })
  }
  if (pathname === '/boom') throw new Error('broken for ana@example.com')
  // an outside login: the check must not follow it
  if (pathname === '/login') return Response.redirect('http://127.0.0.2:9/oauth', 302)
  if (pathname === '/private') return new Response('no', { status: 401 })
  return new Response('not found', { status: 404 })
}
