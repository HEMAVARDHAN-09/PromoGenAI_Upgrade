'use strict';
/* PromoGen AI - single-page frontend (vanilla JS). Talks to the FastAPI backend under /api. */

// Use the current origin for the app's HTTP and HTTPS servers; Live Server calls the HTTP backend directly.
const sameOriginServer = location.protocol.startsWith('http') && (['8000', '8443'].includes(location.port) || location.port === '');
const API = window.PROMOGEN_API || (sameOriginServer ? '/api' : 'http://127.0.0.1:8000/api');

const state = { token: localStorage.getItem('pg_token'), user: null, caps: null, page: 'dashboard', product: null, project: null, pollTimer: null, blobs: new Map() };
const app = document.querySelector('#app');
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
const STATUS = { draft: 'Draft', script_generated: 'Script ready', script_edited: 'Edited - needs approval', approved: 'Approved', video_ready: 'Video ready' };
const fmtDate = s => { try { return new Date(s).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); } catch { return ''; } };
const ICON = {
  dashboard: 'M3 13h8V3H3zm0 8h8v-6H3zm10 0h8V11h-8zm0-18v6h8V3z', products: 'M20 7l-8-4-8 4v10l8 4 8-4zM4 7l8 4m0 0l8-4m-8 4v10',
  script: 'M4 4h12l4 4v12H4zM8 12h8M8 16h5', studio: 'M4 5h16v14H4zM10 9l5 3-5 3z', projects: 'M3 7h18v12H3zM3 7l2-3h5l2 3',
  scripts: 'M6 3h9l4 4v14H6zM9 12h7M9 16h7', videos: 'M3 6h13v12H3zM16 10l5-3v10l-5-3', settings: 'M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9L7 7M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1',
};
const icon = n => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${ICON[n]}"/></svg>`;

/* ---------------------------------------------------------------- helpers */
function toast(msg, bad = false) {
  const e = document.createElement('div');
  e.className = 'toast ' + (bad ? 'bad' : 'good'); e.textContent = msg; e.setAttribute('role', bad ? 'alert' : 'status');
  $('#toast').append(e); setTimeout(() => e.remove(), bad ? 6000 : 3500);
}

async function api(path, opts = {}) {
  opts.headers = { ...(opts.headers || {}) };
  if (state.token) opts.headers.Authorization = 'Bearer ' + state.token;
  if (opts.body && !(opts.body instanceof FormData)) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(opts.body); }
  let r;
  try { r = await fetch(API + path, opts); }
  catch { throw Error('Cannot reach the PromoGen server. Make sure the backend is running at http://127.0.0.1:8000'); }
  if (r.status === 401 && state.token && !path.startsWith('/auth/')) { logout(true); throw Error('Your session expired. Please log in again.'); }
  if (!r.ok) {
    let d = {}; try { d = await r.json(); } catch { /* not JSON */ }
    let msg = d.detail;
    if (Array.isArray(msg)) msg = msg.map(x => (x.loc?.slice(-1)[0] ? x.loc.slice(-1)[0] + ': ' : '') + (x.msg || '').replace(/^Value error, /, '')).join('; ');
    throw Error(typeof msg === 'string' && msg ? msg : `Request failed (${r.status})`);
  }
  return r.json();
}

/** Fetch a protected file (needs the bearer token) and return a blob: URL. Cached per path. */
async function blobUrl(path) {
  if (state.blobs.has(path)) return state.blobs.get(path);
  const r = await fetch(API + path, { headers: { Authorization: 'Bearer ' + state.token } });
  if (!r.ok) throw Error('Could not load file');
  const u = URL.createObjectURL(await r.blob());
  state.blobs.set(path, u); return u;
}
function hydrateAuthMedia(root = document) {
  $$('[data-auth-src]', root).forEach(async el => {
    try { el.src = await blobUrl(el.dataset.authSrc); } catch { el.remove(); }
  });
}
document.addEventListener('error', e => { if (e.target.matches?.('.pimg img')) e.target.remove(); }, true);

const opts = (list, val) => list.map(x => { const [v, l] = Array.isArray(x) ? x : [x, x]; return `<option value="${esc(v)}" ${String(v) === String(val) ? 'selected' : ''}>${esc(l)}</option>`; }).join('');
const initials = n => String(n || '?').split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();
function productImage(p, cls = '') {
  let img = '';
  if (p.image) img = p.image.startsWith('/api/') ? `<img data-auth-src="${esc(p.image.slice(4))}" alt="">` : `<img src="${esc(p.image)}" alt="" loading="lazy" referrerpolicy="no-referrer">`;
  return `<div class="pimg ${cls}"><span>${esc(initials(p.name))}</span>${img}</div>`;
}
const mediaKind = f => (/\.(mp4|webm|mov)$/i.test(f || '') ? 'video' : 'image');

function stopPoll() { clearTimeout(state.pollTimer); state.pollTimer = null; }

/* ---------------------------------------------------------------- voice input (real speech-to-text only) */
async function micInto(btn, field, language) {
  if (btn._rec) { btn._rec.stop(); return; }
  if (!state.caps) state.caps = await api('/capabilities').catch(() => null);
  if (!state.caps?.stt) return toast('Speech-to-text is not configured on this server (set STT_API_KEY in backend/.env and restart). Please type instead.', true);
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') return toast('This browser cannot record audio. Please type instead.', true);
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch { return toast('Microphone access was blocked. Allow it in the browser address bar, or type instead.', true); }
  const chunks = [], rec = new MediaRecorder(stream), label = btn.textContent;
  btn._rec = rec; btn.classList.add('rec'); btn.textContent = 'Stop recording';
  rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  rec.onstop = async () => {
    stream.getTracks().forEach(t => t.stop()); btn._rec = null; btn.classList.remove('rec'); btn.textContent = 'Transcribing...'; btn.disabled = true;
    const fd = new FormData(); fd.append('file', new Blob(chunks, { type: rec.mimeType || 'audio/webm' }), 'speech.webm'); if (language) fd.append('language', language);
    try { const d = await api('/speech/transcribe', { method: 'POST', body: fd }); field.value = (field.value.trim() ? field.value.trim() + ' ' : '') + d.text; toast('Transcribed. Please read and correct the text.'); }
    catch (e) { toast(e.message, true); }
    btn.disabled = false; btn.textContent = label;
  };
  rec.start(); toast('Recording... click the button again to stop.');
}
const micBtn = id => `<button type="button" class="btn mic" id="${id}">Speak</button>`;

