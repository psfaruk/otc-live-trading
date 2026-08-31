/* ── Plybit AI — nav.js ─────────────────────────────────────────────────── *
 * Page navigation system: sidebar (desktop) + bottom tabs (mobile)         *
 * 5-tab layout: Home / Chart / Win Rates / History / Settings                          *
 * chart.js's _setActiveTab calls Nav.setPage() to keep both nav systems     *
 * in sync. Sidebar clicks are routed THROUGH _setActiveTab so per-tab        *
 * loaders (share-signals poll, history load, settings poll) actually fire.  *
 * ─────────────────────────────────────────────────────────────────────────── */
'use strict';

const Nav = (() => {
  const PAGES = ['tab-home', 'tab-advance', 'tab-analytics', 'tab-history', 'tab-settings'];
  const TAB_MAP = {
    home:      'tab-home',
    chart:     'tab-advance',
    analytics: 'tab-analytics',
    history:   'tab-history',
    settings:  'tab-settings',
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

  // ── Side panel toggle (collapse/expand on desktop, slide-up sheet on
  // mobile) ─────────────────────────────────────────────────────────────
  // Desktop (>=768px) uses .collapsed to slide the always-visible panel off
  // to the right (style.css:3024-3033). Mobile (<=767px) renders the panel
  // as a bottom sheet that's hidden by default and needs .active to slide
  // up (style.css:2144); .collapsed has the SAME off-screen transform as
  // the default hidden state there, so toggling it on mobile was a no-op —
  // the button did nothing on any phone/tablet under 768px.
  const sidePanel = document.getElementById('side-panel');
  const sidePanelToggle = document.getElementById('side-panel-toggle');
  // Mobile-only floating trigger — lives OUTSIDE #side-panel in the DOM
  // (index.html) so it isn't dragged off-screen by the panel's own
  // show/hide transform the way #side-panel-toggle (nested inside) is
  // while the sheet is closed. See index.html's comment on this element.
  const sidePanelToggleMobile = document.getElementById('side-panel-toggle-mobile');
  const _mobileNavQuery = window.matchMedia('(max-width: 1023px)');

  function toggleSidePanel() {
    if (_mobileNavQuery.matches) {
      // Mobile bottom-sheet: .open slides it up (style.css media query).
      const nowOpen = sidePanel.classList.toggle('open');
      if (sidePanelToggleMobile) {
        sidePanelToggleMobile.classList.toggle('open', nowOpen);
      }
    } else {
      sidePanel.classList.toggle('collapsed');
    }
  }

  if (sidePanelToggle && sidePanel) {
    sidePanelToggle.addEventListener('click', toggleSidePanel);
  }
  if (sidePanelToggleMobile && sidePanel) {
    sidePanelToggleMobile.addEventListener('click', toggleSidePanel);
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

    // The Deep Analysis panel only exists on the Chart tab — the floating
    // mobile toggle that opens it (a sibling of #bottom-tabs, not scoped
    // inside #tab-advance — see index.html) has to be shown/hidden by JS
    // here rather than by nesting/CSS, since it deliberately lives outside
    // that page's DOM subtree.
    if (sidePanelToggleMobile) {
      sidePanelToggleMobile.classList.toggle('hidden', tabKey !== 'chart');
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
