// The N+1 of handlers_bad behind a response cache: the second call (and every
// call after a reseed) costs 0 queries, so only the larger count shows it.
import { handle as slow } from './handlers_bad.mjs'
const cache = new Map()
export async function handle(req, env) {
  const key = new URL(req.url).pathname
  if (!cache.has(key)) cache.set(key, await (await slow(req, env)).text())
  return new Response(cache.get(key), { headers: { 'content-type': 'application/json' } })
}
