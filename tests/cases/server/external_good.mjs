// Calls that end on time, or that never leave the Worker.
export async function calls(env, id, init) {
  await fetch('https://api.example.com/x', { signal: AbortSignal.timeout(5000) }) // kit:short-timeout a case for the server checks, never run as a test
  const ctl = new AbortController()
  setTimeout(() => ctl.abort(), 3000)
  await fetch(`https://api.example.com/y/${id}`, { method: 'POST', signal: ctl.signal })
  await fetch('/internal/path')
  await fetch(`/items/${id}`)
  await env.API.fetch('https://anything/x') // a service binding
  await fetch('https://own.example.org/z') // listed as internal
  await fetch('https://api.example.com/w', { ...init }) // the caller's init may carry the signal
}
