export default {
  async fetch(request: Request, env: Env) { return fetch(request) },
} satisfies ExportedHandler<Env>
