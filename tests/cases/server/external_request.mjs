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