/* ---------------------------------------------------------------- auth */
function authView(mode = 'login') {
  const reg = mode === 'register';
  app.innerHTML = `<main class="auth"><section class="auth-card">
    <div class="brand"><span class="logo">${icon('studio').replace('<svg', '<svg width="18" height="18"')}</span>PromoGen AI</div>
    <h1>${reg ? 'Create your account' : 'Welcome back'}</h1>
    <p class="muted">Turn any product or service into a script and a real promotional video.</p>
    <form id="authForm" novalidate>
      ${reg ? '<div class="field"><label for="fn">Full name</label><input id="fn" name="full_name" autocomplete="name" required minlength="2"></div>' : ''}
      <div class="field"><label for="em">Email address</label><input id="em" name="email" type="email" autocomplete="email" required></div>
      <div class="field"><label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="${reg ? 'new-password' : 'current-password'}" required minlength="8">${reg ? '<small>At least 8 characters, including a letter and a number.</small>' : ''}</div>
      ${reg ? '<div class="field"><label for="cp">Confirm password</label><input id="cp" name="confirm_password" type="password" autocomplete="new-password" required></div>' : ''}
      <button class="btn primary block">${reg ? 'Create account' : 'Log in'}</button>
    </form>
    <p class="muted" style="margin-top:16px">${reg ? 'Already registered? <a href="#" id="swap">Log in</a>' : 'New here? <a href="#" id="swap">Create an account</a>'}</p>
  </section></main>`;
  $('#swap').onclick = e => { e.preventDefault(); authView(reg ? 'login' : 'register'); };
  $('#authForm').onsubmit = async e => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target)); const btn = $('button', e.target);
    if (reg) {
      if (f.full_name.trim().length < 2) return toast('Please enter your full name', true);
      if (!/^\S+@\S+\.\S+$/.test(f.email)) return toast('Please enter a valid email address', true);
      if (f.password.length < 8 || !/[A-Za-z]/.test(f.password) || !/\d/.test(f.password)) return toast('Password needs 8+ characters with a letter and a number', true);
      if (f.password !== f.confirm_password) return toast('Passwords do not match', true);
    }
    btn.disabled = true;
    try {
      if (reg) { await api('/auth/register', { method: 'POST', body: f }); toast('Account created. Please log in.'); authView('login'); }
      else { const d = await api('/auth/login', { method: 'POST', body: f }); state.token = d.access_token; localStorage.setItem('pg_token', d.access_token); await boot(); }
    } catch (x) { toast(x.message, true); btn.disabled = false; }
  };
}

function logout(expired = false) {
  stopPoll();
  if (state.token && !expired) fetch(API + '/auth/logout', { method: 'POST', headers: { Authorization: 'Bearer ' + state.token } }).catch(() => {});
  state.blobs.forEach(u => URL.revokeObjectURL(u)); state.blobs.clear();
  Object.assign(state, { token: null, user: null, product: null, project: null });
  localStorage.removeItem('pg_token'); location.hash = ''; authView('login');
  if (expired) toast('Your session expired. Please log in again.', true);
}

/* ---------------------------------------------------------------- shell */
const NAV = [['dashboard', 'Dashboard'], ['products', 'Explore Products'], ['script', 'Script Generator'], ['studio', 'Video Studio'], ['sep'], ['projects', 'My Projects'], ['scripts', 'Saved Scripts'], ['videos', 'Video History'], ['sep'], ['settings', 'Settings']];
const TITLES = { dashboard: 'Dashboard', products: 'Explore Products', script: 'AI Script Generator', studio: 'Video Studio', projects: 'My Projects', scripts: 'Saved Scripts', videos: 'Video History', settings: 'Settings' };

function shell(content) {
  app.innerHTML = `<div class="shell"><aside class="sidebar" id="side">
    <div class="brand"><span class="logo">${icon('studio').replace('<svg', '<svg width="18" height="18"')}</span>PromoGen AI</div>
    <nav class="nav" aria-label="Main">${NAV.map(([p, l]) => p === 'sep' ? '<div class="sep"></div>' : `<button data-page="${p}" class="${state.page === p ? 'active' : ''}" ${state.page === p ? 'aria-current="page"' : ''}>${icon(p)}${l}</button>`).join('')}</nav>
    <div class="side-foot"><b>${esc(state.user?.full_name)}</b><div class="muted" style="word-break:break-all">${esc(state.user?.email)}</div><button class="btn sm block" id="logout" style="margin-top:10px">Log out</button></div>
  </aside><main class="main"><header class="topbar"><button class="btn ghost hamb" id="hamb" aria-label="Menu">&#9776;</button><div><span class="muted">Workspace</span><h2>${TITLES[state.page] || 'PromoGen AI'}</h2></div></header><div id="content">${content}</div></main></div>`;
  $$('[data-page]').forEach(b => b.onclick = () => go(b.dataset.page));
  $('#logout').onclick = () => logout();
  $('#hamb').onclick = () => $('#side').classList.toggle('open');
  hydrateAuthMedia(app);
}

const PAGES = {};
async function go(page) {
  stopPoll(); state.page = page; location.hash = page; window.scrollTo(0, 0);
  try { await PAGES[page](); } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------------- dashboard */
function projectCard(p) {
  const canStudio = ['approved', 'video_ready'].includes(p.status);
  return `<article class="card"><span class="badge ${p.status === 'video_ready' ? 'good' : p.status === 'script_edited' ? 'warn' : ''}">${STATUS[p.status] || esc(p.status)}</span>
    <h3>${esc(p.title)}</h3><p class="muted">${esc(p.product?.name || 'Custom campaign')} &middot; ${fmtDate(p.updated_at)}</p>
    <div class="row">${p.script ? `<button class="btn sm" data-act="open" data-id="${p.id}">Open script</button>` : ''}${canStudio ? `<button class="btn sm primary" data-act="studio" data-id="${p.id}">${p.video_filename ? 'Preview video' : 'Video studio'}</button>` : ''}${p.video_filename ? `<button class="btn sm" data-act="download" data-id="${p.id}">Download MP4</button>` : ''}<button class="btn sm danger" data-act="delete" data-id="${p.id}">Delete</button></div></article>`;
}
function projectRow(p, video) {
  return `<div class="list-row"><div><b>${esc(p.title)}</b><small>${esc(p.product?.name || '')} &middot; ${fmtDate(p.updated_at)}</small></div><div class="row">${video ? `<button class="btn sm primary" data-act="studio" data-id="${p.id}">Preview</button><button class="btn sm" data-act="download" data-id="${p.id}">Download</button>` : `<button class="btn sm" data-act="open" data-id="${p.id}">Open</button>`}</div></div>`;
}
function bindProjectActions(root = document) {
  $$('[data-act]', root).forEach(b => b.onclick = async () => {
    const id = +b.dataset.id, act = b.dataset.act;
    try {
      if (act === 'open') await openProject(id, 'script');
      else if (act === 'studio') await openProject(id, 'studio');
      else if (act === 'download') await downloadVideo(id);
      else if (act === 'delete') {
        if (!confirm('Delete this project, its script and its rendered video? This cannot be undone.')) return;
        await api('/projects/' + id, { method: 'DELETE' });
        if (state.project?.id === id) state.project = null;
        toast('Project deleted'); go(state.page);
      }
    } catch (e) { toast(e.message, true); }
  });
}
async function openProject(id, target) {
  state.project = await api('/projects/' + id); state.product = state.project.product; await go(target);
}
async function downloadVideo(id) {
  const r = await fetch(`${API}/projects/${id}/video?download=true`, { headers: { Authorization: 'Bearer ' + state.token } });
  if (!r.ok) throw Error('Download failed. The video file may be missing - render it again.');
  const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob());
  a.download = (r.headers.get('content-disposition')?.match(/filename="?([^";]+)/)?.[1]) || `promogen-${id}.mp4`;
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

