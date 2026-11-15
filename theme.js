// 所有页面共用的小工具：白天/夜间、震动反馈。
// 主题存在 localStorage('theme')：dark（默认）/ light / auto（跟随系统）。
// 放在 <head> 里同步加载，页面画出来之前就定好颜色，不会先黑一下再变白。
(function () {
  var K = 'theme', mq = window.matchMedia ? matchMedia('(prefers-color-scheme: light)') : null;
  function get() { try { return localStorage.getItem(K) || 'dark'; } catch (e) { return 'dark'; } }
  function apply() {
    var t = get(), light = t === 'light' || (t === 'auto' && mq && mq.matches), h = document.documentElement;
    h.classList.toggle('light', !!light);
    h.style.colorScheme = light ? 'light' : 'dark';
  }
  apply();
  addEventListener('storage', function (e) { if (e.key === K) apply(); });
  if (mq && mq.addEventListener) mq.addEventListener('change', apply);
  window.getTheme = get;
  window.setTheme = function (t) { try { localStorage.setItem(K, t); } catch (e) {} apply(); };

  // 震动反馈，克制着用：tap 轻点（切换开关、选中）、confirm 成功、reject 不行/出错、long 长按弹出菜单。
  // 有 navigator.vibrate 就震（安卓浏览器），没有（iPhone Safari）就什么也不做
  window.haptic = function (k) {
    k = k || 'tap';
    try { navigator.vibrate && navigator.vibrate(k === 'confirm' ? [12, 70, 12] : k === 'reject' ? [30, 50, 30] : k === 'long' ? 25 : 8); } catch (e) {}
  };

  // 底部面板往下拽就关。每页的面板结构不一样，所以由页面告诉它：哪块是面板、现在开着没、怎么关。
  // 用 touch 不用 pointer：手指一动浏览器开始滚动会发 pointercancel，pointer 拿不到后面的位置。
  // 面板内容没滚到顶时不接管，免得和面板里的滚动打架；拽不够 90px 就弹回去
  window.dragToClose = function (panel, isOpen, close) {
    var y0 = 0, dy = 0, on = false, drag = false;
    panel.addEventListener('touchstart', function (e) {
      on = isOpen() && panel.scrollTop <= 0 && !e.target.closest('input,textarea,select,[data-nodrag]');
      drag = false; dy = 0; if (on) y0 = e.touches[0].clientY;
    }, { passive: true });
    panel.addEventListener('touchmove', function (e) {
      if (!on) return; dy = e.touches[0].clientY - y0;
      if (dy < 0 && !drag) { on = false; return; }
      if (dy > 6) { e.preventDefault(); drag = true; panel.style.transition = 'none'; panel.style.transform = 'translateY(' + Math.max(0, dy) + 'px)'; }
    }, { passive: false });
    function end() {
      if (!on) return; on = false; if (!drag) return;
      panel.style.transition = '';
      if (dy > 90) {   // 从松手的位置接着滑下去，不先弹回顶再收
        panel.style.transform = 'translateY(105%)'; close();
        setTimeout(function () { panel.style.transform = ''; }, 320);
      } else panel.style.transform = '';
    }
    panel.addEventListener('touchend', end); panel.addEventListener('touchcancel', end);
  };
})();
