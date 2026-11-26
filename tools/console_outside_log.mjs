// node console_outside_log.mjs <root> <skip-file>... -- <dir>...: console
// calls outside the kit's log helper (templates/server/observe.ts), as cases
// for server_observability.py: [{"file", "line", "rule": "console-outside-log", "text"}].
//
// A case = console.<anything> or console["x"] (called or passed as a value,
// as in .catch(console.error)), globalThis.console.x, and console assigned
// to a variable or passed as a value (const { log } = console, new Logger(console)). Logs go through obs.log, which writes one JSON
// line with the request id; a loose console line has no id, no fixed fields
// and can carry personal data. The files listed before "--" (the app's copies
// of observe.ts and d1_queries.ts) are skipped. Test files are skipped.
import fs from 'node:fs'
import path from 'node:path'
import ts from 'typescript'

const EXT = /\.(m|c)?(j|t)sx?$/
const SKIP = new Set(['node_modules', '.git', '.wrangler', 'dist', 'build', 'coverage'])

const args = process.argv.slice(2)
const root = args.shift()
const sep = args.indexOf('--')
const skip = new Set((sep < 0 ? [] : args.slice(0, sep)).map((f) => path.resolve(root, f)))
const dirs = sep < 0 ? args : args.slice(sep + 1)
const cases = []

function files(dir) {
  const out = []
  if (!fs.existsSync(dir)) return out
  if (fs.statSync(dir).isFile()) return EXT.test(dir) ? [dir] : out
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (SKIP.has(e.name)) continue
    const p = path.join(dir, e.name)
    if (e.isDirectory()) out.push(...files(p))
    else if (EXT.test(e.name) && !/\.(test|spec)\./.test(e.name) && !e.name.endsWith('.d.ts')) out.push(p)
  }
  return out
}

function isConsole(node) {
  if (ts.isIdentifier(node)) return node.text === 'console'
  // globalThis.console, self.console, window.console
  return ts.isPropertyAccessExpression(node) && node.name.text === 'console' &&
    ts.isIdentifier(node.expression) && ['globalThis', 'self', 'window'].includes(node.expression.text)
}

for (const dir of dirs) {
  for (const file of files(path.resolve(root, dir))) {
    if (skip.has(path.resolve(file))) continue
    const text = fs.readFileSync(file, 'utf8')
    const kind = file.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
    const src = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind)
    const add = (node, shown) => {
      const { line } = src.getLineAndCharacterOfPosition(node.getStart(src))
      cases.push({ file: path.relative(root, file), line: line + 1, rule: 'console-outside-log',
        text: shown.getText(src).replace(/\s+/g, ' ').slice(0, 120) })
    }
    const visit = (node) => {
      // console.x and console['x'], called or passed as a value (.catch(console.error))
      if ((ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) && isConsole(node.expression)) {
        add(node, ts.isCallExpression(node.parent) && node.parent.expression === node ? node.parent : node)
        return
      }
      // console itself used as a value: const c = console, { logger: console }, new Logger(console)
      if (ts.isIdentifier(node) && node.text === 'console') {
        const p = node.parent
        const isName = (ts.isPropertyAssignment(p) && p.name === node) || ts.isPropertyAccessExpression(p) && p.name === node
        if (!isName && !ts.isTypeOfExpression(p)) add(node, p)
        return
      }
      ts.forEachChild(node, visit)
    }
    visit(src)
  }
}
console.log(JSON.stringify(cases))
