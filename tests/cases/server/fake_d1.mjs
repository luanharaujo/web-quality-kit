// The part of the D1 API the example servers use, on node:sqlite, so the
// kit's tests run the real query counter without wrangler.
import { DatabaseSync } from 'node:sqlite'

class Statement {
  constructor(db, sql, args = []) { this.db = db; this.sql = sql; this.args = args }
  bind(...args) { return new Statement(this.db, this.sql, args) }
  async first(col) {
    const row = this.db.prepare(this.sql).get(...this.args) ?? null
    return row && col ? row[col] : row
  }
  async all() { return { results: this.db.prepare(this.sql).all(...this.args), success: true } }
  async run() { this.db.prepare(this.sql).run(...this.args); return { success: true } }
  async raw() { return this.db.prepare(this.sql).all(...this.args).map((r) => Object.values(r)) }
}

export class FakeD1 {
  constructor(file) { this.db = new DatabaseSync(file) }
  prepare(sql) { return new Statement(this.db, sql) }
  async batch(stmts) {
    for (const s of stmts) if (!(s instanceof Statement)) throw new Error('batch got a non-statement')
    return Promise.all(stmts.map((s) => s.all()))
  }
  async exec(sql) { this.db.exec(sql); return { count: 1 } }
}