PAGES.dashboard = async () => {
  const d = await api('/dashboard');
  const s = d.stats;
  shell(`<section class="hero"><span class="badge">AI campaign workspace</span><h1 style="margin-top:12px">Welcome, ${esc(state.user.full_name.split(' ')[0])}</h1>
    <p class="muted">Pick a product or describe your own, generate a script, edit the storyboard and render a real MP4.</p>
    <div class="row"><button class="btn primary" id="start">Start a new campaign</button><button class="btn" id="mine">Promote my own product</button></div></section>
    <section class="grid stats section">${[['Projects', s.projects], ['Scripts generated', s.scripts], ['Approved scripts', s.approved], ['Rendered videos', s.videos], ['My products', s.products]].map(x => `<div class="card stat"><span class="muted">${x[0]}</span><br><strong>${x[1]}</strong></div>`).join('')}</section>
    <section class="section"><h2>Recent projects</h2>${d.recent.length ? `<div class="grid products">${d.recent.map(projectCard).join('')}</div>` : '<div class="card empty">No campaigns yet. Choose a product to begin.</div>'}</section>
    <section class="grid two section"><div class="card"><h2>Saved scripts</h2>${d.scripts.map(p => projectRow(p, false)).join('') || '<p class="muted">Generated scripts are saved here automatically.</p>'}</div>
    <div class="card"><h2>Video history</h2>${d.videos.map(p => projectRow(p, true)).join('') || '<p class="muted">Rendered videos will appear here.</p>'}</div></section>`);
  $('#start').onclick = () => go('products'); $('#mine').onclick = () => ownProductModal();
  bindProjectActions();
};

/* ---------------------------------------------------------------- products */
function productCard(p) {
  return `<article class="card product-card" data-id="${esc(p.id)}" tabindex="0" role="button" aria-label="View ${esc(p.name)}">${productImage(p)}<div class="body"><div class="row spread"><span class="badge">${esc(p.category)}</span>${p.sample ? '<span class="badge good">Sample</span>' : ''}${p.price ? `<span class="price">${esc(p.price)}</span>` : ''}</div>
    <h3>${esc(p.name)}</h3><div class="muted">${esc(p.brand || 'Your brand')}</div><p>${esc(p.description)}</p><button class="btn primary sm" data-select="${esc(p.id)}">Select product</button></div></article>`;
}
function selectProduct(p) {
  state.product = p; state.project = null; go('script');
}

PAGES.products = async () => {
  shell(`<div class="toolbar"><input id="q" class="input" placeholder="Search by product, brand or category" aria-label="Search products"><select id="cat" aria-label="Category"><option value="">All categories</option></select><input id="brand" placeholder="Brand" aria-label="Brand"><button class="btn primary" id="own">Promote my own product</button></div>
    <section id="mineBox" class="hidden"><h2>My products</h2><div id="mineGrid" class="grid products"></div><div class="section"></div></section>
    <h2 id="catTitle">Product catalog</h2><div id="grid" class="grid products"></div>`);
  let items = [], mine = [];
  const bind = root => {
    $$('.product-card', root).forEach(c => {
      const open = () => productDetail([...items, ...mine].find(x => String(x.id) === c.dataset.id));
      c.onclick = open; c.onkeydown = e => { if (e.key === 'Enter' && e.target === c) open(); };
    });
    $$('[data-select]', root).forEach(b => b.onclick = e => { e.stopPropagation(); selectProduct([...items, ...mine].find(x => String(x.id) === b.dataset.select)); });
  };
  const load = async () => {
    try {
      const d = await api(`/products?q=${encodeURIComponent($('#q').value)}&category=${encodeURIComponent($('#cat').value)}&brand=${encodeURIComponent($('#brand').value)}`);
      if (!$('#cat').dataset.ready) { $('#cat').innerHTML = '<option value="">All categories</option>' + d.categories.map(x => `<option>${esc(x)}</option>`).join(''); $('#cat').dataset.ready = 1; }
      items = d.items; $('#catTitle').textContent = `Product catalog (${d.total})`;
      $('#grid').innerHTML = items.map(productCard).join('') || '<div class="card empty">No matching products. Try another search, or promote your own product.</div>';
      bind($('#grid')); hydrateAuthMedia($('#grid'));
    } catch (e) { toast(e.message, true); }
  };
  let t; const debounce = () => { clearTimeout(t); t = setTimeout(load, 250); };
  $('#q').oninput = $('#brand').oninput = debounce; $('#cat').onchange = load; $('#own').onclick = () => ownProductModal();
  try {
    mine = (await api('/products/mine')).items;
    if (mine.length) {
      $('#mineBox').classList.remove('hidden'); $('#mineGrid').innerHTML = mine.map(p => productCard(p).replace('</div></article>', `<button class="btn sm danger" data-del="${esc(p.id)}">Remove</button></div></article>`)).join('');
      bind($('#mineGrid')); hydrateAuthMedia($('#mineGrid'));
      $$('[data-del]').forEach(b => b.onclick = async e => { e.stopPropagation(); if (!confirm('Remove this product?')) return; try { await api('/products/custom/' + b.dataset.del.replace('custom-', ''), { method: 'DELETE' }); go('products'); } catch (x) { toast(x.message, true); } });
    }
  } catch (e) { toast(e.message, true); }
  load();
};

function openModal(html) {
  const m = document.createElement('div'); m.className = 'modal'; m.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true">${html}</div>`;
  const close = () => { m.remove(); document.removeEventListener('keydown', esc_); };
  const esc_ = e => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', esc_); m.addEventListener('mousedown', e => { if (e.target === m) close(); });
  document.body.append(m); hydrateAuthMedia(m); return { el: m, close };
}

function productDetail(p) {
  if (!p) return;
  const m = openModal(`<div class="row spread"><span class="badge">${esc(p.category)}</span><button class="btn ghost sm" data-close>Close</button></div>
    ${productImage(p, 'detail-img')}<h2>${esc(p.name)}</h2><p class="muted">${esc(p.brand || '')}${p.price ? ' &middot; <span class="price">' + esc(p.price) + '</span>' : ''}</p><p>${esc(p.description)}</p>
    ${(p.features || []).length ? `<h3>Key features</h3><div class="chips">${p.features.map(f => `<span class="chip">${esc(f)}</span>`).join('')}</div>` : ''}
    ${p.target_audience ? `<p class="muted" style="margin-top:14px">Target audience: ${esc(p.target_audience)}</p>` : ''}
    <div class="row" style="margin-top:20px"><button class="btn primary" data-pick>Create campaign for this product</button></div>`);
  $('[data-close]', m.el).onclick = m.close; $('[data-pick]', m.el).onclick = () => { m.close(); selectProduct(p); };
}

