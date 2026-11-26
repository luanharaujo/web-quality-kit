export default class extends WorkerEntrypoint {
  async fetch(request: Request) { return fetch(request) }
}
