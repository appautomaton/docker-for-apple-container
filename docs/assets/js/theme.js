// theme.js — theme toggle (persisted), scroll reveals, responsive nav.
(function () {
  "use strict";
  var STORAGE = "dac-theme";
  var root = document.documentElement;

  function systemPref() {
    return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  var toggle = document.querySelector("[data-theme-toggle]");
  var stored = null;
  try { stored = localStorage.getItem(STORAGE); } catch (e) {}
  var initial = (stored === "light" || stored === "dark") ? stored : systemPref();
  root.setAttribute("data-theme", initial);
  if (toggle) toggle.setAttribute("data-state", initial);

  // Follow the system when the user has not chosen explicitly.
  if (window.matchMedia) {
    var mqDark = matchMedia("(prefers-color-scheme: dark)");
    var onSystem = function (e) {
      try { if (localStorage.getItem(STORAGE)) return; } catch (err) {}
      var next = e.matches ? "dark" : "light";
      root.setAttribute("data-theme", next);
      if (toggle) toggle.setAttribute("data-state", next);
    };
    if (mqDark.addEventListener) mqDark.addEventListener("change", onSystem);
    else if (mqDark.addListener) mqDark.addListener(onSystem);
  }

  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      toggle.setAttribute("data-state", next);
      try { localStorage.setItem(STORAGE, next); } catch (e) {}
    });
  }

  // Reveal on scroll (only when motion is allowed).
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!reduce && "IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { entry.target.classList.add("is-in"); io.unobserve(entry.target); }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.05 });
    document.querySelectorAll(".reveal").forEach(function (el) { io.observe(el); });
  } else {
    document.querySelectorAll(".reveal").forEach(function (el) { el.classList.add("is-in"); });
  }

  // Nav: inline + open on desktop, collapsible on mobile.
  var nav = document.querySelector(".nav");
  if (nav) {
    var mqMobile = matchMedia("(max-width: 767px)");
    var syncNav = function () {
      if (mqMobile.matches) nav.removeAttribute("open");
      else nav.setAttribute("open", "");
    };
    syncNav();
    if (mqMobile.addEventListener) mqMobile.addEventListener("change", syncNav);
    else if (mqMobile.addListener) mqMobile.addListener(syncNav);
    nav.querySelectorAll(".nav__links a").forEach(function (link) {
      link.addEventListener("click", function () { if (mqMobile.matches) nav.removeAttribute("open"); });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && mqMobile.matches) nav.removeAttribute("open");
    });
  }
})();
