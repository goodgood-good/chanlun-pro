(function (root) {
  'use strict';
  if (root.WorkspaceDevice) return;
  let viewport = root;
  try { if (root.top && root.top.location.origin === root.location.origin) viewport = root.top; } catch (_) {}
  const narrow = Number(viewport.innerWidth || 1440) <= 760;
  const screen = viewport.screen || {};
  const smallScreen = Math.min(Number(screen.width || 1440), Number(screen.height || 900)) <= 760;
  const touch = !!viewport.matchMedia?.('(pointer: coarse)').matches;
  // Keep the profile stable across orientation changes and embedded chart sizes.
  const phone = narrow || (touch && smallScreen);
  const scoped = new Set(['tv_chart', 'chart_menu_width', 'chart_menu_collapsed']);
  root.WorkspaceDevice = {
    isPhone: phone,
    preferenceKey(key) { return phone && scoped.has(key) ? 'phone_' + key : key; },
    isCurrentKey(key) { return phone ? !scoped.has(key) : !String(key).startsWith('phone_'); },
    layoutClient(client) { return phone ? client + '_phone' : client; },
    layout(value) { return phone ? 'single' : value; },
  };
  root.document?.documentElement?.classList?.toggle('cp-mobile', phone);
})(typeof window !== 'undefined' ? window : globalThis);
