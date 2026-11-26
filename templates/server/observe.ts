// web-quality-kit: structured logs and request ids for Cloudflare Workers (+ D1).
//
// Copy this file and d1_queries.ts into the same folder of the app, unchanged
// (the server-observability check compares both with the kit's copies). Every
// request goes through withObservability:
//
//   import { withObservability } from './observe'
//   import { withQueryCount } from './d1_queries'
//   export default {
//     fetch: (req, env, ctx) =>
//       withObservability(req, env, (env, obs) => handle(req, env, ctx, obs), { withQueryCount }),
//   }
//
// - Request id: the caller's x-request-id is kept when valid (8 to 64 of
//   A-Z a-z 0-9 _ -, with a letter and no run of 8+ digits outside a UUID,
//   counting across - and _),
//   otherwise a new one is made. It goes back in the
//   response (x-request-id), on every log line of the request, and on calls
//   to other services made with obs.fetch.
// - One JSON line per request, fixed fields:
//   {"time","level","event":"request","request_id","route","method","status","ms","queries"}
//   route is the path with every part that is not a plain lowercase word
//   replaced by :id (no query string); queries is the D1 count from the kit's
//   d1_queries.ts, passed as opts.withQueryCount (0 without it or env.DB).
// - Other events only through obs.log.info/warn/error(event, fields). Fields
//   are flat (string, number, boolean). A key with a personal word in it
//   (customerName, userRg, sessionId, ip...) becomes "[removed]", and so does
//   any text that looks like personal data or a credential (e-mail, CPF,
//   CNPJ, phone, IP, URL, token, password, cookie, 8+ digits) and any integer
//   with 8+ digits. An Error keeps only its type (and a code like SQLITE_BUSY).
// - An exception in the handler becomes a 500 with {"error":"internal_error","request_id"}
//   and a log line with level "error", so the id the user sees is in the log.
// - console.log/console.error outside this file fail the kit's lint: log
//   through obs.log.
//
// Kept to plain TypeScript (no enums, no decorators) so Node can also run it
// with type stripping in the kit's own tests. No runtime import: the counter
// comes in through the options, so the file works with or without D1.

import type { QueryCountOptions } from './d1_queries'

export const REQUEST_ID_HEADER = 'x-request-id'
const ID_OK = /^[A-Za-z0-9_-]{8,64}$/

export type Level = 'info' | 'warn' | 'error'
export type Field = string | number | boolean | null | undefined | Error
export type Fields = Record<string, Field>

export interface Log {
  info(event: string, fields?: Fields): void
  warn(event: string, fields?: Fields): void
  error(event: string, fields?: Fields): void
}

export interface Obs {
  requestId: string
  log: Log
  // fetch that forwards the request id to the service called.
  fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response>
}

export interface ObserveOptions {
  binding?: string // name of the D1 binding in env (default "DB")
  route?: (req: Request) => string // route template for the log
  write?: (line: string) => void // default console.log (tests capture here)
  now?: () => Date
  // Personal words of the app's own language, lowercase (its words for name,
  // phone, address, password...). Matched like the built-in English ones.
  personalWords?: string[]
  // The kit's withQueryCount (d1_queries.ts): adds the D1 count to the line.
  withQueryCount?: <T extends Record<string, any>>(
    req: Request,
    env: T,
    handler: (env: T) => Promise<Response> | Response,
    opts?: QueryCountOptions,
  ) => Promise<Response>
}

