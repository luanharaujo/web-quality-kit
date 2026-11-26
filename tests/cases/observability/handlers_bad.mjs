// The usual way logs go wrong: loose console calls, text instead of JSON,
// personal data in the line, no request id.
export async function handle(req, env) {
  const { pathname } = new URL(req.url)
  console.log('request', req.method, pathname)
  if (req.method === 'GET' && pathname === '/health') return Response.json({ ok: true })
  if (req.method === 'POST' && pathname === '/orders') {
    const body = await req.json()
    await env.DB.prepare('INSERT INTO orders (total, email) VALUES (?, ?)').bind(body.total, body.email).run()
    console.log(JSON.stringify({ event: 'order_created', email: body.email }))
    return Response.json({ ok: true }, { status: 201 })
  }
  if (req.method === 'GET' && pathname === '/orders') {
    try {
      const { results } = await env.DB.prepare('SELECT id, total FROM orders').all()
      return Response.json(results)
    } catch (err) {
      console.error(err)
      return new Response('error', { status: 500 })
    }
  }
  return new Response('not found', { status: 404 })
}
