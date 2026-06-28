// theme.js — system + manual dark/light toggle, persisted.
// Honor prefers-color-scheme; explicit choice wins; no FOUC (script in head).

(function () {
  "use strict";
  var STORAGE = "dac-color-scheme";
  var root = document.documentElement;
  var toggle = document.querySelector("[data-theme-toggle]");
  var stateLabel = document.querySelector("[data-theme-state]");

  function systemPref() {
    return window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark" : "light";
  }

  // Use stored if valid; else system; default light if no signal.
  var stored = null;
  try { stored = localStorage.getItem(STORAGE); } catch (e) {}
  var initial = (stored === "light" || stored === "dark") ? stored : systemPref();
  root.setAttribute("data-theme", initial);
  if (toggle)   toggle.setAttribute("data-state", initial);
  if (stateLabel) stateLabel.textContent = initial;

  // Update toggle glyph on subsequent system changes when user hasn't chosen.
  if (window.matchMedia) {
    var mql = window.matchMedia("(prefers-color-scheme: dark)");
    var onChange = function (e) {
      try { if (localStorage.getItem(STORAGE)) return; } catch (err) {}
      var next = e.matches ? "dark" : "light";
      root.setAttribute("data-theme", next);
      if (toggle) toggle.setAttribute("data-state", next);
      if (stateLabel) stateLabel.textContent = next;
    };
    if (mql.addEventListener) mql.addEventListener("change", onChange);
    else if (mql.addListener) mql.addListener(onChange);
  }

  // Click to cycle only between explicit light/dark.
  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = (root.getAttribute("data-theme") === "dark") ? "light" : "dark";
      root.setAttribute("data-theme", next);
      toggle.setAttribute("data-state", next);
      if (stateLabel) stateLabel.textContent = next;
      try { localStorage.setItem(STORAGE, next); } catch (e) {}
    });
  }

  // Progressive enhancement reveal on scroll — gated by reduced motion.
  var prefersReduced = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!prefersReduced && "IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in");
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -10% 0px", threshold: 0.05 });
    document.querySelectorAll(".reveal").forEach(function (el) {
      io.observe(el);
    });
  } else {
    // No observer: just show everything.
    document.querySelectorAll(".reveal").forEach(function (el) {
      el.classList.add("is-in");
    });
  }
})();