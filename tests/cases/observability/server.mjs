// node server.mjs <good|bad|nowrap|fresh-id> <port>: an example Worker-like
// server for the observability check. good = the kit's wrapper; bad = loose
// console logs, wrapper present; nowrap = no wrapper (no x-request-id);
// fresh-id = a homemade wrapper that ignores the caller's id and logs text
// (with an e-mail in it).
import http from 'node:http'
import { FakeD1 } from '../server/fake_d1.mjs'
import { withQueryCount } from '../../../templates/server/d1_queries.ts'
import { withObservability } from '../../../templates/server/observe.ts'

const [variant, port] = process.argv.slice(2)
const { handle } = await import(`./handlers_${variant === 'good' ? 'good' : 'bad'}.mjs`)
const env = { DB: new FakeD1(':memory:') }
await env.DB.exec('CREATE TABLE orders (id INTEGER PRIMARY KEY, total INTEGER, email TEXT); INSERT INTO orders (total) VALUES (10), (20);')

async function serve(req) {
  if (variant === 'nowrap') return handle(req, env, null)
  if (variant === 'fresh-id') {
    const id = crypto.randomUUID()
    const res = await handle(req, env, null)
    console.log(`done ${req.method} ${new URL(req.url).pathname} ${res.status} id=${id} for ana@example.com`)
    const headers = new Headers(res.headers)
    headers.set('x-request-id', id)
    return new Response(res.body, { status: res.status, headers })
  }
  return withObservability(req, env, (e, obs) => handle(req, e, obs), { withQueryCount })
}

// what a runtime prints on start: a package@version is not an e-mail
process.stdout.write('starting with hono@4.6.1 and wrangler@3.90.0\n')

http.createServer(async (inc, out) => {
  const chunks = []
  for await (const c of inc) chunks.push(c)
  const body = ['GET', 'HEAD'].includes(inc.method) ? undefined : Buffer.concat(chunks)
  const req = new Request(`http://${inc.headers.host}${inc.url}`, { method: inc.method, headers: inc.headers, body })
  const res = await serve(req)
  out.writeHead(res.status, Object.fromEntries(res.headers))
  out.end(Buffer.from(await res.arrayBuffer()))
}).listen(Number(port), '127.0.0.1')
