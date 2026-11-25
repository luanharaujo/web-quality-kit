// Where the interaction checks write their results (JSON lines, one file per
// process) and the switches that drive them. See index.mjs.
import fs from 'node:fs';
import path from 'node:path';

export function settings() {
  return {
    mode: process.env.KIT_INTERACTION_MODE || 'slow',
    enforce: process.env.KIT_INTERACTION_ENFORCE || 'throw',
    dir: process.env.KIT_INTERACTION_REPORT_DIR || path.resolve('test-results/kit-interaction'),
  };
}

export function report(entry) {
  const { dir } = settings();
  fs.mkdirSync(dir, { recursive: true });
  fs.appendFileSync(path.join(dir, `report-${process.pid}.jsonl`), JSON.stringify(entry) + '\n');
}
