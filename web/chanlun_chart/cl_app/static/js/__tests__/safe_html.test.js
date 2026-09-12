"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const path = require("node:path");

const safeHtmlPath = path.resolve(__dirname, "../safe_html.js");

test("escapeText handles all HTML metacharacters", () => {
  delete require.cache[require.resolve(safeHtmlPath)];
  const SafeHtml = require(safeHtmlPath);

  assert.equal(
    SafeHtml.escapeText(`<img title="x" data-v='y'>&`),
    "&lt;img title=&quot;x&quot; data-v=&#39;y&#39;&gt;&amp;"
  );
});
