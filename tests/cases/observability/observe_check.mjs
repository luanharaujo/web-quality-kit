// Node proof of templates/server/observe.ts: one "ok <name>" or "FAIL <name>" per line.
import { withObservability, cleanFields, safeRoute, validId, levelOf } from '../../../templates/server/observe.ts'
import { withQueryCount } from '../../../templates/server/d1_queries.ts'
import { FakeD1 } from '../server/fake_d1.mjs'

let fails = 0
const check = (name, cond, detail = '') => {
  console.log((cond ? 'ok ' : 'FAIL ') + name + (cond ? '' : `  ${JSON.stringify(detail)}`))
  if (!cond) fails++
}
// PII-shaped samples, built at runtime (no literal phone or CPF in the repository).
const D = (...parts) => parts.join('')
const CPF_DOTS = D('111', '.222', '.333', '-44')
const CPF_RAW = D('111', '222', '333', '44')
const PHONE_A = D('61 9', '9999', '-1234')
const PHONE_B = D('(61) 9', '8888', '-7777')
// the lines without their request ids (a random UUID can look like a phone)
const withoutIds = (lines) => JSON.stringify(lines.map(({ request_id, ...rest }) => rest))
const PII = /[\w.+-]+@[\w-]+\.[\w.-]+|\d{3}\.?\d{3}\.?\d{3}-?\d{2}|9\d{4}-?\d{4}/

async function call(path, { method = 'GET', id, handler, env = {} } = {}) {
  const lines = []
  const headers = id === undefined ? {} : { 'x-request-id': id }
  const req = new Request('https://app.example' + path, { method, headers })
  const res = await withObservability(req, env, handler || (() => new Response('ok')), {
    write: (l) => lines.push(JSON.parse(l)),
    withQueryCount,
  })
  return { res, lines, rid: res.headers.get('x-request-id') }
}

// request id
let r = await call('/x')
check('new id when none is sent', validId(r.rid), r.rid)
r = await call('/x', { id: 'caller-id-123' })
check('valid caller id is kept', r.rid === 'caller-id-123', r.rid)
for (const bad of [D('111', '22233344'), D('123456', '789-09'), D('cpf-123', '45678901'), D('tel-61-9', '9999-9999'), 'short', 'has space inside', 'x'.repeat(65), 'quote"inject12', '../../etc/passwd'])
  check(`invalid caller id replaced (${bad.slice(0, 12)})`, (await call('/x', { id: bad })).rid !== bad)
const a = await call('/x'), b = await call('/x')
check('two requests get different ids', a.rid !== b.rid)

// one line per request with the fixed fields, same id everywhere
const db = new FakeD1(':memory:')
await db.exec('CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER); INSERT INTO t (v) VALUES (1), (2);')
r = await call('/orders/42/items/0a1b2c3d4e5f60718293a4b5', {
  id: 'req-abcdef12',
  env: { DB: db },
  handler: async (env, obs) => {
    await env.DB.prepare('SELECT * FROM t').all()
    await env.DB.prepare('SELECT * FROM t WHERE id = ?').bind(1).first()
    obs.log.info('looked_up', { rows: 2 })
    return new Response('ok')
  },
})
const req = r.lines.at(-1)
check('request line has the fixed fields in order',
  Object.keys(req).join() === 'time,level,event,request_id,route,method,status,ms,queries', Object.keys(req))
check('request line: route is a template', req.route === '/orders/:id/items/:id', req.route)
check('request line: D1 queries counted', req.queries === 2, req.queries)
check('request line: status and level', req.status === 200 && req.level === 'info', req)
check('every line of the request has the same id', r.lines.length === 2 && r.lines.every((l) => l.request_id === 'req-abcdef12'), r.lines)
check('event line carries route and method too', r.lines[0].route === req.route && r.lines[0].method === 'GET', r.lines[0])
check('time is ISO 8601', !Number.isNaN(Date.parse(req.time)) && req.time.endsWith('Z'), req.time)

// outgoing calls carry the id
const realFetch = globalThis.fetch
let sent = null
globalThis.fetch = async (req2) => { sent = req2.headers.get('x-request-id'); return new Response('ok') }
await call('/x', { id: 'forward-id-1', handler: async (env, obs) => { await obs.fetch('https://other.example/api', { method: 'POST' }); return new Response('ok') } })
check('obs.fetch forwards x-request-id', sent === 'forward-id-1', sent)
await call('/x', { handler: async (env, obs) => { await obs.fetch('https://other.example/api', { headers: { 'x-request-id': 'own-id-12345' } }); return new Response('ok') } })
check('obs.fetch keeps an id the app set on purpose', sent === 'own-id-12345', sent)
const proxied = await call('/x', { id: 'bad id', handler: async (env, obs) => { await obs.fetch('https://other.example/api', { headers: { 'x-request-id': 'bad id' } }); return new Response('ok') } })
check('obs.fetch replaces an invalid id copied from the request (proxy)', sent === proxied.rid, [sent, proxied.rid])
globalThis.fetch = realFetch

