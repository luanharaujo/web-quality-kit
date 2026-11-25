#!/usr/bin/env node
// Code points of WOFF2 fonts, read from their cmap table (no dependency:
// Node's own Brotli). Prints {"file": {"ranges": [[start, end], ...]}} or
// {"file": {"error": "..."}} per font, as JSON.
//
//   node font_cmap.mjs a.woff2 b.woff2 > cmap.json
import { readFileSync } from "node:fs";
import { brotliDecompressSync } from "node:zlib";
import { pathToFileURL } from "node:url";
import { realpathSync } from "node:fs";

// WOFF2 known-table tags; the 6-bit index in each directory entry points here.
const KNOWN = ["cmap", "head", "hhea", "hmtx", "maxp", "name", "OS/2", "post", "cvt ", "fpgm", "glyf", "loca",
  "prep", "CFF ", "VORG", "EBDT", "EBLC", "gasp", "hdmx", "kern", "LTSH", "PCLT", "VDMX", "vhea", "vmtx", "BASE",
  "GDEF", "GPOS", "GSUB", "EBSC", "JSTF", "MATH", "CBDT", "CBLC", "COLR", "CPAL", "SVG ", "sbix", "acnt", "avar",
  "bdat", "bloc", "bsln", "cvar", "fdsc", "feat", "fmtx", "fvar", "gvar", "hsty", "just", "lcar", "mort", "morx",
  "opbd", "prop", "trak", "Zapf", "Silf", "Glat", "Gloc", "Feat", "Sill"];

function base128(buf, pos) {
  let value = 0;
  for (let i = 0; i < 5; i++) {
    const b = buf[pos.at++];
    if (i === 0 && b === 0x80) throw new Error("bad UIntBase128");
    value = value * 128 + (b & 0x7f);
    if (!(b & 0x80)) return value;
  }
  throw new Error("UIntBase128 too long");
}

export function cmapTable(buf) {
  if (buf.toString("latin1", 0, 4) !== "wOF2") throw new Error("not a WOFF2 file");
  if (buf.toString("latin1", 4, 8) === "ttcf") throw new Error("font collections are not supported");
  const numTables = buf.readUInt16BE(12);
  const compressed = buf.readUInt32BE(20);
  const pos = { at: 48 };
  const tables = [];
  for (let i = 0; i < numTables; i++) {
    const flags = buf[pos.at++];
    let tag = KNOWN[flags & 0x3f];
    if ((flags & 0x3f) === 63) {
      tag = buf.toString("latin1", pos.at, pos.at + 4);
      pos.at += 4;
    }
    const version = flags >> 6;
    const origLength = base128(buf, pos);
    let length = origLength;
    const transformed = tag === "glyf" || tag === "loca" ? version === 0 : version !== 0;
    if (transformed) length = base128(buf, pos);
    tables.push({ tag, length });
  }
  const data = brotliDecompressSync(buf.subarray(pos.at, pos.at + compressed));
  let offset = 0;
  for (const t of tables) {
    if (t.tag === "cmap") return data.subarray(offset, offset + t.length);
    offset += t.length;
  }
  throw new Error("no cmap table");
}

// Unicode ranges [start, end] mapped by the best Unicode subtable.
export function codeRanges(cmap) {
  const n = cmap.readUInt16BE(2);
  const subtables = [];
  for (let i = 0; i < n; i++) {
    const platform = cmap.readUInt16BE(4 + i * 8);
    const encoding = cmap.readUInt16BE(6 + i * 8);
    const offset = cmap.readUInt32BE(8 + i * 8);
    subtables.push({ platform, encoding, offset, format: cmap.readUInt16BE(offset) });
  }
  const pick = subtables.find((s) => s.format === 12) || subtables.find((s) => s.format === 4);
  if (!pick) throw new Error("no Unicode cmap subtable (format 4 or 12)");
  const ranges = [];
  const o = pick.offset;
  if (pick.format === 12) {
    const groups = cmap.readUInt32BE(o + 12);
    for (let i = 0; i < groups; i++) {
      ranges.push([cmap.readUInt32BE(o + 16 + i * 12), cmap.readUInt32BE(o + 20 + i * 12)]);
    }
  } else {
    const segs = cmap.readUInt16BE(o + 6) / 2;
    for (let i = 0; i < segs; i++) {
      const end = cmap.readUInt16BE(o + 14 + i * 2);
      const start = cmap.readUInt16BE(o + 16 + segs * 2 + i * 2);
      if (start === 0xffff) continue;
      ranges.push([start, end]);
    }
  }
  return ranges;
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  const out = {};
  for (const file of process.argv.slice(2)) {
    try {
      out[file] = { ranges: codeRanges(cmapTable(readFileSync(file))) };
    } catch (e) {
      out[file] = { error: e.message };
    }
  }
  process.stdout.write(JSON.stringify(out));
}
