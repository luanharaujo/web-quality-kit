// Loops that look like queries but are not: the lint must stay quiet.
export async function f(items, text) {
  const out = []
  for (const it of items) {
    out.push(await Promise.all(it.parts.map(async (p) => p.size)))
    const m = /(\d+)/.exec(text)
    out.push(JSON.parse(it.json), Object.keys(it), m)
  }
  return items.map((it) => Array.from(it.tags))
}
