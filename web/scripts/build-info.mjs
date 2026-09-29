// Stamp the build with a fingerprint of the source it was built from.
//
// api/static/ is committed ([[decisions]] 056), so it can drift from web/src:
// change a component, forget `npm run build`, commit, and the VM serves the old
// page. tests/test_web_build.py recomputes this same fingerprint from the
// source and fails if it differs from the one written here.
//
// KEEP IN STEP WITH tests/test_web_build.py -- same files, same order, same
// normalisation. Line endings are normalised to \n because git turns them into
// \r\n on the Windows laptop but not on the Linux VM; without that the test
// would fail on one of the two machines forever.
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const WEB = fileURLToPath(new URL("..", import.meta.url));
const TOP_LEVEL = ["package.json", "package-lock.json", "index.html", "vite.config.ts", "tsconfig.json"];
const IS_TEST = /\.test\.(ts|tsx)$/;

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return e.name === "test" ? [] : walk(p);
    return IS_TEST.test(e.name) ? [] : [p];
  });
}

export function sourceHash() {
  const files = [...TOP_LEVEL.map((f) => join(WEB, f)), ...walk(join(WEB, "src"))]
    .map((p) => relative(WEB, p).split(sep).join("/"))
    .sort();
  const h = createHash("sha256");
  for (const rel of files) {
    const text = readFileSync(join(WEB, rel)).toString("utf8").replace(/\r\n/g, "\n");
    h.update(rel + "\n" + text + "\n");
  }
  return h.digest("hex");
}

const out = join(WEB, "..", "api", "static", "build-info.json");
writeFileSync(out, JSON.stringify({ source_hash: sourceHash() }, null, 2) + "\n");
console.log(`build-info: ${out}`);
