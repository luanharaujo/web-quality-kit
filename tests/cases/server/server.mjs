// node server.mjs [--no-header] <good|bad> <db file> <port>: an example
// server wired like a Worker (fetch handler wrapped by withQueryCount).
// --no-header: the handler is not wrapped (a server that forgot the counter).
import http from 'node:http'
import { FakeD1 } from './fake_d1.mjs'
import { withQueryCount } from '../../../templates/server/d1_queries.ts'

const args = process.argv.slice(2)
const wrap = args[0] !== '--no-header'
if (!wrap) args.shift()
const [variant, file, port] = args
const { handle } = await import(`./handlers_${variant}.mjs`)
const env = { DB: new FakeD1(file), DB_QUERY_HEADERS: '1' }

http.createServer(async (inc, out) => {
  const req = new Request(`http://${inc.headers.host}${inc.url}`, { method: inc.method })
  const res = wrap ? await withQueryCount(req, env, (e) => handle(req, e), { log: () => {} }) : await handle(req, env)
  out.writeHead(res.status, Object.fromEntries(res.headers))
  out.end(Buffer.from(await res.arrayBuffer()))
}).listen(Number(port), '127.0.0.1')
