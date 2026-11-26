// Small demo app for the kit's own interaction tests: one good page and one
// bad page per rule. No dependencies. PORT env, default 4791.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const records = new Map(); // page -> count
const answers = new Map(); // page + key -> answer

function send(res, status, body, type = 'application/json') {
  res.writeHead(status, { 'content-type': type, 'cache-control': 'no-store' });
  res.end(typeof body === 'string' ? body : JSON.stringify(body));
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    const body = Buffer.concat(chunks);
    let m = url.pathname.match(/^\/api\/([a-z-]+)\/bookings$/);
    if (m && req.method === 'POST') {
      const page = m[1];
      const key = req.headers['idempotency-key'];
      // bad-double ignores the key: every request is a new booking
      if (key && page !== 'bad-double' && answers.has(page + key)) return send(res, 201, answers.get(page + key));
      const n = (records.get(page) || 0) + 1;
      records.set(page, n);
      const answer = { id: n, slot: 'tue-18h' };
      if (key) answers.set(page + key, answer);
      return send(res, 201, answer);
    }
    if (/^\/api\/[a-z-]+\/(items|search|status)$/.test(url.pathname) && req.method === 'GET') {
      return send(res, 200, ['Handstand basics', 'Partner balance', 'Flexibility']);
    }
    m = url.pathname.match(/^\/api\/([a-z-]+)\/uploads$/);
    if (m && req.method === 'POST') return send(res, 201, { ok: true, bytes: body.length });
    if (url.pathname === '/api/test/count') return send(res, 200, { n: records.get(url.searchParams.get('page')) || 0 });
    const file = path.join(HERE, 'pages', path.basename(url.pathname) || 'index.html');
    if (fs.existsSync(file)) return send(res, 200, fs.readFileSync(file, 'utf8'), 'text/html; charset=utf-8');
    send(res, 404, { error: 'not found' });
  });
});

server.listen(Number(process.env.PORT || 4791), '127.0.0.1');
