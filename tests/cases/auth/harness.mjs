// Runs an example Worker (src/worker.js of the folder in argv[2]) as a local
// HTTP server with fake bindings: a D1 that knows one number, an in-memory
// ratelimits binding and the log helper. Stands in for `wrangler dev`.
import http from "node:http";
import path from "node:path";
import { pathToFileURL } from "node:url";

const dir = path.resolve(process.argv[2]);
const port = Number(process.argv[3]);
const { handle } = await import(pathToFileURL(path.join(dir, "src/worker.js")).href);
const known = new Set(["5561900000001"]);
const hits = new Map();
const env = {
  TURNSTILE_SECRET: process.env.TURNSTILE_SECRET,
  TURNSTILE_VERIFY_URL: process.env.TURNSTILE_VERIFY_URL,
  DB: {
    prepare: () => ({
      bind: (...args) => ({
        first: async () => (known.has(args[0]) ? { 1: 1 } : null),
        run: async () => ({ success: true }),
      }),
      first: async () => ({ n: 0 }),
    }),
  },
  RATELIMIT: {
    limit: async ({ key }) => {
      const n = (hits.get(key) ?? 0) + 1;
      hits.set(key, n);
      return { success: n <= Number(process.env.EXAMPLE_LIMIT ?? 1000) };
    },
  },
  obs: { log: Object.fromEntries(["info", "warn", "error"].map((l) => [l, (event, f) => process.stdout.write(JSON.stringify({ level: l, event, ...f }) + "\n")])) },
};
http.createServer(async (req, res) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const headers = Object.fromEntries(Object.entries(req.headers).map(([k, v]) => [k, String(v)]));
  const request = new Request(`http://localhost${req.url}`, { method: req.method, headers, body: ["GET", "HEAD"].includes(req.method) ? undefined : Buffer.concat(chunks) });
  const r = await handle(request, env);
  res.writeHead(r.status, Object.fromEntries(r.headers));
  res.end(Buffer.from(await r.arrayBuffer()));
}).listen(port, "127.0.0.1");