// Fields the request line owns: an event can never overwrite them.
const FIXED = new Set(['time', 'level', 'event', 'request_id', 'route', 'method', 'status', 'ms', 'queries'])
// Words that make a field personal, matched as whole words of the key
// (camelCase, acronyms, "_" and "-" split it): customerName, CPFNumber,
// userRg, refreshTokens, sessionId all go; "org" or "origin" do not.
// The app adds the words of its own language with opts.personalWords.
const PERSONAL_WORDS = new Set([
  'email', 'mail', 'name', 'names', 'surname', 'username', 'login', 'user',
  'phone', 'mobile', 'whatsapp', 'cpf', 'cnpj', 'rg', 'pix', 'iban', 'card',
  'address', 'cep', 'zip', 'birth', 'birthday', 'password', 'pwd',
  'secret', 'token', 'apikey', 'cookie', 'authorization', 'session', 'sid', 'pin', 'otp', 'cvv',
  'ip', 'host', 'hostname', 'device', 'latitude', 'lat', 'longitude', 'lon', 'lng', 'location', 'geo',
])
// Words that remove the field even glued to others (useremail, phonenumber,
// addresses, sessionid): long and unambiguous enough to look for inside a word.
const PERSONAL_PARTS = ['email', 'phone', 'password', 'passwd', 'secret', 'token', 'cookie', 'address',
  'birth', 'cpf', 'cnpj', 'whatsapp', 'latitude', 'longitude', 'iban', 'apikey', 'session', 'username']
// A word ending in "name" is a person's name (firstname, fullname, nickname)
// unless it is one of these technical names (also as a whole key: tableName).
const TECHNICAL_NAMES = new Set(['filename', 'pathname', 'eventname', 'routename', 'typename', 'classname',
  'tagname', 'keyname', 'fieldname', 'tablename', 'columnname', 'servicename', 'appname', 'bucketname', 'dbname',
  'rename', 'basename', 'dirname', 'modelname', 'methodname', 'funcname', 'functionname', 'filenames'])
const REMOVED = '[removed]'
const MAX_TEXT = 200 // what a field keeps
const MAX_SCAN = 2000 // text is cut to this before the patterns run (no slow regex on huge input)

