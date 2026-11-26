import { withObservability } from './observe'
export default {
  async fetch(req: Request, env: unknown) {
    return withObservability(req, env, async (_env, o) => {
      await o.fetch('https://api.third.example/a')
      await o.fetch('https://api.third.example/b', { signal: AbortSignal.timeout(5000) })
      return new Response('ok')
    })
  },
}
export async function charge(obs: { fetch: typeof fetch }) {
  return obs.fetch('https://pay.example/charge', { method: 'POST' })
}
export const bound = (req: Request, env: { API: { fetch: typeof fetch } }) =>
  withObservability(req, env, async ({ API }, obs) => API.fetch('https://svc.example/x'))
