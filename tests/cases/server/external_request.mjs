// fetch(new Request(...)) and a Worker forwarding its incoming request.
export default { async fetch(request, env) {
  await fetch(new Request("/x", { signal: AbortSignal.timeout(10) }))
  await fetch(new Request("https://api.example.com/x", { signal: AbortSignal.timeout(10) }))
  await fetch(new Request("https://api.example.com/y"))
  await fetch(new Request("https://api.example.com/z"), { signal: AbortSignal.timeout(10) })
  return fetch(request)
}}
const other = (request) => fetch(request)
const api = { fetch(url) { return fetch(url) } }
const late = (u, init) => fetch(u, { ...init, signal: AbortSignal.timeout(999999) })
const early = (u, init) => fetch(u, { signal: AbortSignal.timeout(5000), ...init })
const proto = () => fetch('//api.example.com/x')
const MINUTE = 60 * 1000
const longish = (u) => fetch(u, { signal: AbortSignal.timeout(MINUTE) })
const inline = (u) => fetch(u, { signal: AbortSignal.timeout(2 * 60_000) })
const fine = (u) => fetch(u, { signal: AbortSignal.timeout(5 * 1000) })
function a(u) { const ms = 30000; return fetch(u, { signal: AbortSignal.timeout(ms) }) }
function b(u) { const ms = 5000; return fetch(u, { signal: AbortSignal.timeout(ms) }) }
const LIMIT = 5000
const byParam = (u, LIMIT) => fetch(u, { signal: AbortSignal.timeout(LIMIT) })
const local = (signal) => { const r = new Request('/local', { signal }); return fetch(r) }
export class Entry extends WorkerEntrypoint { fetch(request) { return fetch(request) } }