// no personal data in the lines
r = await call(`/users/ana%40example.com/cpf/${CPF_DOTS}`, {
  handler: (env, obs) => {
    obs.log.warn('signup', {
      email: 'ana@example.com', name: 'Ana Souza', mobile: PHONE_A, cpf: CPF_RAW,
      note: `contact ana@example.com or ${PHONE_B}, cpf ${CPF_DOTS}`, profile: { email: 'x@y.z' },
      count: 3, ok: true, error: new Error('lookup failed for bruno@example.org'),
      request_id: 'overwrite', status: 999,
    })
    return new Response('no', { status: 400 })
  },
})
const text = withoutIds(r.lines)
check('no e-mail, CPF or phone anywhere in the lines', !PII.test(text), text)
const ev = r.lines[0]
check('personal keys removed', ['email', 'name', 'mobile', 'cpf'].every((k) => ev[k] === '[removed]'), ev)
check('objects are not logged', ev.profile === '[removed]', ev.profile)
check('numbers and booleans kept', ev.count === 3 && ev.ok === true, ev)
check('error keeps only its type (the message can carry tokens)', ev.error === 'Error', ev.error)
check('fixed fields cannot be overwritten by an event', ev.request_id === r.rid && !('status' in ev), ev)
check('path with e-mail and CPF becomes :id', r.lines.at(-1).route === '/users/:id/cpf/:id', r.lines.at(-1).route)
check('4xx is a warn line', r.lines.at(-1).level === 'warn')
check('a log call with null fields does not break the request', (() => { try { cleanFields(null); return true } catch { return false } })())
check('cleanFields drops undefined', !('x' in cleanFields({ x: undefined })))
check('safeRoute: plain words stay', safeRoute('/api/workouts/week') === '/api/workouts/week')
check('level of 503 is error', levelOf(503) === 'error')

// personal keys and credential-shaped text
const KEYS_OUT = ['customerName', 'customerFullName', 'CPFNumber', 'userRg', 'username', 'apikey', 'apiKey',
  'refreshTokens', 'sessionId', 'hostname', 'deviceName', 'pwd', 'sid', 'pin', 'otp', 'cvv', 'rg', 'cep',
  'latitude', 'lat', 'location', 'ip', 'user_id', 'pix', 'login', 'iban', 'Authorization']
const kf = cleanFields(Object.fromEntries(KEYS_OUT.map((k) => [k, 'x'])))
check('personal words anywhere in the key are removed', KEYS_OUT.every((k) => kf[k] === '[removed]'),
  KEYS_OUT.filter((k) => kf[k] !== '[removed]'))
const kept = cleanFields({ org: 'a', origin: 'b', eventName: 'c', plan: 'd', total: 5, auth_method: 'passkey', result: 'ok' })
check('technical keys stay (org, origin, eventName, auth_method)', Object.values(kept).every((v) => v !== '[removed]'), kept)
check('personalWords adds the app\'s own words', cleanFields({ handle: 'x' }).handle === 'x' &&
  cleanFields({ customerHandle: 'x' }, new Set(['handle'])).customerHandle === '[removed]')
check('a key that is itself an e-mail is removed', JSON.stringify(cleanFields({ 'ana@example.com': 1 })) === '{"[removed]":"[removed]"}')
const TEXTS = {
  url: 'callback https://api.example.com/x?token=abc123&user=9 failed',
  jwt: D('eyJhbGciOiJIUzI1NiJ9', '.eyJzdWIiOiIxMjM0In0', '.c2lnbmF0dXJl'),
  cnpj: D('12.345', '.678/0001-95'),
  cpfSpace: D('111 222', ' 333 44'),
  cpfGlued: D('cpf', '11122233344', 'ok'),
  phoneNoArea: D('call 9', '8888-7777'),
  digits: D('order 1234', '5678 done'),
  pct40: 'ana%40example.com',
  authHeader: 'Authorization: Digest username="a", response="b"',
  bearer: 'sent Bearer abc.def-123',
  tokenEq: 'retry with token=s3cr3t&x=1',
  password: 'password: hunter2 my long phrase',
  jsonPassword: '{\\"password\\":\\"hunter2\\"}',
  cookie: 'Set-Cookie: sid=abc; HttpOnly',
  ip: D('from 10.0', '.0.1'),
  ipv6: D('client 2001:db8', ':5c80:8a1f::1 refused'),
  clientSecret: 'client_secret=abc123XY',
  clientSecretJson: '{"client_secret":"abc123XY"}',
  dbPassword: 'db_password=hunter2',
  authToken: 'auth_token=abcd',
  xApiKey: D('x-api-key: sk', '_live_zzzzzz'),
  apiKeyDash: 'api-key=zzzzzz',
  cookieJson: '{"cookie":"sess=abcXYZ"}',
  authJson: '{"authorization":"ApiKey abcdefgh"}',
  setCookieJson: '{"headers":{"set-cookie":"a=zzzz"}}',
}
const LEAKS = { url: 'abc123', jwt: 'eyJ', cnpj: '0001', cpfSpace: '333', cpfGlued: '222', phoneNoArea: '7777',
  digits: '5678', pct40: 'ana', authHeader: 'response', bearer: 'def-123', tokenEq: 's3cr3t', password: 'phrase',
  jsonPassword: 'hunter2', cookie: 'abc', ip: D('10.0', '.0.1'), ipv6: '8a1f', clientSecret: 'abc123XY', clientSecretJson: 'abc123XY',
  dbPassword: 'hunter2', authToken: 'abcd', xApiKey: 'zzzzzz', apiKeyDash: 'zzzzzz',
  cookieJson: 'abcXYZ', authJson: 'abcdefgh', setCookieJson: 'zzzz' }
