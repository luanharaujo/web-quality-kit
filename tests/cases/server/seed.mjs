// node seed.mjs <db file> <migrations dir> <n>: the schema from the
// migrations, then n customers, each with one order.
import { DatabaseSync } from 'node:sqlite'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
const [file, migrations, n] = [process.argv[2], process.argv[3], Number(process.argv[4])]
const db = new DatabaseSync(file)
db.exec('DROP TABLE IF EXISTS orders; DROP TABLE IF EXISTS customers; DROP TABLE IF EXISTS plans;')
for (const f of readdirSync(migrations).filter((f) => f.endsWith('.sql')).sort()) db.exec(readFileSync(join(migrations, f), 'utf8'))
for (let i = 1; i <= n; i++) {
  db.prepare('INSERT INTO customers (id, name) VALUES (?, ?)').run(i, `Customer ${i}`)
  db.prepare('INSERT INTO orders (id, customer_id, total) VALUES (?, ?, ?)').run(i, i, i * 10)
}
db.close()