// Every pattern has bounded repetition, so a long string never makes it slow.
const TEXT_PATTERNS: RegExp[] = [
  // whole URLs (they carry tokens and ids in the query string)
  /\b[a-z][a-z0-9+.-]{1,15}:\/\/[^\s"'<>]{1,2000}/gi,
  // credentials in headers or key=value: to the end of the line
  // (also inside JSON: "cookie":"..." and escaped \"authorization\":...)
  /\b(?:proxy-)?authorization\\?["']?\s*[:=][^\r\n]{0,2000}/gi,
  /\b(?:set-)?cookie\\?["']?\s*[:=][^\r\n]{0,2000}/gi,
  // any key that ends in one of these words: client_secret, db_password, auth_token, x-api-key
  /[\w-]{0,40}(?:password|passwd|pwd|secret|token|api[_-]?key|session[_-]?id|sid)\\?["']?\s*[:=][^\r\n]{0,2000}/gi,
  /\b(?:bearer|basic|digest|token)\s+[\w.~+/=-]{4,2000}/gi,
  // JWT
  /\beyJ[\w-]{4,2000}\.[\w-]{4,2000}\.?[\w-]{0,2000}/g,
  // e-mail, also URL-encoded
  /[\w.+-]{1,64}(?:@|%40)[\w-]{1,63}(?:\.[\w-]{1,63}){0,7}\.[a-z]{2,24}\b/gi,
  // CNPJ and CPF, with or without dots, spaces, glued to letters
  /(?<!\d)\d{2}[.\s]?\d{3}[.\s]?\d{3}[/\s]?\d{4}[-\s]?\d{2}(?!\d)/g,
  /(?<!\d)\d{3}[.\s]?\d{3}[.\s]?\d{3}[-\s]?\d{2}(?!\d)/g,
  // phone numbers, with or without country and area code
  /(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2}\)?[\s.-]?)?9?\d{4}[\s.-]?\d{4}(?!\d)/g,
  // IPv4 and IPv6 addresses (4+ groups, or a "::"; a time like 10:30:00 stays) and any run of 8 or more digits
  /(?<!\d)\d{1,3}(?:\.\d{1,3}){3}(?!\d)/g,
  /(?<![\w:])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?![\w:])/gi,
  /(?<![\w:])(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?(?![\w:])/gi,
  /(?<!\d)\d{8,}(?!\d)/g,
]

export function validId(id: string | null | undefined): id is string {
  // An id needs a letter and no run of 8+ digits outside a UUID, counting
  // across - and _: a CPF or a phone sent as an id (cpf-12345678901,
  // tel-61-99999-9999) would go to the log as is.
  return (
    typeof id === 'string' &&
    ID_OK.test(id) &&
    /[A-Za-z]/.test(id) &&
    !/\d{8}/.test(id.replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, '').replace(/[-_]/g, ''))
  )
}

export function newId(): string {
  return crypto.randomUUID()
}

export function requestId(req: Request): string {
  const given = req.headers.get(REQUEST_ID_HEADER)
  return validId(given) ? given : newId()
}

// Text without anything that looks like personal data or a credential:
// URLs, tokens, e-mails, CPF, CNPJ, phones, IPs, long numbers.
export function clean(text: string): string {
  let t = text.slice(0, MAX_SCAN)
  for (const p of TEXT_PATTERNS) t = t.replace(p, REMOVED)
  return t.slice(0, MAX_TEXT)
}

// Words of a key: customerName → customer, name; CPFNumber → cpf, number.
export function keyWords(key: string): string[] {
  return key
    .slice(0, 100)
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .map((w) => w.toLowerCase())
}

export function personalKey(key: string, extra: ReadonlySet<string> = new Set()): boolean {
  // technical keys stay whole: eventName, table_name, fileName
  if (TECHNICAL_NAMES.has(key.toLowerCase().replace(/[^a-z0-9]/g, ''))) return false
  const words = keyWords(key)
  const personal = (w: string) => PERSONAL_WORDS.has(w) || extra.has(w)
  for (let i = 0; i < words.length; i++) {
    const w = words[i]
    if (personal(w) || (w.endsWith('s') && personal(w.slice(0, -1))) || (w.endsWith('es') && personal(w.slice(0, -2))))
      return true
    if (PERSONAL_PARTS.some((part) => w.includes(part))) return true
    if (/names?$/.test(w) && w.length > 5 && !TECHNICAL_NAMES.has(w)) return true
    if (w === 'api' && words[i + 1] === 'key') return true
  }
  return false
}

// An error keeps only its type (and a code like SQLITE_BUSY): the message
// often carries a URL with a token or the values of the query.
export function errorText(err: Error): string {
  const code = (err as { code?: unknown }).code
  const name = /^[A-Za-z_$][\w$]{0,60}$/.test(err.name) ? err.name : 'Error'
  return typeof code === 'string' && /^[A-Z0-9_]{1,40}$/.test(code) ? `${name}:${code}` : name
}

export function cleanFields(
  fields: Fields = {},
  extra: ReadonlySet<string> = new Set(),
): Record<string, string | number | boolean | null> {
  const out: Record<string, string | number | boolean | null> = {}
  for (const [key, value] of Object.entries(fields || {})) {
    if (FIXED.has(key) || value === undefined) continue
    if (clean(key) !== key) out[REMOVED] = REMOVED // the key itself is personal data
    else if (personalKey(key, extra)) out[key] = REMOVED
    else if (value instanceof Error) out[key] = errorText(value)
    else if (typeof value === 'string') out[key] = clean(value)
    else if (typeof value === 'number' && Number.isInteger(value) && Math.abs(value) >= 1e7) out[key] = REMOVED
    else if (value === null || typeof value === 'number' || typeof value === 'boolean') out[key] = value
    else out[key] = REMOVED // objects and arrays: per-user detail does not go in logs
  }
  return out
}

// Path → route template: numbers, UUIDs and long tokens become :id (the same
// rule as d1_queries.ts).
export function routeTemplate(path: string): string {
  return (
    path
      .split('/')
      .map((part) =>
        /^\d+$/.test(part) ||
        /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(part) ||
        (part.length >= 16 && /\d/.test(part) && /^[\w-]+$/.test(part))
          ? ':id'
          : part,
      )
      .join('/')
      .replace(/\/+$/, '') || '/'
  )
}

// Route template for the log: ids become :id, and so does any part that is
// not a plain lowercase word: a digit, an uppercase letter, any of = + @ % .,
// 24 or more characters, or anything that looks like personal data.
export function safeRoute(path: string): string {
  let decoded = path.slice(0, MAX_SCAN)
  try {
    decoded = decodeURIComponent(decoded)
  } catch {
    // keep the raw path
  }
  return routeTemplate(decoded)
    .split('/')
    .map((part) =>
      part && part !== ':id' && (/[\dA-Z=+@%.]/.test(part) || part.length >= 24 || clean(part) !== part) ? ':id' : part,
    )
    .join('/')
}

// File and line of the first stack frame (no message, no arguments):
// "worker.js:120". Empty when the stack has none.
export function stackPlace(err: Error): string {
  const frame = String(err.stack || '').split('\n').slice(1, 2).join('')
  const m = frame.match(/([\w.-]{1,80}):(\d{1,7}):\d{1,7}\)?\s*$/)
  return m ? `${m[1]}:${m[2]}` : ''
}

