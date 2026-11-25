// node query_in_loop.mjs <root> <dir>...: database queries inside a loop
// (the usual shape of an N+1), as warning cases for web_health.py:
// [{"file", "line", "rule": "query-in-loop", "text"}].
//
// A query = a call to prepare/query/execute/first/all/run/raw/batch/findMany/
// findUnique/findFirst on anything (not Promise, Object, Array...) inside the
// body of for/for-of/for-in/while/do, or inside the callback of
// map/forEach/flatMap/reduce/filter/some/every. Only the loop body counts: the
// query that produces the list being looped over is fine.
import fs from 'node:fs'
import path from 'node:path'
import ts from 'typescript'

const QUERY = new Set(['prepare', 'query', 'execute', 'first', 'all', 'run', 'raw', 'batch',
  'findMany', 'findUnique', 'findFirst', 'findOne', 'exec'])
const ITER = new Set(['map', 'forEach', 'flatMap', 'reduce', 'filter', 'some', 'every'])
const EXT = /\.(m|c)?(j|t)sx?$/
const SKIP = new Set(['node_modules', '.git', '.wrangler', 'dist', 'build', 'coverage'])

const [root, ...dirs] = process.argv.slice(2)
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

const NOT_DB = new Set(['Promise', 'Object', 'Array', 'JSON', 'Math', 'console', 'Reflect'])

function isQueryCall(node) {
  if (!ts.isCallExpression(node) || !ts.isPropertyAccessExpression(node.expression)) return false
  const target = node.expression.expression
  if (ts.isIdentifier(target) && NOT_DB.has(target.text)) return false // Promise.all, ...
  if (ts.isRegularExpressionLiteral(target)) return false // /x/.exec(...)
  return QUERY.has(node.expression.name.text)
}

// The first query call in a loop body (a prepare(...) built per row for a
// batch counts too: the batch still grows with the rows).
function queryIn(body) {
  let hit = null
  const visit = (node) => {
    if (hit) return
    // A nested function that is not called here (a handler defined in a loop) is not the loop's work.
    if (node !== body && (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node))) return
    if (isQueryCall(node)) { hit = node; return }
    ts.forEachChild(node, visit)
  }
  visit(body)
  return hit
}

function report(sf, file, call) {
  const { line } = sf.getLineAndCharacterOfPosition(call.getStart(sf))
  const text = sf.text.split('\n')[line].trim().slice(0, 160)
  cases.push({ file: path.relative(root, file), line: line + 1, rule: 'query-in-loop', text })
}

for (const dir of dirs) {
  for (const file of files(path.resolve(root, dir))) {
    const src = fs.readFileSync(file, 'utf8')
    const sf = ts.createSourceFile(file, src, ts.ScriptTarget.Latest, true)
    const seen = new Set()
    const flag = (call) => {
      if (call && !seen.has(call.pos)) { seen.add(call.pos); report(sf, file, call) }
    }
    const visit = (node) => {
      if (ts.isForStatement(node) || ts.isForOfStatement(node) || ts.isForInStatement(node) ||
          ts.isWhileStatement(node) || ts.isDoStatement(node)) {
        flag(queryIn(node.statement))
      } else if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) &&
                 ITER.has(node.expression.name.text)) {
        const fn = node.arguments[0]
        if (fn && (ts.isArrowFunction(fn) || ts.isFunctionExpression(fn))) flag(queryIn(fn.body))
      }
      ts.forEachChild(node, visit)
    }
    visit(sf)
  }
}
cases.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line)
process.stdout.write(JSON.stringify(cases) + '\n')
