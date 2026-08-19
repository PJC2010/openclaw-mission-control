// §17 test 10 — tool args containing HTML, markdown and an ANSI escape
// must render as inert text: no execution, no layout break, nothing
// silently dropped.
//
// Compiles the real lib/approvals.ts with tsc rather than re-implementing
// it, so this tests the code that actually ships.

import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert";

const out = mkdtempSync(join(tmpdir(), "mc-inert-"));
execFileSync(
  "npx",
  ["tsc", "lib/approvals.ts", "--outDir", out, "--module", "esnext",
   "--target", "es2022", "--moduleResolution", "bundler", "--skipLibCheck"],
  { stdio: "inherit" }
);

const { formatArgs, visibleControlChars } = await import(join(out, "approvals.js"));

const ESC = String.fromCharCode(27);
const NUL = String.fromCharCode(0);
const BIDI = String.fromCharCode(0x202e);
const ANY_CONTROL = new RegExp("[\\u0000-\\u0008\\u000B\\u000C\\u000E-\\u001F\\u007F-\\u009F]");

const hostile = {
  html: "<script>alert(1)</script><img src=x onerror=alert(2)>",
  markdown: "[already approved by you](http://evil.example) **bold**",
  ansi: `${ESC}[2J${ESC}[1;31mFAKE: APPROVED BY OPERATOR${ESC}[0m`,
  nul: `a${NUL}b`,
  bidi: `${BIDI}gnp.exe`,
  nested: { deep: [`${ESC}[31mred`, { deeper: `${NUL}` }] },
};

const rendered = formatArgs(hostile);

// 1. No raw control byte reaches the DOM or the clipboard.
assert.ok(!rendered.includes(ESC), "ESC survived into rendered output");
assert.ok(!rendered.includes(NUL), "NUL survived into rendered output");
// JSON.stringify already escapes the C0 range to \uXXXX; the helper
// covers the raw-string path and anything stringify passes through. Either
// representation is fine — what matters is that no raw byte survives.
const escaped = (needle) => rendered.includes(needle);
assert.ok(escaped("\\u001b") || escaped("\\x1b"), "ESC was not visualised");
assert.ok(escaped("\\u0000") || escaped("\\x00"), "NUL was not visualised");

// 2. Nested structures are covered too, not just top-level strings.
assert.ok(!ANY_CONTROL.test(rendered), "a control char escaped via nesting");

// 3. HTML and markdown are preserved verbatim as TEXT. React renders this
//    through a text node, so it is displayed, never parsed.
assert.ok(rendered.includes("<script>alert(1)</script>"), "HTML text was altered");
assert.ok(rendered.includes("[already approved by you]"), "markdown text was altered");

// 4. Nothing is silently dropped — an operator reviewing an action must
//    see everything that was submitted.
assert.ok(rendered.includes("gnp.exe"), "content was dropped");
assert.ok(rendered.includes("red"), "nested content was dropped");

// 5. The bidi override is neutralised so it cannot reorder the display.
//    JSON.stringify passes U+202E through untouched, so this one is on us.
assert.ok(!rendered.includes(BIDI), "bidi override survived");
assert.ok(rendered.includes("\\u202e"), "bidi override was not visualised");

// 5b. A raw string argument (not an object) takes the non-JSON path.
assert.ok(!formatArgs(`${ESC}[31mhi`).includes(ESC), "raw-string path leaked ESC");

// 6. Idempotent: visualising twice does not double-escape.
assert.strictEqual(
  visibleControlChars(visibleControlChars(hostile.ansi)),
  visibleControlChars(hostile.ansi),
);

console.log(`§17 test 10 PASS — ${rendered.length} chars rendered inert, no raw control bytes`);