export function levelOf(status: number): Level {
  return status >= 500 ? 'error' : status >= 400 ? 'warn' : 'info'
}

export async function withObservability<E extends Record<string, any>>(
  req: Request,
  env: E,
  handler: (env: E, obs: Obs) => Promise<Response> | Response,
  opts: ObserveOptions = {},
): Promise<Response> {
  const write = opts.write || ((line: string) => console.log(line))
  const now = opts.now || (() => new Date())
  const id = requestId(req)
  const method = req.method
  let route = '/'
  try {
    route = opts.route ? opts.route(req) : safeRoute(new URL(req.url).pathname)
  } catch {
    // a broken route function never breaks the request
  }
  const base = { request_id: id, route, method }
  const extra = new Set((opts.personalWords || []).map((w) => w.toLowerCase()))
  const emit = (level: Level, event: string, fields?: Fields) =>
    write(JSON.stringify({ time: now().toISOString(), level, event: clean(String(event)).slice(0, 64), ...base, ...cleanFields(fields, extra) }))
  const log: Log = {
    info: (event, fields) => emit('info', event, fields),
    warn: (event, fields) => emit('warn', event, fields),
    error: (event, fields) => emit('error', event, fields),
  }
  const obs: Obs = {
    requestId: id,
    log,
    fetch: (input, init) => {
      const outgoing = new Request(input as RequestInfo, init)
      const headers = new Headers(outgoing.headers)
      // An id the app set on purpose stays; one copied from the incoming
      // request (a proxy) or not valid becomes the id of this request.
      const current = headers.get(REQUEST_ID_HEADER)
      if (!validId(current) || (current !== id && current === req.headers.get(REQUEST_ID_HEADER))) {
        headers.set(REQUEST_ID_HEADER, id)
      }
      return fetch(new Request(outgoing, { headers }))
    },
  }

  const binding = opts.binding || 'DB'
  let queries = 0
  const t0 = Date.now()
  let res: Response
  try {
    res = opts.withQueryCount && env && env[binding]
      ? await opts.withQueryCount(req, env, (counted) => handler(counted, obs), {
          binding,
          route: () => route,
          log: () => {},
          onQueries: (_req, list) => {
            queries = list.length
          },
        })
      : await handler(env, obs)
    // a handler that returns nothing or Response.error() is a server error too
    if (!(res instanceof Response) || res.type === 'error') throw new TypeError('the handler did not return a Response')
  } catch (err) {
    const e = err instanceof Error ? err : new Error(String(err))
    emit('error', 'unhandled_error', { error: e, at: stackPlace(e) })
    res = new Response(JSON.stringify({ error: 'internal_error', request_id: id }), {
      status: 500,
      headers: { 'content-type': 'application/json' },
    })
  }
  const ms = Date.now() - t0
  write(JSON.stringify({ time: now().toISOString(), level: levelOf(res.status), event: 'request', ...base, status: res.status, ms, queries }))
  // A WebSocket upgrade keeps its own response object.
  if (res.status === 101) return res
  // new Response(body, res) keeps what the runtime adds (cf, encodeBody), so
  // a compressed body is not compressed again.
  const out = new Response(res.body, res)
  out.headers.set(REQUEST_ID_HEADER, id)
  return out
}