const tf = cleanFields(TEXTS)
for (const [k, leak] of Object.entries(LEAKS)) check(`text: ${k} removed`, !String(tf[k]).includes(leak), tf[k])
check('a time of day is not an IPv6 address', cleanFields({ t: 'at 10:30:00' }).t === 'at 10:30:00', cleanFields({ t: 'at 10:30:00' }).t)
check('a run of 70 digits is removed too', cleanFields({ n: '7'.repeat(70) }).n === '[removed]', cleanFields({ n: '7'.repeat(70) }).n)
check('a UUID from the browser is a valid id', validId(crypto.randomUUID()))
check('integers with 8+ digits removed, small ones kept', cleanFields({ n: Number(D('6199', '9991234')), m: 42 }).n === '[removed]' && cleanFields({ m: 42 }).m === 42)
for (const [path, want] of [['/api/Items/x', '/api/:id/x'], ['/a/token=abc/b', '/a/:id/b'], ['/a/x.json', '/a/:id'],
  ['/a/' + 'k'.repeat(24), '/a/:id'], ['/a/b+c', '/a/:id'], ['/a/v2', '/a/:id'], ['/api/workouts', '/api/workouts']])
  check(`route ${path} → ${want}`, safeRoute(path) === want, safeRoute(path))
const t0 = Date.now()
cleanFields({ big: 'a@'.repeat(20000) + '1'.repeat(20000), big2: ' 9'.repeat(20000) })
check('40 thousand characters are cleaned fast (< 200 ms)', Date.now() - t0 < 200, Date.now() - t0)

const GLUED = ['firstname', 'fullname', 'useremail', 'phonenumber', 'addresses', 'nickname', 'displayName', 'ipaddress']
const gf = cleanFields(Object.fromEntries(GLUED.map((k) => [k, 'x'])))
check('words glued without a separator are removed', GLUED.every((k) => gf[k] === '[removed]'), GLUED.filter((k) => gf[k] !== '[removed]'))
const tech = cleanFields({ filename: 'a.txt', pathname: '/x', tableName: 'orders', rename: true })
check('technical *name keys stay (filename, pathname, tableName)', Object.values(tech).every((v) => v !== '[removed]'), tech)
check('package@version is not an e-mail', cleanFields({ dep: 'hono@4.6.1 and wrangler@3.90.0' }).dep === 'hono@4.6.1 and wrangler@3.90.0',
  cleanFields({ dep: 'hono@4.6.1 and wrangler@3.90.0' }).dep)

// an exception still answers with the id, and the log has it
r = await call('/boom', { id: 'crash-id-001', handler: () => { throw new Error('db down for ana@example.com') } })
const body = await r.res.json()
check('exception: 500 with the request id in body and header', r.res.status === 500 && body.request_id === 'crash-id-001' && r.rid === 'crash-id-001', body)
check('exception: the error line says where (file:line), not the message', /^[\w.-]+:\d+$/.test(r.lines[0].at), r.lines[0].at)
check('exception: error line and request line, same id, no e-mail',
  r.lines.length === 2 && r.lines[0].level === 'error' && r.lines[0].event === 'unhandled_error' &&
  r.lines.every((l) => l.request_id === 'crash-id-001') && !PII.test(withoutIds(r.lines)), r.lines)

for (const [what, h] of [['nothing', () => undefined], ['Response.error()', () => Response.error()]]) {
  r = await call('/x', { id: 'empty-id-001', handler: h })
  check(`a handler that returns ${what}: 500 with the id, and logged`, r.res.status === 500 && r.rid === 'empty-id-001' &&
    r.lines.some((l) => l.event === 'unhandled_error'), r.lines)
}

// headers of an immutable response (Response.redirect) still get the id
r = await call('/go', { handler: () => Response.redirect('https://app.example/x', 302) })
check('redirect gets the id too', r.res.status === 302 && validId(r.rid))

process.exit(fails ? 1 : 0)
