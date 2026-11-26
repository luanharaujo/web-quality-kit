// web-quality-kit: database query counter for Cloudflare Workers + D1.
//
// Copy this file into the app unchanged (the server-queries check compares it
// with the kit's copy). It wraps env.DB so every request knows how many
// queries it made:
//
//   import { withQueryCount } from './d1_queries'
//   export default {
//     fetch: (req, env, ctx) => withQueryCount(req, env, (env) => handle(req, env, ctx)),
//   }
//
// - Production: one structured log line per request that touched the
//   database: {"event":"db_queries","method","route","status","queries","ms"}.
//   The route is the path with ids replaced by :id (no personal data), and
//   the SQL is never logged (it may carry inline values).
// - Local tests: with DB_QUERY_HEADERS = "1" (in .dev.vars, never in
//   wrangler.toml) and a request to localhost/127.0.0.1, the response also
//   carries x-db-queries: <count> and x-db-sql: the distinct SQL texts of
//   the request (base64 of a JSON list), for the kit's index check (EXPLAIN
//   QUERY PLAN). Any other host never gets them, even with the variable set
//   by mistake.
// - opts.onQueries receives every query (SQL text and time) for checks that
//   need them, such as an index checker (EXPLAIN QUERY PLAN) in tests.
//
// Kept to plain TypeScript (no enums, no decorators) so Node can also run it
// with type stripping in the kit's own tests.

export interface QueryRecord {
  sql: string
  ms: number
  kind: 'first' | 'all' | 'run' | 'raw' | 'batch' | 'exec'
}

export interface QueryCountOptions {
  binding?: string // name of the D1 binding in env (default "DB")
  route?: (req: Request) => string // route template for the log
  log?: (line: string) => void // default console.log
  onQueries?: (req: Request, queries: QueryRecord[]) => void
}

export const QUERY_HEADER = 'x-db-queries'
export const SQL_HEADER = 'x-db-sql'
const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]', '::1'])
// Symbols the statement proxy answers, so batch() can unwrap it.
const RAW = Symbol('db-queries.raw')
const SQL = Symbol('db-queries.sql')

// Wraps a D1Database (or a D1 session) so each statement it runs is recorded.
// batch([...]) counts one query per statement: a batch of N lookups built in a
// loop still grows with N.
export function countQueries<T extends object>(db: T, queries: QueryRecord[]): T {
  const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())
  const wrapStatement = (stmt: any, sql: string): any =>
    new Proxy(stmt, {
      get(target, prop) {
        if (prop === RAW) return target
        if (prop === SQL) return sql
        const value = Reflect.get(target, prop, target)
        if (typeof value !== 'function') return value
        if (prop === 'bind') return (...args: unknown[]) => wrapStatement(value.apply(target, args), sql)
        if (prop === 'first' || prop === 'all' || prop === 'run' || prop === 'raw') {
          return async (...args: unknown[]) => {
            const t = now()
            try {
              return await value.apply(target, args)
            } finally {
              queries.push({ sql, ms: now() - t, kind: prop })
            }
          }
        }
        return value.bind(target)
      },
    })
  return new Proxy(db, {
    get(target, prop) {
      const value = Reflect.get(target, prop, target)
      if (typeof value !== 'function') return value
      if (prop === 'prepare') return (sql: string) => wrapStatement(value.call(target, sql), sql)
      if (prop === 'batch') {
        return async (stmts: any[]) => {
          const t = now()
          // The real statements, not the proxies: D1 checks what it gets.
          const raw = stmts.map((s) => (s && s[RAW]) || s)
          try {
            return await value.call(target, raw)
          } finally {
            const ms = (now() - t) / Math.max(1, stmts.length)
            for (const s of stmts) queries.push({ sql: (s && s[SQL]) || '(batch)', ms, kind: 'batch' })
          }
        }
      }
      if (prop === 'exec') {
        return async (sql: string) => {
          const t = now()
          try {
            return await value.call(target, sql)
          } finally {
            queries.push({ sql, ms: now() - t, kind: 'exec' })
          }
        }
      }
      if (prop === 'withSession') return (...args: unknown[]) => countQueries(value.apply(target, args), queries)
      return value.bind(target)
    },
  }) as T
}

// Path → route template: numbers, ids, tokens, e-mails, encoded text and
// words with capitals become :id; plain lowercase words (users, by-date) and
// version segments (v1) stay. A lowercase value in the path (/users/jane) is
// indistinguishable from a word: pass opts.route with the app's own route
// pattern when a path can carry one.
export function routeTemplate(path: string): string {
  return (
    path
      .split('/')
      .map((part) => (part === '' || /^[a-z][a-z_-]{0,31}$/.test(part) || /^v\d{1,2}$/.test(part) ? part : ':id'))
      .join('/')
      .replace(/\/+$/, '') || '/'
  )
}

export function headerAllowed(req: Request, env: Record<string, unknown>): boolean {
  if (env.DB_QUERY_HEADERS !== '1') return false
  return LOCAL_HOSTS.has(new URL(req.url).hostname)
}

export async function withQueryCount<E extends Record<string, any>>(
  req: Request,
  env: E,
  handler: (env: E) => Promise<Response> | Response,
  opts: QueryCountOptions = {},
): Promise<Response> {
  const binding = opts.binding || 'DB'
  if (!env || !env[binding]) return handler(env)
  const queries: QueryRecord[] = []
  const counted = { ...env, [binding]: countQueries(env[binding], queries) } as E
  const t0 = Date.now()
  let res: Response
  try {
    res = await handler(counted)
  } catch (err) {
    report(req, opts, queries, 500, Date.now() - t0)
    throw err
  }
  report(req, opts, queries, res.status, Date.now() - t0)
  if (!headerAllowed(req, env)) return res
  const headers = new Headers(res.headers)
  headers.set(QUERY_HEADER, String(queries.length))
  headers.set(SQL_HEADER, toBase64(JSON.stringify([...new Set(queries.map((q) => q.sql))])))
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers })
}

function report(req: Request, opts: QueryCountOptions, queries: QueryRecord[], status: number, ms: number) {
  if (opts.onQueries) opts.onQueries(req, queries)
  if (queries.length === 0) return
  const route = opts.route ? opts.route(req) : routeTemplate(new URL(req.url).pathname)
  const line = JSON.stringify({ event: 'db_queries', method: req.method, route, status, queries: queries.length, ms })
  ;(opts.log || console.log)(line)
}

function toBase64(text: string): string {
  let bin = ''
  for (const b of new TextEncoder().encode(text)) bin += String.fromCharCode(b)
  return btoa(bin)
}
