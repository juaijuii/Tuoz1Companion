// 头像墙的界面。数据全从本机后台（avatar_picker.py）拿；会改东西的请求带上页面里的那串钥匙。
(() => {
  'use strict';
  const KEY = document.querySelector('meta[name=key]').content;
  const $ = (id) => document.getElementById(id);
  const api = async (path, body) => {
    const res = await fetch(path, body === undefined
      ? { headers: { 'X-Key': KEY } }
      : { method: 'POST', headers: { 'X-Key': KEY, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const X = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="m3 3 6 6M9 3 3 9"/></svg>';
  const STAR = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m8 2 1.8 3.8 4.2.5-3.1 2.9.8 4.1L8 11.3l-3.7 2 .8-4.1L2 6.3l4.2-.5z"/></svg>';
  const MARK = '<span class="mark"><svg class="ok" viewBox="0 0 16 16"><path d="m3.5 8.5 3 3 6-7"/></svg><svg class="no" viewBox="0 0 16 16"><path d="m4 4 8 8M12 4l-8 8"/></svg></span>';

  let icons = [], byId = new Map(), sets = [];
  let state = null;
  const filter = { view: 'all', set: null, year: null, q: '' };
  let shown = [];                       // 现在墙上该有的头像（筛过、排好）
  let cols = 9, rowH = 140, first = -1, last = -1, firstPaint = true;
  const live = new Map();               // 编号 -> 已经在页面上的格子
  const wall = $('wall'), grid = $('grid'), spacer = $('spacer');

  // ---------- 墙：只把看得见的那几行（上下各多 3 行）放进页面 ----------
  function measure() {
    const w = wall.clientWidth;
    cols = Math.max(3, Math.floor((w + 1) / 95));
    const tile = (w - (cols - 1)) / cols;
    rowH = Math.round(tile - 20 + 10 + 7 + 14 + 31 + 8) + 1;      // 图 + 上边距 + 编号 + 两行名字 + 下边距 + 接缝
    grid.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
    grid.style.gridAutoRows = (rowH - 1) + 'px';
  }

  function makeTile(icon, row) {
    const el = document.createElement('div');
    el.className = 'tile';
    el.dataset.id = icon.id;
    el.innerHTML = `<button type="button" class="pick" aria-label="换成 ${esc(icon.t)}（${icon.id} 号）" title="${esc(icon.t)}">` +
      `<span class="ph"><img alt="" decoding="async" src="/icon/${icon.id}.jpg"></span>` +
      `<span class="no">${icon.id}</span><span class="nm">${esc(icon.t)}</span></button>` +
      `<button type="button" class="star" aria-label="收藏" aria-pressed="false">${STAR}</button>${MARK}`;
    const img = el.querySelector('img'), ph = img.parentNode;
    const ready = () => ph.classList.add('in');
    if (img.complete && img.naturalWidth) ready(); else {
      img.addEventListener('load', ready, { once: true });
      img.addEventListener('error', () => { ph.classList.add('miss'); img.remove(); }, { once: true });
    }
    if (firstPaint && row < 7) { el.classList.add('rise'); el.style.animationDelay = (row * 28) + 'ms'; }
    paintTile(el, icon.id);
    return el;
  }

  function paintTile(el, id) {
    if (!state) return;
    el.classList.toggle('cur', state.desired === id || (state.desired == null && state.chat_icon === id));
    el.querySelector('.star').setAttribute('aria-pressed', state.favs.includes(id));
  }

  function render(force) {
    const rows = Math.ceil(shown.length / cols);
    spacer.style.height = Math.max(rows * rowH, 0) + 'px';
    const a = Math.max(0, Math.floor(wall.scrollTop / rowH) - 3);
    const b = Math.min(rows, Math.ceil((wall.scrollTop + wall.clientHeight) / rowH) + 3);
    if (!force && a === first && b === last) return;
    first = a; last = b;
    const want = shown.slice(a * cols, b * cols);
    const keep = new Set(want.map((i) => i.id));
    for (const [id, el] of live) if (!keep.has(id)) { el.remove(); live.delete(id); }
    grid.querySelectorAll('.blank, .skel').forEach((e) => e.remove());
    let prev = null;
    want.forEach((icon, n) => {
      let el = live.get(icon.id);
      if (!el) { el = makeTile(icon, Math.floor(n / cols)); live.set(icon.id, el); }
      const next = prev ? prev.nextSibling : grid.firstChild;
      if (next !== el) grid.insertBefore(el, next);
      prev = el;
    });
    for (let n = want.length; n % cols; n++) grid.appendChild(Object.assign(document.createElement('div'), { className: 'tile blank' }));
    grid.style.transform = `translateY(${a * rowH}px)`;
    firstPaint = false;
  }

  function skeleton() {
    measure();
    const n = cols * (Math.ceil(wall.clientHeight / rowH) + 1);
    grid.innerHTML = Array.from({ length: n }, () => '<div class="tile skel"><span class="ph"></span><i></i><i></i></div>').join('');
    spacer.style.height = wall.clientHeight + 'px';
  }

  // ---------- 筛选 ----------
  function refilter(keepScroll) {
    const q = filter.q.trim().toLowerCase();
    let list;
    if (filter.view === 'fav') list = (state ? state.favs : []).map((id) => byId.get(id)).filter(Boolean);
    else if (filter.view === 'recent') list = (state ? state.recent : []).map((id) => byId.get(id)).filter(Boolean);
    else list = icons;
    shown = list.filter((i) => (filter.set == null || i.s.includes(filter.set)) && (filter.year == null || i.y === filter.year)
      && (!q || i.t.toLowerCase().includes(q) || String(i.id).startsWith(q)));
    const order = { all: '从新到旧', fav: '按收藏的先后', recent: '按用过的先后' }[filter.view];
    $('count').innerHTML = icons.length ? `<span class="num">${shown.length}</span> 个 · ${order}` : '';
    paintFilters();
    paintEmpty();
    if (!keepScroll) wall.scrollTop = 0;
    render(true);
  }

  function paintFilters() {
    document.querySelectorAll('#views button').forEach((b) => b.setAttribute('aria-current', b.dataset.view === filter.view));
    document.querySelectorAll('#sets button').forEach((b) => b.setAttribute('aria-current', +b.dataset.set === filter.set));
    document.querySelectorAll('#years button').forEach((b) => b.setAttribute('aria-current', +b.dataset.year === filter.year));
    $('view-select').value = filter.view;
    $('set-select').value = filter.set == null ? '' : filter.set;
    $('year-select').value = filter.year == null ? '' : filter.year;
    const chips = [];
    if (filter.set != null) chips.push(`<button type="button" data-clear="set" aria-label="不再只看这个系列">系列：${esc(sets[filter.set].name)}${X}</button>`);
    if (filter.year != null) chips.push(`<button type="button" data-clear="year" aria-label="不再只看这一年">${filter.year} 年${X}</button>`);
    $('chips').innerHTML = chips.join('');
  }

  function paintEmpty() {
    const box = $('empty');
    let html = '';
    if (!icons.length) {
      html = state && state.connected
        ? '<h3>正在读头像目录</h3><p>客户端里有五千多个头像，第一次读要几秒。</p>'
        : '<h3>先把英雄联盟客户端开到大厅</h3><p>头像目录和头像图都是从你电脑上的客户端里读的。连上以后，头像墙会自己出现。</p>';
    } else if (!shown.length) {
      if (filter.view === 'fav' && !filter.q && filter.set == null && filter.year == null) html = '<h3>还没有收藏</h3><p>鼠标移到墙上的头像上，点右上角的星，它就会出现在这里。</p><button type="button" data-clear="all">回到全部头像</button>';
      else if (filter.view === 'recent' && !filter.q && filter.set == null && filter.year == null) html = '<h3>还没换过头像</h3><p>换过的头像会按先后记在这里，最多 12 个。</p><button type="button" data-clear="all">回到全部头像</button>';
      else html = `<h3>没有对得上的头像</h3><p>${filter.q ? `名字里没有「${esc(filter.q)}」，编号也不是它开头的。` : '这几个条件凑在一起，一个头像都没有。'}换个词，或者清掉筛选。</p><button type="button" data-clear="all">清掉搜索和筛选</button>`;
    }
    box.innerHTML = html;
    box.hidden = !html;
    grid.hidden = !!html && !!icons.length;
  }

  function buildFacets() {
    const order = sets.map((s, n) => ({ ...s, n0: n })).filter((s) => s.n > 0).sort((a, b) => b.n - a.n);
    const TOP = 7, rest = order.length - TOP;
    $('sets').innerHTML = order.map((s, n) => `<li${n >= TOP ? ' class="x"' : ''}><button type="button" data-set="${s.n0}"><span>${esc(s.name)}</span><b>${s.n}</b></button></li>`).join('')
      + (rest > 0 ? `<li><button type="button" class="more" data-more="${rest}" aria-expanded="false"><span>还有 ${rest} 个</span><svg viewBox="0 0 12 12" aria-hidden="true"><path d="m2.5 4.5 3.5 3.5 3.5-3.5"/></svg></button></li>` : '');
    const years = new Map();
    for (const i of icons) if (i.y) years.set(i.y, (years.get(i.y) || 0) + 1);
    const ys = [...years].sort((a, b) => b[0] - a[0]);
    $('years').innerHTML = ys.map(([y, n]) => `<li><button type="button" data-year="${y}"><span>${y}</span><b>${n}</b></button></li>`).join('');
    $('set-select').innerHTML = '<option value="">系列：全部</option>' + order.map((s) => `<option value="${s.n0}">${esc(s.name)}（${s.n}）</option>`).join('');
    $('year-select').innerHTML = '<option value="">年份：全部</option>' + ys.map(([y, n]) => `<option value="${y}">${y}（${n}）</option>`).join('');
    $('n-all').textContent = icons.length;
  }

  async function loadCatalog() {
    const data = await api('/api/catalog');
    icons = data.icons || [];
    sets = data.sets || [];
    byId = new Map(icons.map((i) => [i.id, i]));
    if (icons.length) { grid.innerHTML = ''; live.clear(); first = last = -1; firstPaint = true; measure(); buildFacets(); }
    refilter();
    paintInspector(true);
  }

  // ---------- 右栏 ----------
  const TITLE = { on: '', reverted: '正在重设 · ', stopped: '停手了 · ', off: '被改回了 · ', idle: '', no_client: '客户端没开 · ' };
  let notice = null;                    // 一句要多留一会儿的提示：{text, until}
  const ST = { on: '已设上', reverted: '被改回了，正在重设', stopped: '停手了', off: '被改回了，没在重设', idle: '用的是你自己的头像', no_client: '客户端没开' };
  const KIND = { set: '换上', reset: '改回', stop: '停手', error: '出错', restore: '换回', offline: '离线', resume: '接上' };
  let faceId = null, logTop = null;

  function paintInspector(force) {
    if (!state) return;
    const id = state.desired != null ? state.desired : (state.chat_icon != null ? state.chat_icon : state.account_icon);
    const icon = id != null ? byId.get(id) : null;
    if (id !== faceId || force) {
      faceId = id;
      const face = $('face');
      face.querySelector('img')?.remove();
      $('cur-none').hidden = id != null;
      if (id != null) {
        const img = Object.assign(document.createElement('img'), { alt: '', className: 'swap', src: `/icon/${id}.jpg` });
        img.addEventListener('error', () => { img.remove(); $('cur-none').hidden = false; });
        face.prepend(img);
      }
      $('cur-name').textContent = icon ? icon.t : (id != null ? `${id} 号` : (state.connected ? '还没有头像' : '客户端没开'));
      $('cur-id').textContent = id != null ? id : '–';
      $('cur-year').textContent = icon && icon.y ? icon.y : '–';
      $('cur-set').textContent = icon && icon.s.length ? icon.s.map((n) => sets[n].name).join('、') : '–';
    }
    const owned = id != null && state.owned.includes(id);
    $('cur-own').textContent = id == null || !state.connected ? '–' : (owned ? '有' : '没有');
    $('cur-who').textContent = id == null || !state.connected ? '–' : (owned ? '所有人' : '好友（没实测过）');
    const again = state.status === 'stopped' ? '<button type="button" class="again" id="again">再试一次</button>' : '';
    if (notice && Date.now() > notice.until) notice = null;
    $('cur-status').innerHTML = notice ? `<span class="st" data-st="stopped">${notice.text}</span>`
      : `<span class="st" data-st="${state.status}">${ST[state.status]}</span>${again}`;
    $('face').dataset.st = state.status;
    document.title = TITLE[state.status] + '头像墙';
    $('n-reset').hidden = !state.resets || !state.log.length;
    $('n-reset').innerHTML = `重设 <span class="num">${state.resets || 0}</span> 次`;
    $('auto').setAttribute('aria-checked', state.auto);
    $('restore').disabled = state.desired == null;
    $('link').className = 'link' + (state.connected ? ' on' : '');
    $('link').lastElementChild.textContent = state.connected
      ? ['已连上客户端', state.region, state.phase].filter(Boolean).join(' · ') : '客户端没开，开了会自己连上';
    $('n-fav').textContent = state.favs.length;
    $('n-recent').textContent = state.recent.length;

    const top = state.log.length ? state.log[0].t + state.log[0].text : null;
    if (top !== logTop || force) {
      const fresh = logTop !== null && top !== logTop;
      logTop = top;
      $('log').innerHTML = state.log.map((e, n) => {
        const d = new Date(e.t * 1000), hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
        return `<li data-k="${e.kind}"${fresh && n === 0 ? ' class="new"' : ''}><time>${hm}</time><b>${KIND[e.kind] || ''}</b><span>${esc(e.text)}${e.kind === 'set' && byId.has(e.id) ? ' · ' + esc(byId.get(e.id).t) : ''}${e.kind === 'reset' ? '<em>已重设</em>' : ''}</span></li>`;
      }).join('');
      $('log-empty').hidden = state.log.length > 0;
      $('log').hidden = !state.log.length;
    }
  }

  function setState(s, repaintTiles) {
    const before = state;
    state = s;
    paintInspector();
    if (repaintTiles || !before || before.desired !== s.desired || before.chat_icon !== s.chat_icon || String(before.favs) !== String(s.favs))
      for (const [id, el] of live) paintTile(el, id);
    if (before && (filter.view === 'fav' && String(before.favs) !== String(s.favs) || filter.view === 'recent' && String(before.recent) !== String(s.recent))) refilter(true);
    if (!icons.length && s.icons) loadCatalog();
    if (!before || before.connected !== s.connected) paintEmpty();
  }

  async function poll() {
    try { setState(await api('/api/state')); } catch (e) { /* 后台关了：窗口也该关了 */ }
    setTimeout(poll, 1500);
  }

  // ---------- 动作 ----------
  async function choose(id) {
    const el = live.get(id);
    if (!state || !state.connected) {
      notice = { text: '客户端没开，开了再换', until: Date.now() + 4000 };
      if (state) paintInspector();
      if (el) { el.classList.add('fail'); setTimeout(() => el.classList.remove('fail'), 1100); }
      return;
    }
    notice = null;
    el?.classList.remove('done', 'fail');
    el?.classList.add('busy');
    let ok = false;
    try { const r = await api('/api/choose', { id }); ok = r.ok; setState(r.state, true); } catch (e) { /* 下面按没成处理 */ }
    if (el) {
      el.classList.remove('busy');
      el.classList.add(ok ? 'done' : 'fail');
      setTimeout(() => el.classList.remove('done', 'fail'), 1100);
    }
  }

  grid.addEventListener('click', (ev) => {
    const tile = ev.target.closest('.tile');
    if (!tile || !tile.dataset.id) return;
    const id = +tile.dataset.id;
    const star = ev.target.closest('.star');
    if (star) {
      const on = star.getAttribute('aria-pressed') !== 'true';
      star.setAttribute('aria-pressed', on);
      api('/api/fav', { id, on }).then((r) => setState(r.state)).catch(() => star.setAttribute('aria-pressed', !on));
    } else if (ev.target.closest('.pick')) choose(id);
  });

  // 方向键在墙上走格子，回车换上，F 收藏
  grid.addEventListener('keydown', (ev) => {
    const tile = ev.target.closest('.tile');
    if (!tile || !tile.dataset.id) return;
    const at = shown.findIndex((i) => i.id === +tile.dataset.id);
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols }[ev.key];
    if (step !== undefined) {
      ev.preventDefault();
      const to = at + step;
      if (to < 0 || to >= shown.length) return;
      const row = Math.floor(to / cols);
      if (row * rowH < wall.scrollTop) wall.scrollTop = row * rowH;
      else if ((row + 1) * rowH > wall.scrollTop + wall.clientHeight) wall.scrollTop = (row + 1) * rowH - wall.clientHeight;
      render();
      live.get(shown[to].id)?.querySelector('.pick').focus({ preventScroll: true });
    } else if (ev.key.toLowerCase() === 'f' && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
      tile.querySelector('.star').click();
    }
  });

  document.querySelector('.rail').addEventListener('click', (ev) => {
    const b = ev.target.closest('button');
    if (!b) return;
    if (b.dataset.more) {
      const open = $('sets').classList.toggle('open');
      b.setAttribute('aria-expanded', open);
      b.firstElementChild.textContent = open ? '收起' : `还有 ${b.dataset.more} 个`;
      return;
    }
    if (b.dataset.view) filter.view = b.dataset.view;
    else if (b.dataset.set) filter.set = filter.set === +b.dataset.set ? null : +b.dataset.set;
    else if (b.dataset.year) filter.year = filter.year === +b.dataset.year ? null : +b.dataset.year;
    refilter();
  });
  $('view-select').addEventListener('change', (ev) => { filter.view = ev.target.value; refilter(); });
  $('set-select').addEventListener('change', (ev) => { filter.set = ev.target.value === '' ? null : +ev.target.value; refilter(); });
  $('year-select').addEventListener('change', (ev) => { filter.year = ev.target.value === '' ? null : +ev.target.value; refilter(); });
  document.querySelector('.work').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-clear]');
    if (!b) return;
    if (b.dataset.clear === 'set' || b.dataset.clear === 'all') filter.set = null;
    if (b.dataset.clear === 'year' || b.dataset.clear === 'all') filter.year = null;
    if (b.dataset.clear === 'all') { filter.q = ''; $('q').value = ''; filter.view = 'all'; }
    refilter();
  });
  let typing = 0;
  $('q').addEventListener('input', (ev) => { clearTimeout(typing); typing = setTimeout(() => { filter.q = ev.target.value; refilter(); }, 90); });
  document.addEventListener('keydown', (ev) => {
    if ((ev.key === '/' || (ev.key.toLowerCase() === 'f' && (ev.ctrlKey || ev.metaKey))) && document.activeElement !== $('q')) { ev.preventDefault(); $('q').focus(); $('q').select(); }
    else if (ev.key === 'Escape' && document.activeElement === $('q') && $('q').value) { $('q').value = ''; filter.q = ''; refilter(); }
  });

  $('auto').addEventListener('click', () => {
    const sw = $('auto'), on = sw.getAttribute('aria-checked') !== 'true';
    sw.setAttribute('aria-checked', on);
    sw.classList.remove('moved'); void sw.offsetWidth; sw.classList.add('moved');
    api('/api/auto', { on }).then((r) => setState(r.state)).catch(() => sw.setAttribute('aria-checked', !on));
  });

  $('restore').addEventListener('click', () => api('/api/restore', {}).then((r) => setState(r.state, true)).catch(() => {}));
  $('cur-status').addEventListener('click', (ev) => { if (ev.target.id === 'again' && state.desired != null) choose(state.desired); });

  let ticking = false;
  wall.addEventListener('scroll', () => { if (!ticking) { ticking = true; requestAnimationFrame(() => { ticking = false; render(); }); } }, { passive: true });
  new ResizeObserver(() => { if (!icons.length) return; const c = cols; measure(); render(c !== cols || true); }).observe(wall);

  // ---------- 开始 ----------
  skeleton();
  api('/api/state').then((s) => { state = s; return loadCatalog(); }).catch(() => paintEmpty()).finally(poll);
})();