function ownProductModal() {
  const m = openModal(`<form id="ownForm"><div class="row spread"><h2>Promote your own product or service</h2><button type="button" class="btn ghost sm" data-close>Close</button></div>
    <p class="muted">Works for any product or service: cleaners, skincare, electronics, food, clothing, apps, classes and more.</p>
    <div class="field"><label>Product or service name *</label><input name="name" required maxlength="120"></div>
    <div class="grid two"><div class="field"><label>Brand</label><input name="brand" maxlength="120"></div><div class="field"><label>Price</label><input name="price" maxlength="60" placeholder="e.g. $49 or From Rs 999"></div></div>
    <div class="field"><label>Category / industry *</label><input name="category" required maxlength="120" list="cats" placeholder="e.g. Home cleaning, Skincare, Restaurant"><datalist id="cats">${['Electronics', 'Fashion and clothing', 'Beauty and personal care', 'Food and beverages', 'Home appliances', 'Fitness and lifestyle', 'Education and digital services', 'Travel and hospitality', 'Software and apps', 'Local services'].map(c => `<option value="${c}">`).join('')}</datalist></div>
    <div class="field"><div class="label-row"><label for="od">Description *</label>${micBtn('micDesc')}</div><textarea id="od" name="description" required maxlength="1500" placeholder="What is it and why do customers love it? You can also speak it."></textarea></div>
    <div class="field"><label>Key features and benefits (one per line, or comma separated)</label><textarea name="features" maxlength="2000"></textarea></div>
    <div class="grid two"><div class="field"><label>Target audience</label><input name="target_audience" maxlength="300"></div><div class="field"><label>Marketing objective</label><input name="campaign_objective" maxlength="300" placeholder="e.g. Increase sign-ups"></div></div>
    <div class="grid two"><div class="field"><label>Product image (optional)</label><input name="image" type="file" accept="image/png,image/jpeg,image/webp"></div>
    <div class="field"><label>Logo (optional)</label><input name="logo" type="file" accept="image/png,image/jpeg,image/webp"></div></div>
    <button class="btn primary block">Save and create campaign</button></form>`);
  $('[data-close]', m.el).onclick = m.close;
  $('#micDesc').onclick = () => micInto($('#micDesc'), $('#od'), (state.user.preferences || {}).language || 'English');
  $('#ownForm').onsubmit = async e => {
    e.preventDefault(); const fd = new FormData(e.target);
    ['image', 'logo'].forEach(k => { if (!fd.get(k)?.size) fd.delete(k); });
    const btn = $('button.primary', e.target); btn.disabled = true;
    try { const p = await api('/products/custom', { method: 'POST', body: fd }); m.close(); toast('Product saved'); selectProduct(p); }
    catch (x) { toast(x.message, true); btn.disabled = false; }
  };
}

/* ---------------------------------------------------------------- script generator */
const PLATFORMS = ['Instagram Reels', 'YouTube Shorts', 'TikTok', 'Facebook Reels', 'LinkedIn video'];
const TONES = ['Professional', 'Funny', 'Emotional', 'Energetic', 'Luxury', 'Educational', 'Friendly'];
const LANGS = ['English', 'Hindi', 'Telugu', 'Tamil', 'Kannada', 'Malayalam', 'Marathi', 'Bengali', 'Spanish', 'French', 'German', 'Arabic'];
const OBJECTIVES = ['Increase sales', 'Build brand awareness', 'Generate leads', 'Promote a product launch', 'Drive app installs', 'Grow event registrations'];

PAGES.script = async () => {
  if (!state.product && !state.project) {
    shell('<div class="card empty">Select a product first, then come back to generate a script.<br><br><button class="btn primary" id="choose">Explore products</button></div>');
    $('#choose').onclick = () => go('products'); return;
  }
  if (!state.caps) state.caps = await api('/capabilities').catch(() => null);
  const p = state.product || state.project.product;
  const prefs = state.user.preferences || {}; const c = state.project?.config || {};
  const v = (k, d) => c[k] ?? d;
  shell(`<div class="grid two" style="align-items:start"><section class="card"><span class="badge">${esc(p.category)}</span><h2 style="margin-top:10px">${esc(p.name)}</h2><p class="muted">${esc(p.description)}</p>
    ${state.caps && !state.caps.llm ? '<div class="warn-box" style="margin-bottom:14px"><b>Demo mode.</b> No LLM key is configured, so scripts are generated from a built-in template using your inputs. Add <code>LLM_API_KEY</code> to <code>backend/.env</code> for live AI scripts.</div>' : ''}
    <form id="scriptForm"><div class="field"><label>Target audience</label><input name="target_audience" value="${esc(v('target_audience', p.target_audience || 'General consumers'))}"></div>
    <div class="field"><label>Marketing objective</label><input name="marketing_objective" list="objs" value="${esc(v('marketing_objective', p.campaign_objective || 'Increase sales'))}"><datalist id="objs">${OBJECTIVES.map(o => `<option value="${o}">`).join('')}</datalist></div>
    <div class="grid two"><div class="field"><label>Platform</label><select name="platform">${opts(PLATFORMS, v('platform', prefs.platform || PLATFORMS[0]))}</select></div>
    <div class="field"><label>Duration</label><select name="duration">${opts([[15, '15 seconds'], [30, '30 seconds'], [45, '45 seconds'], [60, '60 seconds']], v('duration', 30))}</select></div></div>
    <div class="grid two"><div class="field"><label>Tone</label><select name="tone">${opts(TONES, v('tone', prefs.tone || 'Energetic'))}</select></div>
    <div class="field"><label>Language</label><select name="language" id="langSel">${opts(LANGS, v('language', prefs.language || 'English'))}</select></div></div><div id="langHint" class="hidden"></div>
    <div class="field"><label>Call to action</label><input name="call_to_action" value="${esc(v('call_to_action', 'Order today'))}"></div>
    <div class="field"><label>Key selling points and benefits (one per line)</label><textarea name="key_selling_points">${esc(v('key_selling_points', (p.features || []).join('\n')))}</textarea></div>
    <div class="field"><div class="label-row"><label for="instr">Extra instructions (optional)</label>${micBtn('micInstr')}</div><textarea id="instr" name="instructions" placeholder="e.g. mention the free delivery offer">${esc(v('instructions', ''))}</textarea></div>
    <button class="btn primary block" id="genBtn">${state.project?.script ? 'Regenerate script' : 'Generate AI script'}</button></form></section>
    <section id="editor">${state.project?.script ? editorHtml(state.project.script) : '<div class="card empty">Your script and storyboard will appear here.</div>'}</section></div>`);
  $('#scriptForm').onsubmit = generate; bindEditor();
  $('#micInstr').onclick = () => micInto($('#micInstr'), $('#instr'), $('#langSel').value);
  $('#langSel').onchange = langHint; langHint();
};

function langHint() {
  const el = $('#langHint'), c = state.caps; if (!el) return; const lang = $('#langSel').value; el.className = 'hidden'; el.style.marginBottom = '14px';
  if (!c) return;
  if (!c.llm && !c.demo_languages.includes(lang)) { el.className = 'warn-box'; el.innerHTML = `<b>${esc(lang)} needs a live AI key.</b> Demo mode only has built-in wording for ${c.demo_languages.map(esc).join(' and ')}, so the script would be written in English. Add <code>LLM_API_KEY</code> to <code>backend/.env</code> for a real ${esc(lang)} script.`; }
  else if (c.llm && lang !== 'English') { el.className = 'info-box'; el.textContent = `The AI will write this script in ${lang}. Please proofread it before approving - AI-written ${lang} can contain mistakes.`; }
}
async function generate(e) {
  e.preventDefault();
  if (state.project?.script && !confirm('Regenerate the script? Your current edits and any rendered video for this project will be replaced.')) return;
  const f = Object.fromEntries(new FormData(e.target)); f.duration = +f.duration;
  f.product = state.product || state.project.product; if (state.project) f.project_id = state.project.id;
  const btn = $('#genBtn'); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Generating...';
  try {
    const d = await api('/scripts/generate', { method: 'POST', body: f });
    state.project = await api('/projects/' + d.project_id); state.product = state.project.product;
    toast(d.script.mode === 'demo' ? (d.script.notice || 'Script generated in demo mode (no LLM key configured) - it is a template, not AI-written') : 'AI script generated', d.script.mode === 'demo' && !!d.script.notice);
    await PAGES.script();
  } catch (x) { toast(x.message, true); btn.disabled = false; btn.textContent = state.project?.script ? 'Regenerate script' : 'Generate AI script'; }
}

