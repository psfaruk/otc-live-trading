/* ── Plybit AI — nav.js ─────────────────────────────────────────────────── *
 * Page navigation system: sidebar (desktop) + bottom tabs (mobile)         *
 * Handles page switching, sidebar toggle, and history-nav shortcut.        *
 * chart.js's _setActiveTab is patched to also call this module's           *
 * setPage() so both navigation systems stay in sync.                       *
 * ─────────────────────────────────────────────────────────────────────────── */
'use strict';

const Nav = (() => {
  const PAGES = ['tab-home', 'tab-signals', 'tab-advance', 'tab-settings'];
  const TAB_MAP = { home: 'tab-home', signals: 'tab-signals', advance: 'tab-advance', settings: 'tab-settings' };

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
        // Trigger CSS entrance animation
        el.classList.remove('page-enter');
        // Force reflow to restart animation
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
      const p = item.dataset.page;
      const isActive = (p === tabKey) || (p === 'history-nav' && tabKey === 'advance');
      item.classList.toggle('active', isActive);
    });

    // Update bottom tabs active
    document.querySelectorAll('#bottom-tabs .tab-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.tab === tabKey);
    });

    // Close mobile sidebar if open
    closeSidebar();
  }

  // ── Sidebar nav item clicks ────────────────────────────────────────────
  document.querySelectorAll('#sidebar .nav-item').forEach((item) => {
    item.addEventListener('click', () => {
      const page = item.dataset.page;
      if (page === 'history-nav') {
        // History: on desktop, just open the modal directly
        setPage('advance');
        if (typeof openHistory === 'function') setTimeout(openHistory, 100);
      } else if (page === 'settings') {
        // Settings: on desktop use page, on mobile use page
        setPage('settings');
      } else {
        setPage(page);
      }
    });
  });

  // ── Expose for chart.js to call ────────────────────────────────────────
  return { setPage, openSidebar, closeSidebar };
})();