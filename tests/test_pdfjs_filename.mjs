import assert from "node:assert/strict";
import test from "node:test";

// The browser bundle constructs a matrix at import time. These tests call only
// the pure filename helper; canvas/text rendering is checked in a browser.
globalThis.DOMMatrix = class {};
const { getPdfFilenameFromUrl } = await import("../app/vendor/pdfjs/build/pdf.mjs");

const examples = [
  ["https://example.test/papers/paper.pdf", "paper.pdf"],
  ["https://example.test/paper.PDF?file=other.pdf#third.pdf", "paper.PDF"],
  ["/viewer.html?file=first.pdf&file=second.pdf#third.pdf", "second.pdf"],
  ["/viewer.html?paper.pdf", "paper.pdf"],
  ["/viewer.html#paper.pdf", "paper.pdf"],
  ["/viewer.html#a.pdf/b.pdf", "b.pdf"],
  ["/viewer.html#file=paper.PdF&zoom=100", "paper.PdF"],
  ["/viewer.html#=.pdf", "document.pdf"],
  ["/viewer.html#a.pdf/=.pdf", "document.pdf"],
  ["/viewer.html#paper.pdfx", "document.pdf"],
  ["/viewer.html#paper.pdf.backup", "paper.pdf"],
  ["/viewer.html#paper%20name.pdf", "paper name.pdf"],
  ["/viewer.html#dir%2Fpaper.pdf", "paper.pdf"],
  ["/viewer.html#%E4%B8%AD%E6%96%87.pdf", "中文.pdf"],
  ["/viewer.html#bad%ZZ.pdf", "bad%ZZ.pdf"],
  ["/viewer.html#page=2", "document.pdf"],
];

test("filename extraction preserves path, query, fragment and decoding rules", () => {
  for (const [url, expected] of examples) {
    assert.equal(getPdfFilenameFromUrl(url), expected, url);
  }
  assert.equal(getPdfFilenameFromUrl("/viewer.html#page=2", "fallback.pdf"), "fallback.pdf");
  assert.equal(getPdfFilenameFromUrl("data:application/pdf;base64,AA=="), "document.pdf");
});

test("long URL fragments complete without quadratic backtracking", () => {
  for (const [hash, expected] of [
    ["a".repeat(200_000), "document.pdf"],
    ["a".repeat(200_000) + "/last.pdf", "last.pdf"],
    ["a.pdf/".repeat(20_000) + "last.PDF", "last.PDF"],
  ]) {
    const start = performance.now();
    assert.equal(getPdfFilenameFromUrl("/viewer.html#" + hash), expected);
    assert.ok(performance.now() - start < 1000, "fragment scan should finish within one second");
  }
});