function editorHtml(s) {
  const total = s.scenes.reduce((a, x) => a + (+x.duration || 0), 0), demo = s.mode === 'demo';
  return `<div class="stack"><div class="card"><div class="row spread"><div class="row"><span class="badge ${demo ? 'warn' : 'good'}">${demo ? 'Demo mode - template script, not written by an AI' : 'Live AI-generated'}</span><span class="badge">${esc(s.language || 'English')}</span></div><span class="muted" id="total">Total ${total}s</span></div>
    ${s.notice ? `<div class="warn-box" style="margin-top:12px">${esc(s.notice)}</div>` : ''}
    <div class="field" style="margin-top:12px"><label>Campaign title</label><input data-root="campaign_title" value="${esc(s.campaign_title)}"></div>
    <div class="field"><label>Hook (attention-grabbing opening)</label><textarea data-root="hook">${esc(s.hook)}</textarea></div>
    <div class="field"><label>Introduction</label><textarea data-root="introduction">${esc(s.introduction || '')}</textarea></div>
    <div class="field"><label>Product benefits (one per line)</label><textarea data-root="benefits">${esc((s.benefits || []).join('\n'))}</textarea></div>
    <div class="field"><label>Full promotional script (narration)</label><textarea data-root="complete_script" style="min-height:130px">${esc(s.complete_script)}</textarea></div></div>
    <div class="row spread"><h2 style="margin:0">Storyboard</h2><button class="btn sm" id="addScene">+ Add scene</button></div>
    <div id="scenes" class="stack">${s.scenes.map(sceneHtml).join('')}</div>
    <div class="card"><div class="field"><label>Social caption</label><textarea data-root="caption">${esc(s.caption)}</textarea></div>
    <div class="field"><label>Hashtags (comma separated)</label><input data-root="hashtags" value="${esc((s.hashtags || []).join(', '))}"></div>
    <div class="grid two"><div class="field"><label>Tagline</label><input data-root="tagline" value="${esc(s.tagline)}"></div><div class="field"><label>Final call to action</label><input data-root="final_cta" value="${esc(s.final_cta)}"></div></div></div>
    <div class="script-tools row"><button class="btn" id="copy">Copy script</button><button class="btn" id="saveScript">Save</button><button class="btn" id="regen">Regenerate</button><span style="flex:1"></span><button class="btn primary" id="approve">Approve and continue to video</button></div></div>`;
}
function sceneHtml(x, i) {
  return `<div class="card scene" data-scene-card="${i}"><div><div class="num">${i + 1}</div><div class="time" data-time="${i}">${x.start}-${x.end}s</div><div class="field" style="margin-top:10px"><label>Seconds</label><input type="number" min="1" max="20" data-scene="${i}" data-key="duration" value="${esc(x.duration)}"></div>
    <button class="btn sm danger" data-rm="${i}">Remove</button></div>
    <div><div class="field"><label>Scene title</label><input data-scene="${i}" data-key="title" value="${esc(x.title)}"></div>
    <div class="field"><label>Visual direction</label><textarea data-scene="${i}" data-key="visual">${esc(x.visual)}</textarea></div>
    <div class="field"><label>Voiceover narration (spoken)</label><textarea data-scene="${i}" data-key="voiceover">${esc(x.voiceover)}</textarea></div>
    <div class="field"><label>Caption (subtitle shown on screen)</label><textarea data-scene="${i}" data-key="caption" style="min-height:62px">${esc(x.caption ?? x.voiceover)}</textarea></div>
    <div class="grid two"><div class="field"><label>On-screen text</label><input data-scene="${i}" data-key="onscreen_text" value="${esc(x.onscreen_text)}"></div>
    <div class="field"><label>Transition</label><select data-scene="${i}" data-key="transition">${opts([['fade', 'Fade'], ['cut', 'Hard cut']], x.transition)}</select></div></div></div></div>`;
}
function recalcTimes(s) {
  let t = 0; s.scenes.forEach((x, i) => { x.start = t; t += (+x.duration || 1); x.end = t; const el = $(`[data-time="${i}"]`); if (el) el.textContent = `${x.start}-${x.end}s`; });
  const tot = $('#total'); if (tot) tot.textContent = `Total ${t}s`;
}
/** Copy the edited form fields back into the in-memory script. */
function syncEditor() {
  const s = state.project?.script; if (!s) return null;
  $$('[data-root]').forEach(e => { s[e.dataset.root] = e.dataset.root === 'hashtags' ? e.value.split(/[,\s]+/).filter(Boolean).map(h => h.startsWith('#') ? h : '#' + h) : e.dataset.root === 'benefits' ? e.value.split('\n').map(x => x.trim()).filter(Boolean) : e.value; });
  $$('[data-scene]').forEach(e => { const sc = s.scenes[+e.dataset.scene]; if (sc) sc[e.dataset.key] = e.type === 'number' ? Math.max(1, Math.min(20, +e.value || 1)) : e.value; });
  s.scenes.forEach((x, i) => x.id = i + 1); recalcTimes(s); return s;
}
function scriptAsText(s) {
  return [`${s.campaign_title}`, '', `HOOK: ${s.hook}`, `INTRODUCTION: ${s.introduction || ''}`, `BENEFITS: ${(s.benefits || []).join('; ')}`, '', 'SCRIPT:', s.complete_script, '', 'STORYBOARD:',
    ...s.scenes.map((x, i) => `${i + 1}. [${x.start}-${x.end}s] ${x.title}\n   Visual: ${x.visual}\n   Voiceover: ${x.voiceover}\n   On-screen: ${x.onscreen_text}\n   Caption: ${x.caption || x.voiceover}`),
    '', `CAPTION: ${s.caption}`, `HASHTAGS: ${(s.hashtags || []).join(' ')}`, `TAGLINE: ${s.tagline}`, `CTA: ${s.final_cta}`].join('\n');
}
async function saveScript() { const s = syncEditor(); await api(`/projects/${state.project.id}/script`, { method: 'PUT', body: { script: s } }); state.project.status = 'script_edited'; }

