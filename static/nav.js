/* NOVA — navigation + shell behaviour */
(function () {
  "use strict";

  const TABS = ["radar", "terminal", "winrates", "history", "settings"];
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  /* ── core tab switching ─────────────────────────────────────────── */
  function setActiveTab(tab) {
    if (!TABS.includes(tab)) tab = "terminal";
    $$(".page").forEach((p) => p.classList.remove("page-active"));
    const page = $("#page-" + tab);
    if (page) {
      // retrigger the enter animation
      page.classList.remove("page-active");
      void page.offsetWidth;
      page.classList.add("page-active");
    }
    $$(".nav-item").forEach((b) =>
      b.classList.toggle("active", b.dataset.page === tab));
    $$(".tab-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.tab === tab));
    try { localStorage.setItem("nova_tab", tab); } catch (e) {}
    // notify chart.js (it wires per-tab data loading)
    document.dispatchEvent(new CustomEvent("nova:tab", { detail: tab }));
  }
  window._setActiveTab = setActiveTab;

  $$(".nav-item").forEach((b) =>
    b.addEventListener("click", () => setActiveTab(b.dataset.page)));
  $$(".tab-btn").forEach((b) =>
    b.addEventListener("click", () => setActiveTab(b.dataset.tab)));

  /* ── mobile sidebar drawer ──────────────────────────────────────── */
  const sidebar = $("#sidebar");
  const backdrop = $("#sidebar-backdrop");
  function closeDrawer() {
    sidebar.classList.remove("open");
    backdrop.classList.remove("show");
  }
  $("#sidebar-toggle").addEventListener("click", () => {
    sidebar.classList.toggle("open");
    backdrop.classList.toggle("show", sidebar.classList.contains("open"));
  });
  backdrop.addEventListener("click", closeDrawer);
  sidebar.addEventListener("click", (e) => {
    if (e.target.closest(".nav-item")) closeDrawer();
  });

  /* ── restore last tab ───────────────────────────────────────────── */
  let saved = "terminal";
  try { saved = localStorage.getItem("nova_tab") || "terminal"; } catch (e) {}
  setActiveTab(TABS.includes(saved) ? saved : "terminal");
})();
