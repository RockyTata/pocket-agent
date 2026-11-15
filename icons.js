// 统一使用继承文字颜色的线性图标，避免界面依赖系统 emoji 样式。
(() => {
  const paths = {
    pin: '<path d="M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6ZM12 14v7"/>',
    trash: '<path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5"/>',
    map: '<path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3V6Zm6-3v15m6-12v15"/>',
    folder: '<path d="M3 6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6Z"/>',
    check: '<path d="m4 12 5 5L20 6"/>', undo: '<path d="M9 7 5 11l4 4M5 11h8a6 6 0 0 1 0 12"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m16 16 4 4"/>',
    theme: '<circle cx="12" cy="12" r="8"/><path d="M12 4v16a8 8 0 0 0 0-16Z"/>',
    book: '<path d="M12 6c-2-2-6-2-9-1v14c3-1 7-1 9 1m0-14c2-2 6-2 9-1v14c-3-1-7-1-9 1V6Z"/>',
    edit: '<path d="m4 20 4-.8L19 8l-3-3L5 16l-1 4ZM14 7l3 3"/>',
    play: '<path d="m8 5 11 7-11 7V5Z"/>', bot: '<rect x="4" y="7" width="16" height="12" rx="4"/><path d="M12 4v3M8 12h.01M16 12h.01M9 16h6"/>',
    clip: '<path d="m8 12 6-6a4 4 0 0 1 6 6l-8 8a6 6 0 0 1-8-8l8-8M8 16l8-8"/>',
    link: '<path d="M10 13a4 4 0 0 0 6 0l3-3a4 4 0 0 0-6-6l-2 2m3 5a4 4 0 0 0-6 0l-3 3a4 4 0 0 0 6 6l2-2"/>',
    model: '<path d="M12 3a4 4 0 0 0-4 4 4 4 0 0 0-2 7 4 4 0 0 0 6 5m0-16a4 4 0 0 1 4 4 4 4 0 0 1 2 7 4 4 0 0 1-6 5V3Z"/>',
    chat: '<path d="M4 5h16v12H9l-5 3V5Z"/>', compact: '<path d="M4 8h16M7 12h10M10 16h4"/>', chart: '<path d="M4 20h16M7 17v-5m5 5V5m5 12V9"/>',
    move: '<path d="M3 8h17m-4-4 4 4-4 4M21 16H4m4-4-4 4 4 4"/>',
    send: '<path d="m3 11 18-8-7 18-3-7-8-3Zm8 3L21 3"/>', alert: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/>', info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/>'
  };
  window.ICON = (name, size = 18) => `<svg class="icon icon-${name}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || ''}</svg>`;
})();
