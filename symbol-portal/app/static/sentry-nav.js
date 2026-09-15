/*
 * Adds a "Symbols" entry to Sentry's primary navigation that opens the Symbol Portal.
 *
 * nginx injects this file into Sentry's HTML (see nginx.conf, sub_filter). Sentry is
 * not patched: we clone one of its own navigation items so the entry inherits the
 * exact styling of the running version, then swap the link, label and icon. React
 * re-renders the sidebar freely, so a MutationObserver puts the entry back whenever
 * it disappears. If a future Sentry release changes the markup, the script simply
 * finds nothing and does nothing.
 */
(function () {
  "use strict";
  if (window.__symbolPortalNav) return;
  window.__symbolPortalNav = true;

  var PORTAL_URL = "/symbols/";
  var LABEL = "Symbols";
  var MARKER = "data-symbol-portal-nav";
  // 16x16 "document with arrow up" glyph in the style of Sentry's icon set.
  var ICON_PATH =
    "M9.5 0H3.25A1.75 1.75 0 0 0 1.5 1.75v12.5C1.5 15.22 2.28 16 3.25 16h9.5c.97 0 1.75-.78 1.75-1.75V5L9.5 0Zm3.5 14.25a.25.25 0 0 1-.25.25h-9.5a.25.25 0 0 1-.25-.25V1.75a.25.25 0 0 1 .25-.25H8.5V5.5c0 .41.34.75.75.75H13v8ZM10 2.56 11.94 4.5H10V2.56ZM8.53 7.22a.75.75 0 0 0-1.06 0l-2 2a.75.75 0 1 0 1.06 1.06l.72-.72v2.94a.75.75 0 0 0 1.5 0V9.56l.72.72a.75.75 0 1 0 1.06-1.06l-2-2Z";

  function findTemplate(nav) {
    // Prefer a plain link item (not a menu button) from the main list.
    var links = nav.querySelectorAll("a[data-primary-navigation-link]");
    for (var i = links.length - 1; i >= 0; i--) {
      var li = links[i].closest("li");
      if (li && !li.hasAttribute(MARKER)) return li;
    }
    return null;
  }

  function stripState(root) {
    var all = [root].concat(Array.prototype.slice.call(root.querySelectorAll("*")));
    all.forEach(function (el) {
      ["aria-current", "aria-selected", "aria-expanded", "data-active", "data-is-active", "id"].forEach(function (attr) {
        el.removeAttribute(attr);
      });
    });
  }

  function buildItem(template) {
    var item = template.cloneNode(true);
    item.setAttribute(MARKER, "");
    stripState(item);

    var link = item.querySelector("a") || item;
    link.setAttribute("href", PORTAL_URL);
    link.setAttribute("aria-label", LABEL);
    link.setAttribute("title", "Symbol Portal: dSYM / ProGuard yükleme ve eksik semboller");
    link.removeAttribute("target");

    // Label: the last text-bearing element that is not the icon container.
    var texts = link.querySelectorAll("span, div");
    var labelEl = null;
    for (var i = texts.length - 1; i >= 0; i--) {
      var el = texts[i];
      if (!el.hasAttribute("data-icon-container") && !el.querySelector("svg") && el.textContent.trim()) {
        labelEl = el;
        break;
      }
    }
    if (labelEl) labelEl.textContent = LABEL;

    var svg = link.querySelector("[data-icon-container] svg") || link.querySelector("svg");
    if (svg) {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      svg.setAttribute("viewBox", "0 0 16 16");
      svg.setAttribute("fill", "currentColor");
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", ICON_PATH);
      svg.appendChild(path);
    }

    // The clone carries no React handlers; make sure clicks do a normal navigation.
    link.addEventListener("click", function (event) {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
      event.preventDefault();
      event.stopPropagation();
      window.location.assign(PORTAL_URL);
    }, true);
    return item;
  }

  function ensure() {
    var nav = document.querySelector('nav[aria-label="Primary Navigation"]');
    if (!nav || nav.querySelector("[" + MARKER + "]")) return;
    var template = findTemplate(nav);
    if (!template || !template.parentNode) return;
    template.parentNode.appendChild(buildItem(template));
  }

  var scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(function () {
      scheduled = false;
      try {
        ensure();
      } catch (e) {
        /* never break Sentry's UI */
      }
    });
  }

  function start() {
    schedule();
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  }

  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start);
})();
