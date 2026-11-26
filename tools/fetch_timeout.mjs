// node fetch_timeout.mjs <root> [--max-ms N] [--internal host]... -- <dir|file>...
// fetch() calls to an external host without a time limit, as cases
// [{"file", "line", "rule", "text"}]:
//   - "fetch-no-timeout": no options, or options without `signal`;
//   - "fetch-timeout-too-long": signal: AbortSignal.timeout(N) with N > max.
// Not flagged: a binding or object method (env.API.fetch, ctx.fetch...), a
// relative URL ("/x", `/x/${id}`), a URL on an --internal host, and a signal
// that is any other expression (an AbortController's signal, a variable):
// the time limit is then set elsewhere. A URL held in a variable counts as
// external: the code cannot show where it goes. fetch(new Request(url, init))
// is read as fetch(url, init) (a signal in either init counts), and a Worker
// forwarding its own incoming request (the first parameter of a `fetch`
// handler) is not an outbound call to a third party.
import fs from 'node:fs'
import path from 'node:path'
import ts from 'typescript'

const EXT = /\.(m|c)?(j|t)sx?$/
const SKIP = new Set(['node_modules', '.git', '.wrangler', 'dist', 'build', 'coverage'])

const argv = process.argv.slice(2)
const root = argv.shift()
let maxMs = 10000
const internal = new Set()
const targets = []
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === '--max-ms') maxMs = Number(argv[++i])
  else if (argv[i] === '--internal') internal.add(argv[++i].toLowerCase())
  else if (argv[i] !== '--') targets.push(argv[i])
}

function files(p) {
  if (!fs.existsSync(p)) { console.error(`source not found: ${p}`); process.exit(2) }
  if (fs.statSync(p).isFile()) return EXT.test(p) ? [p] : []
  const out = []
  for (const e of fs.readdirSync(p, { withFileTypes: true })) {
    if (SKIP.has(e.name)) continue
    const q = path.join(p, e.name)
    if (e.isDirectory()) out.push(...files(q))
    else if (EXT.test(e.name) && !/\.(test|spec)\./.test(e.name) && !e.name.endsWith('.d.ts')) out.push(q)
  }
  return out
}

// The text a URL argument starts with, when the code shows it.
function urlStart(node) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text
  // `${API}/x` starts with an expression: where it goes is not in the code.
  if (ts.isTemplateExpression(node)) return node.head.text === '' ? null : node.head.text
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) return urlStart(node.left)
  if (ts.isNewExpression(node) && node.expression.getText() === 'URL' && node.arguments?.length) {
    // new URL('/x', base) goes where base points; new URL('https://...') is absolute.
    const first = urlStart(node.arguments[0])
    if (first !== null && /^https?:\/\//i.test(first)) return first
    return node.arguments.length > 1 ? urlStart(node.arguments[1]) : first
  }
  return null
}

const isNewRequest = (n) => n && ts.isNewExpression(n) && n.expression.getText() === 'Request' && n.arguments?.length

// `request` in `async fetch(request, env)` of the default export: the incoming
// request of a Worker handler. A one-parameter fetch(url) wrapper (a client's
// api.fetch) is not a handler, so its calls stay under the rule.
function isIncomingRequest(node) {
  if (!ts.isIdentifier(node)) return false
  for (let p = node.parent; p; p = p.parent) {
    if (!ts.isFunctionLike(p)) continue
    const first = p.parameters?.[0]
    if (!first || !ts.isIdentifier(first.name) || first.name.text !== node.text) continue
    const owner = ts.isMethodDeclaration(p) ? p.name
      : ts.isPropertyAssignment(p.parent) ? p.parent.name
      : ts.isFunctionDeclaration(p) ? p.name : null
    if (!owner || owner.getText() !== 'fetch' || p.parameters.length < 2) return false
    const obj = ts.isMethodDeclaration(p) ? p.parent : ts.isPropertyAssignment(p.parent) ? p.parent.parent : null
    return !!obj && ts.isObjectLiteralExpression(obj) && isDefaultExport(obj)
  }
  return false
}

// The object is what `export default` exports, through `satisfies`, `as`,
// parentheses, or a variable exported by name (const worker = {...}; export default worker).
const unwrap = (n) => {
  while (n && (ts.isParenthesizedExpression(n) || ts.isAsExpression(n) || ts.isSatisfiesExpression?.(n) || ts.isTypeAssertionExpression(n))) n = n.expression
  return n
}
function isDefaultExport(obj) {
  let n = obj
  while (n.parent && (ts.isParenthesizedExpression(n.parent) || ts.isAsExpression(n.parent) ||
         ts.isSatisfiesExpression?.(n.parent) || ts.isTypeAssertionExpression(n.parent))) n = n.parent
  if (n.parent && ts.isExportAssignment(n.parent) && !n.parent.isExportEquals) return true
  if (n.parent && ts.isVariableDeclaration(n.parent) && ts.isIdentifier(n.parent.name)) {
    const name = n.parent.name.text
    return n.getSourceFile().statements.some((st) => ts.isExportAssignment(st) && !st.isExportEquals &&
      ts.isIdentifier(unwrap(st.expression)) && unwrap(st.expression).text === name)
  }
  return false
}

