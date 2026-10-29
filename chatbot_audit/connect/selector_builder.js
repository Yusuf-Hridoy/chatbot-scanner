// Builds stable CSS selectors for clicked elements (used by `audit connect`).
// Plain browser JavaScript, no dependencies. Safe to inject more than once.
(function () {
  "use strict";
  if (window.__auditBuildSelector) return;

  var TEST_ATTRIBUTES = ["data-testid", "data-test", "data-qa"];
  var GENERATED_PREFIX = /^(css|sc|jsx|emotion|svelte|styled)-/i;
  var PER_ITEM_ATTRIBUTE = /(^|-)(id|index|key|time|timestamp|uuid|ts)$/i;
  var NOT_TEXT_INPUTS = /^(hidden|password|checkbox|radio|submit|button|reset|file|image|range|color)$/i;
  var TEXTBOX_CANDIDATES = "input, textarea, [contenteditable], [role='textbox']";
  var CONTROLS = "button, input, select, textarea, [role='button']";

  // <html> and <body> are the page itself, never a chat element.
  function isPageRoot(element) {
    var tag = element.tagName.toLowerCase();
    return tag === "html" || tag === "body";
  }

  // A box a customer can type into: a text input, a textarea, a contenteditable element or role="textbox".
  function isTextbox(element) {
    if (!element || element.nodeType !== 1 || isPageRoot(element)) return false;
    var tag = element.tagName.toLowerCase();
    if (tag === "textarea") return true;
    if (tag === "input") return !NOT_TEXT_INPUTS.test(element.getAttribute("type") || "text");
    if (element.isContentEditable) return true;
    return (element.getAttribute("role") || "").toLowerCase() === "textbox";
  }

  // The text box around a click: a click on a line inside a rich text box gives the whole box.
  function textboxOf(element) {
    if (element.isContentEditable) {
      var node = element;
      while (node.parentElement && node.parentElement.isContentEditable && !isPageRoot(node.parentElement)) {
        node = node.parentElement;
      }
      return node;
    }
    if (isTextbox(element)) return element;
    var box = element.closest ? element.closest("[role='textbox']") : null;
    return box || element;
  }

  function isVisible(element) {
    var box = element.getBoundingClientRect();
    var style = getComputedStyle(element);
    return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

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

  // The document plus every open shadow root in it. Playwright CSS selectors look inside open shadow
  // roots too, so a selector is unique only if it matches one element across all of them.
  function searchRoots() {
    var roots = [document];
    for (var i = 0; i < roots.length; i++) {
      var all = roots[i].querySelectorAll("*");
      for (var j = 0; j < all.length; j++) {
        if (all[j].shadowRoot) roots.push(all[j].shadowRoot);
      }
    }
    return roots;
  }

  function matchCount(selector) {
    try {
      return searchRoots().reduce(function (total, root) {
        return total + root.querySelectorAll(selector).length;
      }, 0);
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
      var unique = tiers[i].filter(function (selector) { return matchCount(selector) === 1; });
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
    for (var depth = 0; node && node.nodeType === 1 && !isPageRoot(node) && depth <= 3; depth++) {
      var hooks = similarHooks(node);
      if (hooks) return hooks;
      node = node.parentElement;
    }
    return element.tagName.toLowerCase();
  }

  // mode: "unique" (one element), "all_similar" (every element like it), "textbox" (the text box
  // around the element, as a unique selector) or "inner" (the element's own tag and hooks, never its
  // parents, for use inside another element). Never a selector for <html> or <body>.
  window.__auditBuildSelector = function (element, mode) {
    if (!element || element.nodeType !== 1) return null;
    if (mode === "textbox") element = textboxOf(element);
    if (isPageRoot(element)) return null;
    if (mode === "inner") return similarHooks(element) || element.tagName.toLowerCase();
    if (mode === "all_similar") return similarSelector(element);
    return uniqueSelector(element);
  };

  window.__auditIsTextbox = isTextbox;

  // True if this frame shows a box a customer can type into (search boxes do not count).
  window.__auditHasVisibleTextbox = function () {
    return searchRoots().some(function (root) {
      return Array.prototype.some.call(root.querySelectorAll(TEXTBOX_CANDIDATES), function (element) {
        var type = (element.getAttribute("type") || "").toLowerCase();
        return isTextbox(element) && type !== "search" && isVisible(element);
      });
    });
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
      // Inside a shadow root, event.target is the host element; the composed path starts at the real one.
      var path = event.composedPath ? event.composedPath() : [];
      var target = path.length ? path[0] : event.target;
      if (target && target.nodeType !== 1) target = target.parentElement;
      if (!target) return;
      window.__auditLastPickTarget = target;  // lets the recorder check if the click was inside an element
      if (typeof window.__auditPick === "function") {
        var box = mode === "textbox" ? textboxOf(target) : target;
        window.__auditPick({
          selector: window.__auditBuildSelector(target, mode),  // null for the page background
          text: (target.innerText || "").trim().slice(0, 500),   // never a typed value
          mode: mode,
          textbox: isTextbox(box),
          // What kind of control the click landed on: a text box, another button or field, or neither.
          control: isTextbox(textboxOf(target)) ? "textbox" : target.closest(CONTROLS) ? "button" : null,
        });
      }
    },
    true
  );
})();