function bindEditor() {
  const s = state.project?.script; if (!s) return;
  $('#copy').onclick = async () => {
    const text = scriptAsText(syncEditor());
    try { await navigator.clipboard.writeText(text); toast('Script copied to clipboard'); }
    catch { const t = document.createElement('textarea'); t.value = text; document.body.append(t); t.select(); document.execCommand('copy'); t.remove(); toast('Script copied to clipboard'); }
  };
  $('#saveScript').onclick = async () => { try { await saveScript(); toast('Script saved'); } catch (e) { toast(e.message, true); } };
  $('#regen').onclick = () => $('#scriptForm').requestSubmit();
  $('#approve').onclick = async () => {
    try { await saveScript(); await api(`/projects/${state.project.id}/approve`, { method: 'POST' }); state.project = await api('/projects/' + state.project.id); toast('Script approved'); go('studio'); }
    catch (e) { toast(e.message, true); }
  };
  $('#addScene').onclick = () => {
    syncEditor(); const last = s.scenes[s.scenes.length - 1];
    s.scenes.push({ id: s.scenes.length + 1, title: `Scene ${s.scenes.length + 1}`, visual: '', voiceover: '', caption: '', onscreen_text: '', duration: 4, transition: 'fade', media: null, start: last ? last.end : 0, end: 0 });
    refreshScenes();
  };
  $$('[data-rm]').forEach(b => b.onclick = () => { syncEditor(); if (s.scenes.length <= 1) return toast('A script needs at least one scene', true); s.scenes.splice(+b.dataset.rm, 1); refreshScenes(); });
  $$('[data-key="duration"]').forEach(i => i.oninput = () => syncEditor());
}
function refreshScenes() { const s = state.project.script; recalcTimes(s); $('#scenes').innerHTML = s.scenes.map(sceneHtml).join(''); bindEditor(); }

/* ---------------------------------------------------------------- video studio */
const SCRIPT_RANGE = { telugu: [0x0C00, 0x0C7F], kannada: [0x0C80, 0x0CFF], tamil: [0x0B80, 0x0BFF], malayalam: [0x0D00, 0x0D7F], bengali: [0x0980, 0x09FF], devanagari: [0x0900, 0x097F], arabic: [0x0600, 0x06FF] };
function neededScripts(s) {
  const out = new Set();
  for (const ch of s.scenes.map(x => [x.title, x.onscreen_text, x.voiceover, x.caption].join(' ')).join(' ')) {
    const o = ch.codePointAt(0); for (const [k, [lo, hi]] of Object.entries(SCRIPT_RANGE)) if (o >= lo && o <= hi) out.add(k);
  }
  return [...out];
}
function voiceChoices(caps, lang) {
  const code = (caps.lang_codes || {})[lang] || 'en', match = v => String(v.lang || '').toLowerCase().replace('_', '-').startsWith(code);
  const sys = caps.voices.filter(v => v.id.startsWith('system:')), list = [['none', 'No voiceover (captions only)']];
  sys.filter(match).forEach(v => list.push([v.id, `${v.label} - matches ${lang}`]));
  caps.voices.filter(v => v.id === 'system').forEach(v => list.push([v.id, 'System default voice (language not guaranteed)']));
  sys.filter(v => !match(v)).forEach(v => list.push([v.id, `${v.label} - not ${lang}`]));
  caps.voices.filter(v => v.id.startsWith('api')).forEach(v => list.push([v.id, v.label]));
  return { list, firstMatch: sys.find(match)?.id || 'none', hasMatch: sys.some(match) };
}
function voiceNote(caps, lang, id, hasMatch) {
  if (id === 'none') return ['info-box', 'Captions only - the video will have no audio track.'];
  if (id.startsWith('api')) return ['info-box', `AI voice: quality for ${lang} depends on your TTS provider. Listen to the finished video before sharing it.`];
  if (id.startsWith('system:') && hasMatch && (caps.voices.find(v => v.id === id)?.label || '').includes('matches')) return ['info-box', `An installed voice for ${lang} will be used.`];
  const code = (caps.lang_codes || {})[lang] || 'en', ok = caps.voices.find(v => v.id === id && String(v.lang || '').toLowerCase().replace('_', '-').startsWith(code));
  if (ok) return ['info-box', `An installed voice for ${lang} will be used.`];
  return ['warn-box', `This voice is probably not a ${lang} voice, so pronunciation may be wrong or unintelligible. ${hasMatch ? 'Pick the voice marked "matches".' : `No installed voice matches ${lang}. Install one (Windows: Settings > Time & Language > Speech) or set TTS_API_KEY in backend/.env.`}`];
}

PAGES.studio = async () => {
  if (!state.project) { shell('<div class="card empty">Open a project with an approved script to use the Video Studio.<br><br><button class="btn primary" id="go">My projects</button></div>'); $('#go').onclick = () => go('projects'); return; }
  state.project = await api('/projects/' + state.project.id);
  const pr = state.project, s = pr.script;
  if (!s) { shell('<div class="card empty">This project has no script yet. Generate one first.</div>'); return; }
  if (!['approved', 'video_ready'].includes(pr.status)) {
    shell(`<div class="card empty">This script has not been approved yet. Review it, then approve it to unlock video rendering.${pr.video_filename ? '<br><small>A video from an earlier version of this script exists and can still be downloaded.</small>' : ''}<br><br><div class="row" style="justify-content:center"><button class="btn" id="back">Review script</button><button class="btn primary" id="appr">Approve script now</button>${pr.video_filename ? '<button class="btn" id="oldv">Download earlier video</button>' : ''}</div></div>`);
    $('#back').onclick = () => go('script');
    $('#appr').onclick = async () => { try { await api(`/projects/${pr.id}/approve`, { method: 'POST' }); await go('studio'); } catch (e) { toast(e.message, true); } };
    if ($('#oldv')) $('#oldv').onclick = () => downloadVideo(pr.id).catch(e => toast(e.message, true));
    return;
  }
  if (!state.caps) state.caps = await api('/capabilities').catch(() => null);
  const caps = state.caps || { voices: [{ id: 'none', label: 'No voiceover (captions only)' }], ffmpeg: true, fonts: {}, lang_codes: {} };
  const lang = s.language || 'English', vc = voiceChoices(caps, lang), missing = neededScripts(s).filter(k => caps.fonts && caps.fonts[k] === false);
  const hasImg = String(pr.product?.image || '').startsWith('/api/uploads/'), hasLogo = String(pr.product?.logo || '').startsWith('/api/uploads/');
  const blocked = !caps.ffmpeg || missing.length > 0;
  shell(`<section class="card"><div class="row spread"><div><span class="badge">Video Studio</span><h2 style="margin-top:8px">${esc(s.campaign_title)}</h2></div><div class="row"><span class="badge">${esc(lang)}</span><span class="badge ${pr.video_filename ? 'good' : ''}">${pr.video_filename ? 'Video ready' : 'Not rendered yet'}</span></div></div>
    ${caps.ffmpeg ? '' : '<div class="warn-box" style="margin:12px 0"><b>FFmpeg was not found on the server.</b> Install FFmpeg (<code>winget install Gyan.FFmpeg</code>), restart PowerShell and the backend, then reload this page.</div>'}
    ${missing.length ? `<div class="warn-box" style="margin:12px 0"><b>Missing font for ${missing.map(m => m[0].toUpperCase() + m.slice(1)).join(', ')}.</b> Without it the video text would be empty boxes, so rendering is disabled. Download "Noto Sans ${missing.map(m => m[0].toUpperCase() + m.slice(1))[0]}" (free, fonts.google.com/noto), copy the .ttf into <code>backend/fonts/</code>, restart the server and reload this page.</div>` : ''}
    <p class="code-note">Each scene becomes a real video segment: your uploaded image or clip (or the product image, or a charcoal and gold card), on-screen text, optional burned-in captions and a fade or cut. The wording is locked to the script you approved. <a href="#" id="editScript">Edit the script</a> to change it.</p>
    <div class="settings-bar" style="margin-top:14px"><div class="field"><label>Aspect ratio</label><select id="aspect">${opts([['9:16', '9:16 vertical'], ['1:1', '1:1 square'], ['16:9', '16:9 landscape']], '9:16')}</select></div>
    <div class="field"><label>Resolution</label><select id="res">${opts(['720p', '1080p'], '720p')}</select></div>
    <div class="field"><label>Voiceover (${esc(lang)})</label><select id="voice">${opts(vc.list, vc.firstMatch)}</select></div>
    <div class="field"><label>&nbsp;</label><label class="check"><input type="checkbox" id="captions" checked> Burn in captions</label></div></div>
    <div id="voiceNote" style="margin-bottom:12px"></div>
    <div class="row" style="margin-bottom:12px">${hasImg ? '<label class="check"><input type="checkbox" id="useImg" checked> Use product image on scenes without their own media</label>' : ''}${hasLogo ? '<label class="check"><input type="checkbox" id="useLogo" checked> Show logo</label>' : ''}</div>
    <div class="row"><button class="btn" id="saveScenes">Save scene edits</button><button class="btn primary" id="render" ${blocked ? 'disabled' : ''}>Render MP4</button></div>
    <div id="progress" aria-live="polite"></div></section>
    <section class="card section"><div class="row spread"><h2 style="margin:0">Scene timeline</h2><span class="muted" id="total"></span></div><div class="timeline" id="timeline"></div><small>Click a segment to jump to the scene. If a voiceover is longer than its scene, the scene is extended automatically.</small><div class="studio-scenes" id="sceneList"></div></section>
    <section class="card section" id="previewBox"><h2>Preview</h2><div id="preview"><p class="muted">Render the video to preview and download it here.</p></div></section>`);
  const note = () => { const [cls, txt] = voiceNote(caps, lang, $('#voice').value, vc.hasMatch); $('#voiceNote').innerHTML = `<div class="${cls}">${esc(txt)}</div>`; };
  $('#voice').onchange = note; note();
  $('#editScript').onclick = e => { e.preventDefault(); go('script'); };
  renderSceneList(); renderTimeline();
  $('#saveScenes').onclick = async () => { try { syncStudio(); await api(`/projects/${pr.id}/scenes`, { method: 'PUT', body: { scenes: state.project.script.scenes } }); toast('Scene edits saved'); } catch (e) { toast(e.message, true); } };
  $('#render').onclick = startRender;
  if (pr.render_status === 'rendering') { $('#render').disabled = true; poll(); }
  else if (pr.render_status === 'failed' && pr.render_error) $('#progress').innerHTML = `<div class="warn-box" style="margin-top:12px"><b>The last render failed:</b> ${esc(pr.render_error)}</div>`;
  if (pr.video_filename && pr.render_status !== 'rendering') showVideo();
};

