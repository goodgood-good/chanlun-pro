(function (root, factory) {
  var api = factory(root);
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.SafeHtml = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  var PURIFY_OPTIONS = Object.freeze({
    ALLOWED_TAGS: [
      "p", "br", "strong", "em", "del", "code", "pre", "blockquote",
      "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "a",
      "table", "thead", "tbody", "tr", "th", "td", "hr"
    ],
    ALLOWED_ATTR: ["href", "title"],
    FORBID_TAGS: ["style", "script", "iframe", "object", "embed", "form"],
    FORBID_ATTR: ["style"],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|[^a-z]|[a-z+.-]+(?:[^a-z+.-:]|$))/i,
    RETURN_TRUSTED_TYPE: false
  });

  function escapeText(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return {
        "&": "&amp;", "<": "&lt;", ">": "&gt;",
        '"': "&quot;", "'": "&#39;"
      }[ch];
    });
  }

  function renderMarkdown(markdown) {
    if (!root.marked || typeof root.marked.parse !== "function") {
      throw new Error("marked is required before SafeHtml.renderMarkdown");
    }
    if (!root.DOMPurify || typeof root.DOMPurify.sanitize !== "function") {
      throw new Error("DOMPurify is required before SafeHtml.renderMarkdown");
    }
    var rendered = root.marked.parse(String(markdown == null ? "" : markdown));
    return root.DOMPurify.sanitize(rendered, PURIFY_OPTIONS);
  }

  return {
    escapeText: escapeText,
    renderMarkdown: renderMarkdown,
    purifyOptions: PURIFY_OPTIONS
  };
});
