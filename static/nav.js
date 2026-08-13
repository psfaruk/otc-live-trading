/* ── Plybit AI — nav.js ─────────────────────────────────────────────────── *
 * Page navigation system: sidebar (desktop) + bottom tabs (mobile)         *
 * 4-tab layout: Home / Chart / History / Settings                          *
 * chart.js's _setActiveTab calls Nav.setPage() to keep both nav systems     *
 * in sync. Sidebar clicks are routed THROUGH _setActiveTab so per-tab        *
 * loaders (share-signals poll, history load, settings poll) actually fire.  *
 * ─────────────────────────────────────────────────────────────────────────── */
'use strict';

const Nav = (() => {
  const PAGES = ['tab-home', 'tab-advance', 'tab-history', 'tab-settings'];
  const TAB_MAP = {
    home:     'tab-home',
    chart:    'tab-advance',
    history:  'tab-history',
    settings: 'tab-settings',
  };

  let _currentPage = 'tab-advance';

  // ── Sidebar toggle (mobile drawer) ──────────────────────────────────────
  const sidebar     = document.getElementById('sidebar');
  const sidebarBg   = document.getElementById('sidebar-backdrop');
  const sidebarToggle = document.getElementById('sidebar-toggle');

  function openSidebar() {
    if (!sidebar) return;
    sidebar.classList.add('open');
    if (sidebarBg) sidebarBg.classList.remove('hidden');
  }
  function closeSidebar() {
    if (!sidebar) return;
    sidebar.classList.remove('open');
    if (sidebarBg) sidebarBg.classList.add('hidden');
  }

  if (sidebarToggle) sidebarToggle.addEventListener('click', () => {
    sidebar.classList.contains('open') ? closeSidebar() : openSidebar();
  });
  if (sidebarBg) sidebarBg.addEventListener('click', closeSidebar);

  // ── Side panel toggle (collapse/expand on desktop) ──────────────────────
  const sidePanel = document.getElementById('side-panel');
  const sidePanelToggle = document.getElementById('side-panel-toggle');
  if (sidePanelToggle && sidePanel) {
    sidePanelToggle.addEventListener('click', () => {
      sidePanel.classList.toggle('collapsed');
    });
  }

  // ── Page switching ─────────────────────────────────────────────────────
  function setPage(tabKey) {
    const pageId = TAB_MAP[tabKey];
    if (!pageId) return;
    _currentPage = pageId;

    // Show/hide pages
    for (const id of PAGES) {
      const el = document.getElementById(id);
      if (!el) continue;
      const isTarget = id === pageId;
      el.classList.toggle('hidden', !isTarget);
      el.classList.toggle('page-active', isTarget);
      if (isTarget) {
        el.classList.remove('page-enter');
        void el.offsetWidth;
        el.classList.add('page-enter');
        requestAnimationFrame(() => {
          requestAnimationFrame(() => el.classList.remove('page-enter'));
        });
      } else {
        el.classList.remove('page-enter');
      }
    }

    // Update sidebar active
    document.querySelectorAll('#sidebar .nav-item').forEach((item) => {
      item.classList.toggle('active', item.dataset.page === tabKey);
    });

    // Update bottom tabs active
    document.querySelectorAll('#bottom-tabs .tab-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.tab === tabKey);
    });

    // Close mobile sidebar if open
    closeSidebar();
  }

  // ── Sidebar nav item clicks ────────────────────────────────────────────
  // Route THROUGH chart.js's _setActiveTab so the per-tab loaders fire on
  // desktop too (sidebar clicks used to bypass them — share-signals/history/
  // settings never populated when the user clicked the desktop sidebar).
  document.querySelectorAll('#sidebar .nav-item').forEach((item) => {
    item.addEventListener('click', () => {
      const page = item.dataset.page;
      if (typeof window._setActiveTab === 'function') {
        window._setActiveTab(page);
      } else {
        setPage(page);
      }
    });
  });

  // ── Expose for chart.js to call ────────────────────────────────────────
  return { setPage, openSidebar, closeSidebar };
})();