function renderSceneList() {
  const s = state.project.script;
  $('#sceneList').innerHTML = s.scenes.map((x, i) => `<div class="sscene" id="sc${i}"><div><div class="thumb" data-thumb="${i}">${x.media ? '' : '<span>Charcoal card or product image<br>(upload an image or clip)</span>'}</div></div>
    <div><div class="row spread"><b>Scene ${i + 1} - ${esc(x.title)}</b><span class="badge">${x.start}-${x.end}s</span></div>
    <div class="grid two"><div class="field"><label>On-screen text (from approved script)</label><div class="readonly">${esc(x.onscreen_text)}</div></div>
    <div class="grid two"><div class="field"><label>Seconds</label><input type="number" min="1" max="20" data-i="${i}" data-k="duration" value="${esc(x.duration)}"></div><div class="field"><label>Transition</label><select data-i="${i}" data-k="transition">${opts([['fade', 'Fade'], ['cut', 'Cut']], x.transition)}</select></div></div></div>
    <div class="field"><label>Voiceover (from approved script)</label><div class="readonly">${esc(x.voiceover)}</div></div>
    <div class="row"><label class="btn sm">Upload image or video<input type="file" data-up="${i}" accept="image/png,image/jpeg,image/webp,video/mp4,video/webm,video/quicktime" hidden></label>${x.media ? `<button class="btn sm danger" data-clear="${i}">Remove media</button>` : ''}</div></div></div>`).join('');
  s.scenes.forEach(async (x, i) => {
    if (!x.media) return; const th = $(`[data-thumb="${i}"]`);
    try {
      const url = await blobUrl('/uploads/' + x.media);
      th.innerHTML = mediaKind(x.media) === 'video' ? `<video src="${url}" muted loop playsinline></video>` : `<img src="${url}" alt="Scene ${i + 1} background">`;
      const v = $('video', th); if (v) { th.onmouseenter = () => v.play(); th.onmouseleave = () => v.pause(); }
    } catch { th.innerHTML = '<span>Media unavailable</span>'; }
  });
  $$('#sceneList [data-k]').forEach(el => el.oninput = el.onchange = () => { syncStudio(); renderTimeline(); });
  $$('[data-clear]').forEach(b => b.onclick = () => { syncStudio(); state.project.script.scenes[+b.dataset.clear].media = null; renderSceneList(); });
  $$('[data-up]').forEach(inp => inp.onchange = async () => {
    const file = inp.files[0]; if (!file) return; const i = +inp.dataset.up; syncStudio();
    const fd = new FormData(); fd.append('file', file); const th = $(`[data-thumb="${i}"]`); th.innerHTML = '<span class="spinner"></span>';
    try { const d = await api('/media', { method: 'POST', body: fd }); state.project.script.scenes[i].media = d.filename; toast(`${d.type === 'video' ? 'Video clip' : 'Image'} added to scene ${i + 1}`); }
    catch (e) { toast(e.message, true); }
    renderSceneList();
  });
}
function syncStudio() {
  const s = state.project.script;
  $$('#sceneList [data-k]').forEach(el => { const sc = s.scenes[+el.dataset.i]; if (sc) sc[el.dataset.k] = el.type === 'number' ? Math.max(1, Math.min(20, +el.value || 1)) : el.value; });
  recalcTimes(s);
  $$('#sceneList .sscene').forEach((row, i) => { const b = $('.badge', row); if (b && s.scenes[i]) b.textContent = `${s.scenes[i].start}-${s.scenes[i].end}s`; });
}
function renderTimeline() {
  const s = state.project.script;
  $('#timeline').innerHTML = s.scenes.map((x, i) => `<div class="seg" style="flex-grow:${x.duration}" data-seg="${i}" title="${esc(x.onscreen_text || x.title)}">${i + 1}</div>`).join('');
  $('#total').textContent = `Total ${s.scenes.reduce((a, x) => a + (+x.duration || 0), 0)}s`;
  $$('[data-seg]').forEach(el => el.onclick = () => $('#sc' + el.dataset.seg).scrollIntoView({ behavior: 'smooth', block: 'center' }));
}