// A number the code shows: a literal, + - * / of numbers, parentheses, or a
// const in the same file set to one (60 * 1000, TIMEOUT_MS). Anything else: null.
function constValue(n, sf, seen = new Set()) {
  n = unwrap(n)
  if (ts.isNumericLiteral(n)) return Number(n.text)
  if (ts.isBinaryExpression(n)) {
    const a = constValue(n.left, sf, seen), b = constValue(n.right, sf, seen)
    if (a === null || b === null) return null
    switch (n.operatorToken.kind) {
      case ts.SyntaxKind.AsteriskToken: return a * b
      case ts.SyntaxKind.PlusToken: return a + b
      case ts.SyntaxKind.MinusToken: return a - b
      case ts.SyntaxKind.SlashToken: return b ? a / b : null
      default: return null
    }
  }
  if (ts.isIdentifier(n) && !seen.has(n.text)) {
    seen.add(n.text)
    let found = null
    const look = (x) => {
      if (found !== null) return
      if (ts.isVariableDeclaration(x) && ts.isIdentifier(x.name) && x.name.text === n.text && x.initializer &&
          ts.isVariableDeclarationList(x.parent) && (x.parent.flags & ts.NodeFlags.Const)) found = constValue(x.initializer, sf, seen)
      else ts.forEachChild(x, look)
    }
    look(sf)
    return found
  }
  return null
}

function isInternal(start) {
  if (start === null) return false
  if (start.startsWith('//')) start = 'https:' + start // protocol-relative: another host
  if (start === '' || start.startsWith('/') || start.startsWith('.')) return true
  const m = /^https?:\/\/([^/:?#]+)/i.exec(start)
  if (!m) return false
  const host = m[1].toLowerCase()
  return internal.has(host) || host === 'localhost' || host === '127.0.0.1'
}

// The signal the options end up with: the last explicit `signal` wins over an
// earlier spread; a spread after it (or with none) may carry one: 'unknown'.
function signalOf(opts) {
  if (!opts || !ts.isObjectLiteralExpression(opts)) return opts ? 'unknown' : null
  let sig = null
  for (const p of opts.properties) {
    if (ts.isSpreadAssignment(p)) { sig = 'unknown'; continue }
    const name = p.name && p.name.getText()
    if (name !== 'signal') continue
    const v = ts.isPropertyAssignment(p) ? p.initializer : null
    // signal: undefined / null sets no time limit.
    if (v && (v.kind === ts.SyntaxKind.NullKeyword || (ts.isIdentifier(v) && v.text === 'undefined'))) sig = null
    else sig = v || 'unknown'
  }
  return sig
}

const cases = []
for (const t of targets) {
  for (const file of files(path.resolve(root, t))) {
    const sf = ts.createSourceFile(file, fs.readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true)
    const add = (node, rule) => {
      const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf))
      cases.push({ file: path.relative(root, file), line: line + 1, rule, text: sf.text.split('\n')[line].trim().slice(0, 160) })
    }
    const visit = (node) => {
      if (ts.isCallExpression(node)) {
        const callee = node.expression
        const plain = (ts.isIdentifier(callee) && callee.text === 'fetch') ||
          (ts.isPropertyAccessExpression(callee) && callee.name.text === 'fetch' &&
           ts.isIdentifier(callee.expression) && ['globalThis', 'self', 'window'].includes(callee.expression.text))
        let [target, init] = node.arguments
        let sig = null
        if (isNewRequest(target)) {
          // fetch(new Request(url, init), init2): the URL is url, a signal may be in either init.
          sig = signalOf(target.arguments[1])
          target = target.arguments[0]
        }
        if (plain && target && !isIncomingRequest(target) && !isInternal(urlStart(target))) {
          const outer = signalOf(init)
          if (outer !== null) sig = outer
          if (sig === null) add(node, 'fetch-no-timeout')
          else if (sig !== 'unknown' && ts.isCallExpression(sig) && sig.expression.getText() === 'AbortSignal.timeout') {
            const ms = sig.arguments[0] ? constValue(sig.arguments[0], sf) : null
            if (ms !== null && ms > maxMs) add(node, 'fetch-timeout-too-long')
          }
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(sf)
  }
}
cases.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line)
process.stdout.write(JSON.stringify(cases) + '\n')
