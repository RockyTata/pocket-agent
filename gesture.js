// 派活页的手势（预览版 v2.html 用）：
// 1. 对话页里往右滑 = 返回列表，页面跟着手指走，松手按距离和速度决定退回还是弹回
// 2. 列表里一张对话卡往左滑，露出「置顶」「收起」两个按钮
// 用到的 S / post / loadState / haptic / toast / renderList 都是 v2.html 主脚本里的全局绑定
(() => {
  const W = () => innerWidth;
  const spring = 'cubic-bezier(.32,.72,0,1)';
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  // 这些地方本身要横向滑（代码块、表格、输入框），手势让给它们
  const own = el => el.closest('pre, table, textarea, input, select, #qswipe, .xm-mind, [data-noswipe]');

  // ---------- 1. 右滑返回 ----------
  // 不只认屏幕最左边那一条：安卓全面屏手势把边缘让给了系统返回，从边上起手会被系统吃掉。
  // 改成左半屏任何地方起手、明显是横向往右拖，就当返回
  const parts = () => [document.querySelector('header'), document.getElementById('main'), document.getElementById('foot')];
  let bk = null;
  addEventListener('pointerdown', e => {
    if (!document.body.classList.contains('inthread') || e.pointerType === 'mouse' && e.button) return;
    if (e.clientX > W() * 0.5 || own(e.target) || document.querySelector('.sheet.on, #viewer.on')) return;
    bk = { x: e.clientX, y: e.clientY, t: performance.now(), on: false, dx: 0, id: e.pointerId };
  }, { passive: true });
  addEventListener('pointermove', e => {
    if (!bk || e.pointerId !== bk.id) return;
    const dx = e.clientX - bk.x, dy = e.clientY - bk.y;
    if (!bk.on) {
      // 先判断方向：竖着滑是在看消息，直接放手；横着往右且明显比竖的多，才接管
      if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) { bk = null; return; }
      if (dx < 12 || dx < Math.abs(dy) * 1.6) return;
      bk.on = true; bk.x = e.clientX; document.body.classList.add('swiping');
      parts().forEach(p => p && (p.style.transition = 'none'));
    }
    bk.dx = Math.max(0, e.clientX - bk.x); bk.v = bk.dx / Math.max(1, performance.now() - bk.t);
    const x = bk.dx;
    parts().forEach(p => p && (p.style.transform = `translateX(${x}px)`));
    document.body.style.setProperty('--swipe', (x / W()).toFixed(3));
  }, { passive: true });
  const endBack = e => {
    if (!bk || e.pointerId !== bk.id) return;
    const b = bk; bk = null;
    if (!b.on) return;
    const go = b.dx > W() * 0.33 || (b.v > 0.45 && b.dx > 40);
    const ps = parts().filter(Boolean);
    const dur = reduce ? 0 : go ? 260 : 380;
    ps.forEach(p => { p.style.transition = `transform ${dur}ms ${spring}`; p.style.transform = go ? `translateX(${W()}px)` : ''; });
    setTimeout(() => {
      document.body.classList.remove('swiping'); document.body.style.removeProperty('--swipe');
      if (go) {
        haptic('tap');
        // 先把位置复原再退：退回后列表的进场动画会接着播，不会看到对话页从右边闪回来
        ps.forEach(p => { p.style.transition = 'none'; p.style.transform = ''; p.style.opacity = '0'; });
        history.back();
        requestAnimationFrame(() => requestAnimationFrame(() => ps.forEach(p => { p.style.transition = ''; p.style.opacity = ''; })));
      } else ps.forEach(p => p.style.transition = '');
    }, dur);
  };
  addEventListener('pointerup', endBack); addEventListener('pointercancel', endBack);

  // ---------- 2. 卡片左滑出按钮 ----------
  const list = document.getElementById('list');
  const ACT = 148;                      // 两个按钮露出来的宽度
  let sw = null, open = null;           // sw = 正在拖的；open = 停在打开状态的那张
  // 列表每 2.5 秒整页重画，打开着的卡会被冲掉。拖动或打开时先不重画，收回去再补一次
  const orig = renderList;
  renderList = function () { if (sw || open) { renderList.skipped = true; return; } return orig.apply(this, arguments); };
  // 吞掉紧跟着的那一下点击。拖得远时浏览器根本不发 click，所以 0.5 秒后自己撤掉，免得把下一次真点击吃了
  function swallow(el) {
    const f = ev => { ev.stopPropagation(); ev.preventDefault(); };
    el.addEventListener('click', f, { capture: true, once: true });
    setTimeout(() => el.removeEventListener('click', f, { capture: true }), 500);
  }
  function actions(card) {
    const t = (S.threads || []).find(x => x.id === card.dataset.id); if (!t) return null;
    const a = document.createElement('div'); a.className = 'swAct';
    a.style.top = card.offsetTop + 'px'; a.style.height = card.offsetHeight + 'px';
    a.innerHTML = `<button data-sa="pin">${t.pin ? '取消置顶' : '置顶'}</button>`
      + (t.status === 'running' ? '' : `<button data-sa="${t.status === 'idle' || t.status === 'done' ? 'back' : 'hide'}" class="hide">${t.status === 'idle' || t.status === 'done' ? '放回' : '收起'}</button>`);
    list.appendChild(a); return a;
  }
  function close(animate = true) {
    if (!open) return;
    const { card, act } = open; open = null;
    card.style.transition = animate && !reduce ? `transform .38s ${spring}` : 'none'; card.style.transform = '';
    act.style.opacity = '0';
    setTimeout(() => { act.remove(); card.style.transition = ''; if (renderList.skipped) { renderList.skipped = false; renderList(); } }, animate ? 380 : 0);
  }
  list.addEventListener('pointerdown', e => {
    const card = e.target.closest('.card[data-id]');
    if (open && (!card || card !== open.card) && !e.target.closest('.swAct')) {
      // 这一下只是把开着的卡收回去，不能顺带点开手指下面那张
      swallow(list);
      close(); return;
    }
    if (!card || own(e.target)) return;
    sw = { card, x: e.clientX, y: e.clientY, on: false, id: e.pointerId, base: open && open.card === card ? -ACT : 0 };
  }, { passive: true });
  list.addEventListener('pointermove', e => {
    if (!sw || e.pointerId !== sw.id) return;
    const dx = e.clientX - sw.x, dy = e.clientY - sw.y;
    if (!sw.on) {
      if (Math.abs(dy) > 8 && Math.abs(dy) > Math.abs(dx)) { sw = null; return; }
      if (Math.abs(dx) < 10 || Math.abs(dx) < Math.abs(dy) * 1.6) return;
      if (!sw.base && dx > 0) { sw = null; return; }   // 关着的卡往右滑没意义
      sw.on = true; sw.x = e.clientX;
      if (!open) { const act = actions(sw.card); if (!act) { sw = null; return; } open = { card: sw.card, act }; }
      sw.card.style.transition = 'none'; sw.card.classList.add('swDrag');
    }
    let x = sw.base + (e.clientX - sw.x);
    // 拉过头有阻尼，像 iOS 那样越拉越沉，不会一下甩出屏幕
    if (x < -ACT) x = -ACT - Math.pow(-ACT - x, 0.7);
    if (x > 0) x = 0;
    sw.card.style.transform = `translateX(${x}px)`; sw.last = x;
    open.act.style.opacity = Math.min(1, -x / ACT).toFixed(2);
  }, { passive: true });
  const endCard = e => {
    if (!sw || e.pointerId !== sw.id) return;
    const s = sw; sw = null;
    const eat = () => swallow(s.card);
    // 点一下开着的那张卡 = 收回去，不是点开
    if (!s.on) { if (s.base) { eat(); close(); } return; }
    s.card.classList.remove('swDrag');
    // 卡片本身的点击会紧跟着触发，拖过就不算点开
    eat();
    if (s.last < -ACT * 0.45) {
      s.card.style.transition = reduce ? 'none' : `transform .42s ${spring}`; s.card.style.transform = `translateX(${-ACT}px)`;
      open.act.style.opacity = '1'; haptic('tap');
    } else close();
  };
  list.addEventListener('pointerup', endCard); list.addEventListener('pointercancel', endCard);
  list.addEventListener('click', async e => {
    const b = e.target.closest('.swAct button'); if (!b || !open) return;
    e.stopPropagation();
    const t = (S.threads || []).find(x => x.id === open.card.dataset.id); if (!t) return close();
    try {
      if (b.dataset.sa === 'pin') await post('/api/console/meta', { id: t.id, pin: !t.pin });
      else await post('/api/console/mark', { id: t.id, status: b.dataset.sa === 'hide' ? 'idle' : 'review' });
    } catch (er) { toast('没改成：' + er.message); return close(); }
    haptic('confirm');
    toast(b.dataset.sa === 'pin' ? (t.pin ? '取消置顶了' : '置顶了') : b.dataset.sa === 'hide' ? '收起了，在「其他」里' : '放回「做完了」', true);
    close(false); loadState();
  }, true);
  // 滚动列表时把打开的卡收回去，免得滚走了还开着
  document.getElementById('main').addEventListener('scroll', () => { if (open && !sw) close(); }, { passive: true });

  const css = document.createElement('style');
  css.textContent = `
  #list { position: relative; }
  #list > .card { position: relative; z-index: 1; touch-action: pan-y; }
  /* 对话页只让浏览器管竖着滚，横着的交给右滑返回；不设的话 Chrome 会把横滑当成自己的返回手势或直接取消触摸 */
  body.inthread #main { touch-action: pan-y; }
  .swAct { position: absolute; right: 0; z-index: 0; display: flex; gap: 8px; align-items: center; padding-right: 4px; opacity: 0; transition: opacity .2s; }
  .swAct button { width: 66px; height: 66px; border-radius: 22px; font-size: 13px; font-weight: 600; color: var(--text);
    background: rgba(255,255,255,.08); border: .5px solid rgba(255,255,255,.14); box-shadow: inset 0 .5px 0 rgba(255,255,255,.2);
    -webkit-backdrop-filter: blur(20px) saturate(170%); backdrop-filter: blur(20px) saturate(170%); }
  .swAct button.hide { background: rgba(232,180,138,.22); color: var(--warm); }
  html.light .swAct button { background: rgba(255,255,255,.7); border-color: rgba(0,0,0,.06); }
  .swAct button:active { transform: scale(.94); }
  /* 右滑返回时，对话页左边缘带一道阴影，像一张纸被拉开 */
  body.swiping #main { box-shadow: -18px 0 40px rgba(0,0,0,calc(.35 * (1 - var(--swipe, 0)))); }`;
  document.head.appendChild(css);
})();