async function startRender() {
  const btn = $('#render'); btn.disabled = true;
  try {
    syncStudio();
    await api(`/projects/${state.project.id}/render`, { method: 'POST', body: { scenes: state.project.script.scenes, aspect_ratio: $('#aspect').value, resolution: $('#res').value, voice: $('#voice').value, captions: $('#captions').checked, use_product_image: $('#useImg') ? $('#useImg').checked : false, use_logo: $('#useLogo') ? $('#useLogo').checked : false } });
    $('#preview').innerHTML = '<p class="muted">Rendering - the previous video (if any) stays available until the new one is ready.</p>';
    poll();
  } catch (e) { toast(e.message, true); btn.disabled = false; }
}
async function poll() {
  stopPoll();
  try {
    const d = await api(`/projects/${state.project.id}/render-status`);
    const box = $('#progress'); if (!box) return;
    if (d.status === 'rendering') {
      box.innerHTML = `<p style="margin-top:14px"><b>Rendering...</b> <span class="muted">${esc(d.progress || '')}</span></p><div class="progress"><i></i></div>`;
      state.pollTimer = setTimeout(poll, 1500);
    } else if (d.status === 'completed') {
      box.innerHTML = '<p class="badge good" style="margin-top:12px">Render complete</p>'; $('#render').disabled = false;
      state.project = await api('/projects/' + state.project.id); showVideo(true); toast('Your MP4 is ready');
    } else if (d.status === 'failed') {
      box.innerHTML = `<div class="warn-box" style="margin-top:12px"><b>Rendering failed:</b> ${esc(d.error || 'unknown error')}</div>`; $('#render').disabled = false; toast('Rendering failed', true);
    }
  } catch (e) { toast(e.message, true); const r = $('#render'); if (r) r.disabled = false; }
}
async function showVideo(fresh = false) {
  const box = $('#preview'); if (!box) return;
  box.innerHTML = '<span class="spinner"></span>';
  try {
    const path = `/projects/${state.project.id}/video`; if (fresh) { state.blobs.delete(path); }
    const url = await blobUrl(path);
    box.innerHTML = `<video class="video ${$('#aspect')?.value === '16:9' ? 'wide' : ''}" controls playsinline src="${url}"></video><div class="row"><button class="btn primary" id="download">Download MP4</button><span class="muted">Rendered ${fmtDate(state.project.updated_at)}</span></div>`;
    $('#download').onclick = () => downloadVideo(state.project.id).catch(e => toast(e.message, true));
  } catch (e) { box.innerHTML = `<div class="warn-box">The video file could not be loaded: ${esc(e.message)}. Try rendering again.</div>`; }
}

/* ---------------------------------------------------------------- project lists */
PAGES.projects = PAGES.scripts = PAGES.videos = async () => {
  const kind = state.page === 'projects' ? '' : state.page;
  const empty = { '': 'No saved projects yet.', scripts: 'No saved scripts yet. Generate one from a product.', videos: 'No rendered videos yet. Approve a script and render it in the Video Studio.' }[kind];
  shell(`<div class="toolbar" style="grid-template-columns:2fr 1fr auto"><input id="pq" placeholder="Search by title" aria-label="Search projects"><select id="ps" aria-label="Status"><option value="">All statuses</option>${opts(Object.entries(STATUS).filter(([k]) => k !== 'draft'), '')}</select><button class="btn primary" id="newp">New campaign</button></div><div class="grid products" id="plist"></div>`);
  const load = async () => {
    const d = await api(`/projects?q=${encodeURIComponent($('#pq').value)}&status=${encodeURIComponent($('#ps').value)}&kind=${kind}`);
    $('#plist').innerHTML = d.items.map(projectCard).join('') || `<div class="card empty" style="grid-column:1/-1">${empty}</div>`; bindProjectActions($('#plist'));
  };
  let t; $('#pq').oninput = () => { clearTimeout(t); t = setTimeout(() => load().catch(e => toast(e.message, true)), 250); }; $('#ps').onchange = () => load().catch(e => toast(e.message, true));
  $('#newp').onclick = () => go('products'); await load();
};

/* ---------------------------------------------------------------- settings */
PAGES.settings = async () => {
  state.caps = await api('/capabilities').catch(() => state.caps); const c = state.caps; const pf = state.user.preferences || {};
  shell(`<div class="grid two" style="align-items:start"><form class="card" id="profile"><h2>Profile and defaults</h2><div class="field"><label>Full name</label><input name="full_name" value="${esc(state.user.full_name)}" required minlength="2"></div>
    <div class="field"><label>Email</label><input value="${esc(state.user.email)}" disabled></div>
    <div class="field"><label>Default platform</label><select name="platform">${opts(PLATFORMS, pf.platform || PLATFORMS[0])}</select></div>
    <div class="field"><label>Default tone</label><select name="tone">${opts(TONES, pf.tone || 'Energetic')}</select></div>
    <div class="field"><label>Default language</label><select name="language">${opts(LANGS, pf.language || 'English')}</select></div><button class="btn primary">Save profile</button></form>
    <div class="stack"><form class="card" id="password"><h2>Change password</h2><div class="field"><label>Current password</label><input type="password" name="current_password" autocomplete="current-password" required></div>
    <div class="field"><label>New password</label><input type="password" name="new_password" autocomplete="new-password" minlength="8" required></div><button class="btn primary">Change password</button></form>
    <div class="card"><h2>System status</h2>${c ? `<div class="list-row"><span>Script generation</span><span class="badge ${c.llm ? 'good' : 'warn'}">${c.llm ? 'Live AI (' + esc(c.llm_model) + ')' : 'Demo mode'}</span></div>
    <div class="list-row"><span>FFmpeg (video rendering)</span><span class="badge ${c.ffmpeg ? 'good' : 'bad'}">${c.ffmpeg ? 'Available' : 'Not found'}</span></div>
    <div class="list-row"><span>Voiceover options</span><span class="muted" style="text-align:right">${c.voices.filter(v => v.id !== 'none').map(v => esc(v.label)).join(', ') || 'None available'}</span></div>
    <div class="list-row"><span>Speech-to-text (microphone)</span><span class="badge ${c.stt ? 'good' : 'warn'}">${c.stt ? 'Configured' : 'Not configured'}</span></div>
    <div class="list-row"><span>Video text fonts</span><span class="muted" style="text-align:right">${Object.entries(c.fonts || {}).map(([k, ok]) => `${esc(k)}: ${ok ? 'yes' : 'missing'}`).join(', ')}</span></div>` : '<p class="muted">Status unavailable.</p>'}</div></div></div>`);
  $('#profile').onsubmit = async e => {
    e.preventDefault(); const f = Object.fromEntries(new FormData(e.target));
    try { await api('/me', { method: 'PUT', body: { full_name: f.full_name, preferences: { platform: f.platform, tone: f.tone, language: f.language } } }); state.user = await api('/me'); toast('Profile saved'); PAGES.settings(); }
    catch (x) { toast(x.message, true); }
  };
  $('#password').onsubmit = async e => {
    e.preventDefault();
    try { await api('/me/password', { method: 'PUT', body: Object.fromEntries(new FormData(e.target)) }); e.target.reset(); toast('Password changed'); } catch (x) { toast(x.message, true); }
  };
};

/* ---------------------------------------------------------------- boot */
async function boot() {
  if (!state.token) return authView('login');
  try { state.user = await api('/me'); }
  catch (e) { if (state.token) { stopPoll(); localStorage.removeItem('pg_token'); state.token = null; authView('login'); toast(e.message, true); } return; }
  const hash = location.hash.slice(1);
  await go(PAGES[hash] && !['script', 'studio'].includes(hash) ? hash : 'dashboard');
}
boot();
