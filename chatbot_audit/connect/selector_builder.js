// Builds stable CSS selectors for clicked elements (used by `audit connect`).
// Plain browser JavaScript, no dependencies. Safe to inject more than once.
(function () {
  "use strict";
  if (window.__auditBuildSelector) return;

  var TEST_ATTRIBUTES = ["data-testid", "data-test", "data-qa"];
  var GENERATED_PREFIX = /^(css|sc|jsx|emotion|svelte|styled)-/i;
  var PER_ITEM_ATTRIBUTE = /(^|-)(id|index|key|time|timestamp|uuid|ts)$/i;

  function cssString(value) {
    return '"' + String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\a ") + '"';
  }

  function cssIdent(value) {
    return window.CSS && CSS.escape ? CSS.escape(value) : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  // A token "looks generated" when it carries a build hash: css-/sc- prefixes, long digit runs,
  // letter+digit soup in a 5+ character segment, or short mixed-case hashes like "bZkfAO".
  function looksGenerated(token) {
    if (!token || GENERATED_PREFIX.test(token) || /^_/.test(token) || /\d{4,}/.test(token)) return true;
    return token.split(/[-_]/).some(function (part) {
      if (part.length >= 5 && /\d/.test(part) && /[a-z]/i.test(part)) return true;
      return part.length >= 5 && part.length <= 8 && /^[a-z]/.test(part) && /[A-Z].*[A-Z]/.test(part.slice(1));
    });
  }

  function stableId(id) {
    return !!id && id.length <= 40 && !/\d{3,}/.test(id) && !looksGenerated(id);
  }

  function stableClasses(element) {
    return Array.prototype.filter.call(element.classList || [], function (name) {
      return !looksGenerated(name);
    });
  }

  function shortValue(value) {
    return value.length <= 40 && !looksGenerated(value) && !/\d{3,}/.test(value);
  }

  function attributeSelector(name, value) {
    return value === "" ? "[" + name + "]" : "[" + name + "=" + cssString(value) + "]";
  }

  function dataAttributes(element, forSimilar) {
    var result = [];
    Array.prototype.forEach.call(element.attributes, function (attribute) {
      var name = attribute.name;
      if (name.indexOf("data-") !== 0 || TEST_ATTRIBUTES.indexOf(name) !== -1) return;
      if (forSimilar && (PER_ITEM_ATTRIBUTE.test(name) || /\d/.test(attribute.value))) return;
      if (attribute.value === "" || shortValue(attribute.value)) result.push(attributeSelector(name, attribute.value));
    });
    return result;
  }

  function matchCount(root, selector) {
    try {
      return root.querySelectorAll(selector).length;
    } catch (error) {
      return -1;
    }
  }

  function nthOfTypePath(element) {
    var parts = [];
    var node = element;
    for (var depth = 0; node && node.nodeType === 1 && depth < 5; depth++) {
      var tag = node.tagName.toLowerCase();
      if (stableId(node.id)) {
        parts.unshift("#" + cssIdent(node.id));
        break;
      }
      var index = 1;
      var sibling = node.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === node.tagName) index++;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(tag + ":nth-of-type(" + index + ")");
      if (tag === "body") break;
      node = node.parentElement;
    }
    return parts.join(" > ");
  }

  function uniqueSelector(element) {
    var root = element.ownerDocument;
    var tag = element.tagName.toLowerCase();
    var classes = stableClasses(element);
    var tiers = [
      stableId(element.id) ? ["#" + cssIdent(element.id)] : [],
      TEST_ATTRIBUTES.filter(function (name) { return element.hasAttribute(name); }).map(function (name) {
        return attributeSelector(name, element.getAttribute(name));
      }),
      dataAttributes(element, false).reduce(function (all, selector) {
        return all.concat([selector, tag + selector]);
      }, []),
      ["name", "aria-label", "placeholder"].filter(function (name) {
        return element.getAttribute(name);
      }).reduce(function (all, name) {
        var selector = attributeSelector(name, element.getAttribute(name));
        return all.concat([selector, tag + selector]);
      }, []),
      classes.map(function (name) { return tag + "." + cssIdent(name); }).concat(
        classes.length > 1 ? [tag + "." + classes.map(cssIdent).join(".")] : []
      ),
    ];
    for (var i = 0; i < tiers.length; i++) {
      var unique = tiers[i].filter(function (selector) { return matchCount(root, selector) === 1; });
      if (unique.length) {
        return unique.sort(function (a, b) { return a.length - b.length; })[0];
      }
    }
    return nthOfTypePath(element);
  }

  // No positional parts: the selector should match every element of the same kind.
  function similarHooks(element) {
    var tag = element.tagName.toLowerCase();
    var data = dataAttributes(element, true);
    if (data.length) return tag + data.join("");
    var classes = stableClasses(element);
    if (classes.length) return tag + "." + classes.map(cssIdent).join(".");
    return null;
  }

  function similarSelector(element) {
    var node = element;
    for (var depth = 0; node && node.nodeType === 1 && depth <= 3; depth++) {
      var hooks = similarHooks(node);
      if (hooks) return hooks;
      node = node.parentElement;
    }
    return element.tagName.toLowerCase();
  }

  window.__auditBuildSelector = function (element, mode) {
    if (!element || element.nodeType !== 1) return null;
    return mode === "all_similar" ? similarSelector(element) : uniqueSelector(element);
  };

  // Click-to-pick: while window.__auditPickMode is set, clicks are captured instead of acting.
  if (typeof window.__auditPickMode === "undefined") window.__auditPickMode = null;
  window.addEventListener(
    "click",
    function (event) {
      var mode = window.__auditPickMode;
      if (!mode) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      var target = event.target && event.target.nodeType === 1 ? event.target : event.target.parentElement;
      if (typeof window.__auditPick === "function") {
        window.__auditPick({
          selector: window.__auditBuildSelector(target, mode),
          text: (target.innerText || target.value || "").trim().slice(0, 500),
          mode: mode,
        });
      }
    },
    true
  );
})();
