// Calls that can hang the request for as long as the other side wants.
export async function calls(url, token, API) {
  await fetch('https://api.example.com/x')
  await fetch(`https://api.example.com/y?t=${token}`, { method: 'POST' })
  await fetch(url, { headers: {} })
  await fetch('https://api.example.com/slow', { signal: AbortSignal.timeout(60000) })
  await globalThis.fetch('https://api.example.com/g')
  await fetch(`${API}/users`)
  await fetch(new URL('/x', 'https://api.example.org'))
  await fetch('https://api.example.com/x')
}
