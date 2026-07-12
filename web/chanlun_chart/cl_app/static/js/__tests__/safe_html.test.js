"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const path = require("node:path");

const safeHtmlPath = path.resolve(__dirname, "../safe_html.js");

test("renderMarkdown sends marked output through strict DOMPurify allowlist", () => {
  let sanitizeArgs = null;
  global.marked = {
    parse(value) {
      return `<p>${value}</p><img src=x onerror=alert(1)>`;
    },
  };
  global.DOMPurify = {
    sanitize(html, options) {
      sanitizeArgs = { html, options };
      return "<p>clean</p>";
    },
  };

  delete require.cache[require.resolve(safeHtmlPath)];
  const SafeHtml = require(safeHtmlPath);
  const result = SafeHtml.renderMarkdown("hello");

  assert.equal(result, "<p>clean</p>");
  assert.match(sanitizeArgs.html, /onerror/);
  assert.deepEqual(sanitizeArgs.options.FORBID_TAGS, ["style", "script", "iframe", "object", "embed", "form"]);
  assert.deepEqual(sanitizeArgs.options.FORBID_ATTR, ["style"]);
  assert.equal(sanitizeArgs.options.ALLOW_DATA_ATTR, false);
});

test("escapeText handles all HTML metacharacters", () => {
  global.marked = { parse: (value) => value };
  global.DOMPurify = { sanitize: (value) => value };
  delete require.cache[require.resolve(safeHtmlPath)];
  const SafeHtml = require(safeHtmlPath);

  assert.equal(
    SafeHtml.escapeText(`<img title="x" data-v='y'>&`),
    "&lt;img title=&quot;x&quot; data-v=&#39;y&#39;&gt;&amp;"
  );
});
