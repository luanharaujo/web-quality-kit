const worker = {
  async fetch(request: Request, env: Env) { return fetch(request) },
}
export default worker
const n = (u: string) => fetch(u, { signal: undefined })
