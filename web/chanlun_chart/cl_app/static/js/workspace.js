(function () {
  'use strict';
  // Embedded charts and settings keep their full viewport without a second nav.
  document.documentElement.classList.toggle('cp-embedded', window.self !== window.top);
  function syncTheme() {
    try {
      const theme = JSON.parse(localStorage.getItem(window.WorkspaceDevice?.preferenceKey('tv_chart') || 'tv_chart') || '{}').theme;
      if (theme === 'light' || theme === 'dark') {
        document.documentElement.classList.toggle('cl-theme-dark', theme === 'dark');
      }
    } catch (_) { /* Storage is optional. */ }
  }
  syncTheme();
  window.addEventListener('storage', function (event) {
    if (event.key === (window.WorkspaceDevice?.preferenceKey('tv_chart') || 'tv_chart')) syncTheme();
  });
  window.addEventListener('workspace-preferences-ready', syncTheme);
})();
