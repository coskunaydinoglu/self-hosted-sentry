/*
 * Symbol Portal inside Sentry's own layout.
 *
 * nginx injects this file into Sentry's HTML (see nginx.conf, sub_filter). Sentry is
 * not patched. The script:
 *   1. adds a "Symbols" entry to the primary navigation by cloning one of Sentry's own
 *      items, so it inherits the running version's styling;
 *   2. points it at /organizations/<org>/symbols/, a URL inside Sentry's app, so the
 *      navigation, header and secondary sidebar stay exactly as on every other page;
 *   3. on that URL, replaces the content area (#main, where Sentry would show
 *      "Page Not Found") with the portal in a frame that grows with its content;
 *   4. marks the entry as the current page and keeps the "Route not found" error that
 *      Sentry's frontend SDK would report for this URL out of the internal project.
 * React re-renders freely, so everything is re-applied from a MutationObserver. If a
 * future Sentry release changes the markup, the script finds nothing and does nothing;
 * the portal stays reachable at /symbols/?standalone=1.
 */
(function () {
  "use strict";
  if (window.__symbolPortalNav) return;
  window.__symbolPortalNav = true;

  var DEFAULT_ORG = "__SYMBOL_PORTAL_ORG__";
  var LABEL = "Symbols";
  var TITLE = "Symbols \u2014 Sentry";
  var NAV_MARKER = "data-symbol-portal-nav";
  var VIEW_MARKER = "data-symbol-portal-view";
  var ACTIVE_CLASS = "symbol-portal-active";
  var ROUTE_RE = /^\/organizations\/([^/]+)\/symbols\/?$/;
  var ICON_PATH =
    "M9.5 0H3.25A1.75 1.75 0 0 0 1.5 1.75v12.5C1.5 15.22 2.28 16 3.25 16h9.5c.97 0 1.75-.78 1.75-1.75V5L9.5 0Zm3.5 14.25a.25.25 0 0 1-.25.25h-9.5a.25.25 0 0 1-.25-.25V1.75a.25.25 0 0 1 .25-.25H8.5V5.5c0 .41.34.75.75.75H13v8ZM10 2.56 11.94 4.5H10V2.56ZM8.53 7.22a.75.75 0 0 0-1.06 0l-2 2a.75.75 0 1 0 1.06 1.06l.72-.72v2.94a.75.75 0 0 0 1.5 0V9.56l.72.72a.75.75 0 1 0 1.06-1.06l-2-2Z";

  // -- helpers -------------------------------------------------------------------

  function currentOrg() {
    var m = window.location.pathname.match(/^\/organizations\/([^/]+)\//);
    if (m) return m[1];
    var data = window.__initialData || {};
    if (data.lastOrganization) return data.lastOrganization;
    return DEFAULT_ORG.indexOf("__") === 0 ? "sentry" : DEFAULT_ORG;
  }

  function routeUrl() {
    return "/organizations/" + encodeURIComponent(currentOrg()) + "/symbols/";
  }

  function onRoute() {
    return ROUTE_RE.test(window.location.pathname);
  }

  function installStyles() {
    if (document.getElementById("symbol-portal-style")) return;
    var style = document.createElement("style");
    style.id = "symbol-portal-style";
    // Hide Sentry's own content for this URL (its "Page Not Found") without touching
    // React-owned nodes; removing the class restores everything on navigation.
    style.textContent =
      "html." + ACTIVE_CLASS + " #main > :not([" + VIEW_MARKER + "]) { display: none !important; }" +
      "[" + VIEW_MARKER + "] { display: flex; flex-direction: column; flex: 1; width: 100%; min-width: 0; }" +
      "[" + VIEW_MARKER + "] iframe { display: block; width: 100%; border: 0; background: transparent; }";
    document.head.appendChild(style);
  }

  // -- 1. navigation entry ---------------------------------------------------------

  function findTemplate(nav) {
    var links = nav.querySelectorAll("a[data-primary-navigation-link]");
    for (var i = links.length - 1; i >= 0; i--) {
      var li = links[i].closest("li");
      if (li && !li.hasAttribute(NAV_MARKER)) return li;
    }
    return null;
  }

  function stripState(root) {
    var all = [root].concat(Array.prototype.slice.call(root.querySelectorAll("*")));
    all.forEach(function (el) {
      ["aria-current", "aria-selected", "aria-expanded", "data-active-group", "id"].forEach(function (attr) {
        el.removeAttribute(attr);
      });
    });
  }

  function navigateInApp(url) {
    // Same thing React Router does for a push, so Sentry's router renders the new URL
    // without reloading the page. Fall back to a full load if it does not react.
    var state = window.history.state || {};
    var idx = typeof state.idx === "number" ? state.idx + 1 : 0;
    window.history.pushState({ usr: null, key: Math.random().toString(36).slice(2, 10), idx: idx }, "", url);
    window.dispatchEvent(new PopStateEvent("popstate", { state: window.history.state }));
    window.setTimeout(function () {
      if (onRoute() && !document.querySelector("[" + VIEW_MARKER + "]")) window.location.assign(url);
    }, 2500);
  }

  function buildItem(template) {
    var item = template.cloneNode(true);
    item.setAttribute(NAV_MARKER, "");
    stripState(item);

    var link = item.querySelector("a") || item;
    link.setAttribute("aria-label", LABEL);
    link.setAttribute("title", "Symbol Portal: dSYM / ProGuard y\u00fckleme ve eksik semboller");
    link.removeAttribute("target");

    var texts = link.querySelectorAll("span, div");
    for (var i = texts.length - 1; i >= 0; i--) {
      var el = texts[i];
      if (!el.hasAttribute("data-icon-container") && !el.querySelector("svg") && el.textContent.trim()) {
        el.textContent = LABEL;
        break;
      }
    }

    var svg = link.querySelector("[data-icon-container] svg") || link.querySelector("svg");
    if (svg) {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      svg.setAttribute("viewBox", "0 0 16 16");
      svg.setAttribute("fill", "currentColor");
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", ICON_PATH);
      svg.appendChild(path);
    }

    link.addEventListener("click", function (event) {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      event.stopPropagation();
      if (!onRoute()) navigateInApp(routeUrl());
    }, true);
    return item;
  }

  function ensureNavItem() {
    var nav = document.querySelector('nav[aria-label="Primary Navigation"]');
    if (!nav) return;
    var item = nav.querySelector("[" + NAV_MARKER + "]");
    if (!item) {
      var template = findTemplate(nav);
      if (!template || !template.parentNode) return;
      item = buildItem(template);
      template.parentNode.appendChild(item);
    }
    var link = item.querySelector("a") || item;
    if (link.getAttribute("href") !== routeUrl()) link.setAttribute("href", routeUrl());
    // Sentry styles the current item through aria-current="location".
    if (onRoute()) {
      if (link.getAttribute("aria-current") !== "location") link.setAttribute("aria-current", "location");
    } else if (link.hasAttribute("aria-current")) {
      link.removeAttribute("aria-current");
    }
  }

  // -- 2./3. content area ------------------------------------------------------------

  function buildView() {
    var view = document.createElement("div");
    view.setAttribute(VIEW_MARKER, "");
    var frame = document.createElement("iframe");
    frame.title = "Symbol Portal";
    frame.src = "/symbols/?embedded=1" + window.location.search.replace(/^\?/, "&");
    frame.style.height = "calc(100vh - 40px)";
    view.appendChild(frame);
    return view;
  }

  function ensureView() {
    var root = document.documentElement;
    var main = document.getElementById("main");
    var existing = document.querySelector("[" + VIEW_MARKER + "]");
    if (!onRoute() || !main) {
      root.classList.remove(ACTIVE_CLASS);
      if (existing) existing.parentNode.removeChild(existing);
      return;
    }
    installStyles();
    root.classList.add(ACTIVE_CLASS);
    if (existing && existing.parentNode !== main) {
      existing.parentNode.removeChild(existing);
      existing = null;
    }
    if (!existing) main.appendChild(buildView());
    if (document.title !== TITLE) document.title = TITLE;
  }

  // The portal reports its content height so the page scrolls like any Sentry page.
  window.addEventListener("message", function (event) {
    if (event.origin !== window.location.origin || !event.data || event.data.type !== "symbol-portal:height") return;
    var frame = document.querySelector("[" + VIEW_MARKER + "] iframe");
    if (!frame || event.source !== frame.contentWindow) return;
    var top = frame.getBoundingClientRect().top + window.scrollY;
    var minHeight = Math.max(320, window.innerHeight - top);
    frame.style.height = Math.max(minHeight, Math.ceil(event.data.height)) + "px";
  });

  // -- 4. keep "Route not found" for this URL out of the internal project -----------------

  var filterInstalled = false;
  function installErrorFilter() {
    if (filterInstalled) return;
    var carrier = window.__SENTRY__;
    if (!carrier || typeof carrier !== "object") return;
    Object.keys(carrier).forEach(function (key) {
      var entry = carrier[key];
      var scope = entry && entry.globalScope;
      if (!scope || typeof scope.addEventProcessor !== "function" || filterInstalled) return;
      scope.addEventProcessor(function (event) {
        var tags = (event && event.tags) || {};
        var fp = (event && event.fingerprint) || [];
        if (fp.indexOf("RouteNotFound") !== -1 && ROUTE_RE.test(String(tags.pathname || ""))) return null;
        return event;
      });
      filterInstalled = true;
    });
  }

  // -- scheduling --------------------------------------------------------------------

  var scheduled = false;
  function run() {
    scheduled = false;
    try {
      installErrorFilter();
      ensureNavItem();
      ensureView();
    } catch (e) {
      /* never break Sentry's UI */
    }
  }
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(run);
  }

  function start() {
    schedule();
    new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
    window.addEventListener("popstate", schedule);
  }

  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start);
})();
