// 派活页的小音效：不放音频文件，全部用 Web Audio 现场合成，几十毫秒、音量很低，像 iOS 的键盘声那种「有但不吵」。
// 不用到处埋点：页面里本来就在该震的地方调 haptic(tap/confirm/reject/long)，这里把 haptic 包一层，震的同时配一声。
// 「外观」面板里加一个开关，存 localStorage 'sfx'，默认开。
(() => {
  let ctx = null, master = null;
  const on = () => { try { return localStorage.getItem('sfx') !== '0'; } catch (e) { return true; } };
  function ac() {
    if (!ctx) {
      const C = window.AudioContext || window.webkitAudioContext; if (!C) return null;
      ctx = new C(); master = ctx.createGain(); master.gain.value = 0.5; master.connect(ctx.destination);
    }
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }
  // 浏览器要求先有一次用户操作才能出声；第一次碰屏幕时就把声卡叫醒，后面「做完了」那种不是点出来的提示音才响得了
  addEventListener('pointerdown', () => { if (on()) ac(); }, { once: true, capture: true });

  // 一个音：正弦/三角波，快起慢收，避免「啪」的爆音
  function tone(f, t0, dur, vol, type = 'sine', f2) {
    const c = ac(); if (!c) return;
    const o = c.createOscillator(), g = c.createGain(), t = c.currentTime + t0;
    o.type = type; o.frequency.setValueAtTime(f, t);
    if (f2) o.frequency.exponentialRampToValueAtTime(f2, t + dur);
    g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(vol, t + 0.006);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g); g.connect(master); o.start(t); o.stop(t + dur + 0.02);
  }
  // 一阵带通滤过的白噪声，从低扫到高 = 发送时「嗖」一下
  function whoosh() {
    const c = ac(); if (!c) return;
    const n = c.sampleRate * 0.28, b = c.createBuffer(1, n, c.sampleRate), d = b.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
    const s = c.createBufferSource(), f = c.createBiquadFilter(), g = c.createGain(), t = c.currentTime;
    f.type = 'bandpass'; f.Q.value = 1.4; f.frequency.setValueAtTime(500, t); f.frequency.exponentialRampToValueAtTime(3200, t + 0.24);
    g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(0.09, t + 0.05); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.28);
    s.buffer = b; s.connect(f); f.connect(g); g.connect(master); s.start(t); s.stop(t + 0.3);
  }
  const SND = {
    tap: () => tone(2100, 0, 0.03, 0.035, 'sine', 1500),                              // 轻轻一「嗒」
    confirm: () => { tone(1046, 0, 0.18, 0.05); tone(1568, 0.07, 0.26, 0.045); },       // 两个玻璃音往上走：成了
    reject: () => { tone(240, 0, 0.14, 0.07, 'triangle', 170); },                       // 闷一下：没成
    long: () => { tone(784, 0, 0.5, 0.04); tone(1175, 0.09, 0.6, 0.035); tone(1568, 0.18, 0.7, 0.025); }, // 小钟：活干完了 / 长按
    send: whoosh,
  };
  window.sfx = k => { if (on() && SND[k]) try { SND[k](); } catch (e) {} };

  const h = window.haptic;
  window.haptic = function (k) { sfx(k); return h && h.apply(this, arguments); };

  // 发送按钮：本来没有震动，单独配一声「嗖」
  document.addEventListener('click', e => { if (e.target.closest('#send, .askSend')) sfx('send'); }, true);

  // 外观面板里加开关
  const sh = document.querySelector('#thSheet > div');
  if (sh) {
    const row = document.createElement('div');
    row.innerHTML = `<h2 style="margin-top:18px">声音</h2><div class="seg" id="sfxSeg"><button data-v="1">开</button><button data-v="0">关</button></div>
      <div class="hint">点按、完成、发送时的轻提示音，音量跟着手机的媒体音量走。</div>`;
    sh.appendChild(row);
    const paint = () => row.querySelectorAll('button').forEach(b => b.classList.toggle('on', (b.dataset.v === '1') === on()));
    paint();
    row.querySelector('#sfxSeg').onclick = e => {
      const b = e.target.closest('button'); if (!b) return;
      try { localStorage.setItem('sfx', b.dataset.v); } catch (er) {}
      paint(); if (b.dataset.v === '1') sfx('confirm');
    };
  }
})();
