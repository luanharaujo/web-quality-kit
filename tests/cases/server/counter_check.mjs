// The counter on its own: production log, the headers only on localhost,
// batch and sessions counted. Prints "ok <name>" or "FAIL <name>".
import { DatabaseSync } from 'node:sqlite'
import { FakeD1 } from './fake_d1.mjs'
import { withQueryCount, routeTemplate } from '../../../templates/server/d1_queries.ts'

const file = ':memory:'
const db = new FakeD1(file)
await db.exec("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT); INSERT INTO t VALUES (1, 'a'), (2, 'b')")
db.withSession = () => db
let fails = 0
const check = (name, cond, detail = '') => {
  console.log(`${cond ? 'ok' : 'FAIL'} ${name}${cond ? '' : ' ' + detail}`)
  if (!cond) fails++
}
const handler = async (env) => {
  await env.DB.prepare('SELECT v FROM t WHERE id = ?').bind(1).first()
  await env.DB.batch([env.DB.prepare('SELECT 1'), env.DB.prepare('SELECT 2')])
  await env.DB.withSession().prepare('SELECT v FROM t').all()
  return new Response('ok', { headers: { 'x-app': '1' } })
}
const lines = []
const env = { DB: db, DB_QUERY_HEADERS: '1' }

const prod = await withQueryCount(new Request('https://api.example.com/users/12345/orders'), env, handler, { log: (l) => lines.push(l) })
check('production host never gets the headers, even with DB_QUERY_HEADERS=1', prod.headers.get('x-db-queries') === null && prod.headers.get('x-db-sql') === null)
check('the app response is untouched', (await prod.text()) === 'ok' && prod.headers.get('x-app') === '1')
const log = JSON.parse(lines[0] || '{}')
check('production log: event, route with :id, count', log.event === 'db_queries' && log.route === '/users/:id/orders' && log.queries === 4 && log.method === 'GET', lines[0])
check('production log carries no SQL', !lines[0].includes('SELECT'), lines[0])

const local = await withQueryCount(new Request('http://127.0.0.1:8932/x'), env, handler, { log: () => {} })
check('localhost with the variable: count header (batch counts each statement)', local.headers.get('x-db-queries') === '4', local.headers.get('x-db-queries'))
const sql = JSON.parse(Buffer.from(local.headers.get('x-db-sql'), 'base64').toString())
check('localhost: distinct SQL header', sql.length === 4 && sql.includes('SELECT v FROM t WHERE id = ?'), JSON.stringify(sql))

const off = await withQueryCount(new Request('http://127.0.0.1:8932/x'), { DB: db }, handler, { log: () => {} })
check('localhost without the variable: no header', off.headers.get('x-db-queries') === null)

let seen = null
await withQueryCount(new Request('http://127.0.0.1/x'), env, handler, { log: () => {}, onQueries: (_, q) => { seen = q } })
check('onQueries gets every query with its SQL', seen && seen.length === 4 && seen.every((q) => typeof q.sql === 'string' && q.ms >= 0))

const quiet = []
await withQueryCount(new Request('https://api.example.com/health'), env, async () => new Response('ok'), { log: (l) => quiet.push(l) })
check('a request with no query logs nothing', quiet.length === 0)
check('route template: uuid and long token', routeTemplate('/a/123e4567-e89b-12d3-a456-426614174000/b/abcd1234efgh5678') === '/a/:id/b/:id')
check('route template: e-mail, short code and encoded text never reach the log', routeTemplate('/users/jane@example.com/invite/AbCdEfGh') === '/users/:id/invite/:id' && routeTemplate('/search/Ren%C3%A9e/x7') === '/search/:id/:id', routeTemplate('/users/jane@example.com/invite/AbCdEfGh'))
check('route template: plain words stay', routeTemplate('/api/admin/order-requests/') === '/api/admin/order-requests')
check('route template: version segments stay', routeTemplate('/api/v1/orders/42') === '/api/v1/orders/:id', routeTemplate('/api/v1/orders/42'))
process.exit(fails ? 1 : 0)
