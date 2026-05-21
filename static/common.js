// Shared helpers for both the home dashboard and the device detail page.

// ----- update banner -----
// Polls /api/update-check, shows a dismissible banner if a newer release exists.
// Dismissals are remembered per-version in localStorage so the same release
// doesn't nag, but future releases still surface a fresh banner.
//
// Two banner modes depending on whether the running app can self-install:
//   install_supported=true  → in-app "install & restart" button + progress
//   install_supported=false → fall back to the legacy "download" link
const UPDATE_DISMISS_KEY = 'updateBanner.dismissedVersions';

function _loadDismissedVersions() {
  try {
    const raw = localStorage.getItem(UPDATE_DISMISS_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch (e) { return []; }
}

function _saveDismissedVersion(version) {
  const arr = _loadDismissedVersions();
  if (!arr.includes(version)) arr.push(version);
  // Cap list growth — we only care about recent versions; keep last 20.
  while (arr.length > 20) arr.shift();
  try { localStorage.setItem(UPDATE_DISMISS_KEY, JSON.stringify(arr)); } catch (e) {}
}

function _fmtBytes(n) {
  if (!n || n < 1024) return (n || 0) + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function _pollInstallProgress(host) {
  const progressEl = host.querySelector('.progress');
  const installBtn = host.querySelector('.install-btn');
  const dismissBtn = host.querySelector('.dismiss');
  installBtn.disabled = true;
  installBtn.hidden = true;
  dismissBtn.hidden = true;
  progressEl.hidden = false;

  const tick = async () => {
    let s;
    try {
      const r = await fetch('/api/update-progress');
      if (!r.ok) throw new Error('progress fetch failed');
      s = await r.json();
    } catch (e) {
      // Probably means the app is restarting — that's the happy path.
      progressEl.textContent = 'restarting…';
      return;
    }
    switch (s.phase) {
      case 'downloading':
        progressEl.textContent = `downloading ${s.progress || 0}% (${_fmtBytes(s.downloaded_bytes)}/${_fmtBytes(s.total_bytes)})`;
        break;
      case 'verifying':
        progressEl.textContent = 'verifying signature…';
        break;
      case 'installing':
        progressEl.textContent = 'installing…';
        break;
      case 'relaunching':
        progressEl.textContent = 'restarting…';
        return;
      case 'failed':
        progressEl.hidden = true;
        installBtn.hidden = false;
        installBtn.disabled = false;
        dismissBtn.hidden = false;
        if (typeof toast === 'function') toast('Update failed: ' + (s.error || 'unknown error'), { variant: 'crit' });
        return;
    }
    setTimeout(tick, 500);
  };
  tick();
}

async function checkForUpdates() {
  const host = document.getElementById('update-banner');
  if (!host) return;
  let data;
  try {
    const r = await fetch('/api/update-check');
    if (!r.ok) return;
    data = await r.json();
  } catch (e) { return; }
  if (!data || !data.newer_available || !data.latest) return;
  // banner_recommended gates noisy patch releases (added v1.8.3). Pre-1.8.3
  // backends omit the field entirely, in which case treat as true so the
  // banner still surfaces (preserve old behavior for older installs).
  if (data.banner_recommended === false) return;
  if (_loadDismissedVersions().includes(data.latest)) return;

  host.querySelector('.version').textContent = 'v' + data.latest;
  const notes = host.querySelector('.notes-link');
  if (data.release_url) {
    notes.href = data.release_url;
    notes.hidden = false;
  } else {
    notes.hidden = true;
  }

  const installBtn = host.querySelector('.install-btn');
  const getBtn = host.querySelector('.get-btn');
  if (data.install_supported) {
    installBtn.hidden = false;
    getBtn.hidden = true;
    installBtn.addEventListener('click', async () => {
      installBtn.disabled = true;
      try {
        const r = await fetch('/api/update-install', { method: 'POST' });
        if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          if (typeof toast === 'function') toast('Update failed: ' + (body.error || r.status), { variant: 'crit' });
          installBtn.disabled = false;
          return;
        }
        _pollInstallProgress(host);
      } catch (e) {
        if (typeof toast === 'function') toast('Update failed: ' + e.message, { variant: 'crit' });
        installBtn.disabled = false;
      }
    });
  } else {
    installBtn.hidden = true;
    getBtn.hidden = false;
    getBtn.href = data.platform_download_url || 'https://bitaxeballer.com/';
  }

  host.querySelector('.dismiss').addEventListener('click', () => {
    _saveDismissedVersion(data.latest);
    host.hidden = true;
  });
  host.hidden = false;
}
window.addEventListener('DOMContentLoaded', checkForUpdates);

// ----- theme toggle -----
function applyThemeUI(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const icon = document.getElementById('theme-icon');
  const label = document.getElementById('theme-label');
  if (icon) icon.textContent = theme === 'light' ? '☀' : '🌙';
  if (label) label.textContent = theme;
}
window.addEventListener('DOMContentLoaded', () => {
  applyThemeUI(document.documentElement.getAttribute('data-theme') || 'dark');
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'dark';
      const next = cur === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('theme', next); } catch (e) {}
      applyThemeUI(next);
    });
  }
});

// ----- hashrate unit toggle (GH/s ↔ TH/s) -----
// Bitaxe firmware reports hashrate in GH/s natively. Some users prefer TH/s
// (1 TH = 1000 GH), especially with multiple devices or when comparing against
// pool dashboards that use TH/s. Stored per-browser in localStorage; affects
// display only — server-side logs (CSV, SQLite) always use GH/s.
function getHashrateUnit() {
  try {
    const u = localStorage.getItem('hashrateUnit');
    return u === 'TH' ? 'TH' : 'GH';
  } catch (e) { return 'GH'; }
}
function hashrateUnitLabel() {
  return getHashrateUnit() === 'TH' ? 'TH/s' : 'GH/s';
}
// Format a GH/s value for display. `precisionGh` = decimals when in GH mode.
// In TH mode we add 2 decimals so resolution stays comparable (1234.5 GH/s
// → 1.2345 TH/s, both ≈0.1 GH precision).
function fmtHashrate(ghs, precisionGh = 1) {
  if (ghs == null || !isFinite(Number(ghs))) return '—';
  const n = Number(ghs);
  if (getHashrateUnit() === 'TH') return (n / 1000).toFixed(precisionGh + 2);
  return n.toFixed(precisionGh);
}
function applyHashrateUnitUI(unit) {
  const btn = document.getElementById('hash-unit-toggle');
  if (!btn) return;
  const lbl = btn.querySelector('.lbl');
  if (lbl) lbl.textContent = unit === 'TH' ? 'TH/s' : 'GH/s';
  btn.setAttribute('aria-pressed', unit === 'TH' ? 'true' : 'false');
}
function _injectHashUnitButton() {
  const meta = document.querySelector('header .meta');
  if (!meta || document.getElementById('hash-unit-toggle')) return;
  const btn = document.createElement('button');
  btn.id = 'hash-unit-toggle';
  btn.className = 'theme-toggle';
  btn.type = 'button';
  btn.setAttribute('data-tip',
    'Switch hashrate display between GH/s and TH/s (1 TH = 1000 GH). AxeOS reports natively in GH/s; pool dashboards usually use TH/s. Display only — logs always store GH/s.');
  btn.innerHTML = '<span class="lbl">GH/s</span>';
  const theme = document.getElementById('theme-toggle');
  if (theme) meta.insertBefore(btn, theme);
  else meta.appendChild(btn);
  btn.addEventListener('click', () => {
    const next = getHashrateUnit() === 'GH' ? 'TH' : 'GH';
    try { localStorage.setItem('hashrateUnit', next); } catch (e) {}
    applyHashrateUnitUI(next);
    // Refresh page-level render immediately rather than waiting for next poll.
    if (typeof window.poll === 'function') window.poll();
  });
}
window.addEventListener('DOMContentLoaded', () => {
  _injectHashUnitButton();
  applyHashrateUnitUI(getHashrateUnit());
});

// ----- toast notifications -----
function toast(msg, type = 'info', timeout = 4000) {
  const host = document.getElementById('toasts');
  if (!host) return;
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.textContent = msg;
  host.appendChild(t);
  setTimeout(() => {
    t.style.transition = 'opacity 0.3s';
    t.style.opacity = '0';
    setTimeout(() => t.remove(), 300);
  }, timeout);
}

// ----- API helper -----
async function api(path, method = 'GET', body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

// ----- formatters -----
function fmtUptime(secs) {
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
function fmtTime(ts) { return new Date(ts * 1000).toLocaleTimeString(); }

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

// Bitaxe firmware returns difficulties either as raw numbers (4096, 9271000302)
// or pre-formatted strings. Normalize numeric values to K/M/G/T/P.
function formatDiff(v) {
  if (v == null || v === '') return '—';
  if (typeof v === 'string' && /[a-zA-Z]/.test(v)) return v;
  const n = Number(v);
  if (!isFinite(n) || n === 0) return '0';
  const units = ['', 'K', 'M', 'G', 'T', 'P'];
  let i = 0, x = n;
  while (Math.abs(x) >= 1000 && i < units.length - 1) { x /= 1000; i++; }
  return (i === 0 ? x.toString() : x.toFixed(x >= 100 ? 0 : x >= 10 ? 1 : 2)) + units[i];
}

function formatNum(n) {
  if (n == null) return '0';
  return Number(n).toLocaleString();
}

function tempClass(t, asic = true) {
  if (asic) {
    if (t < 60) return 'good';
    if (t < 65) return 'warn';
    return 'crit';
  } else {
    if (t < 55) return 'good';
    if (t < 65) return 'warn';
    return 'crit';
  }
}
function effClass(j) {
  if (j < 16) return 'good';
  if (j < 19) return '';
  if (j < 22) return 'warn';
  return 'crit';
}
function hwErrClass(p) {
  if (p < 0.1) return 'ok';
  if (p < 0.5) return 'warn';
  return 'bad';
}

// ----- chart drawing -----
function drawChart(canvas, history, key, color) {
  if (!history || history.length < 2) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr; ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  ctx.strokeStyle = 'rgba(120,140,135,0.18)';
  for (let i = 1; i < 4; i++) {
    const y = (h / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }

  const vals = history.map(p => p[key]);
  const lo = Math.min(...vals) * 0.95;
  const hi = Math.max(...vals) * 1.05;
  const range = hi - lo || 1;

  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  history.forEach((p, i) => {
    const x = (i / (history.length - 1)) * w;
    const y = h - ((p[key] - lo) / range) * h;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + '33');
  grad.addColorStop(1, color + '00');
  ctx.fillStyle = grad;
  ctx.fill();
}

// ----- tooltip primitive -----
// Use [data-tip="text"] on any element. Optional [data-tip-pos="top|bottom|left|right"]
// (defaults to top). Works on dynamically rendered nodes too — handlers use event delegation.
(function setupTooltips() {
  let tooltipEl = null;
  let activeTrigger = null;
  let showTimer = null;

  function ensureEl() {
    if (tooltipEl) return tooltipEl;
    tooltipEl = document.createElement('div');
    tooltipEl.className = 'app-tooltip';
    tooltipEl.setAttribute('role', 'tooltip');
    document.body.appendChild(tooltipEl);
    return tooltipEl;
  }

  function position(trigger, el) {
    const rect = trigger.getBoundingClientRect();
    const pos = trigger.getAttribute('data-tip-pos') || 'top';
    el.style.left = '0px';
    el.style.top = '0px';
    const elRect = el.getBoundingClientRect();
    const margin = 8;
    let x, y;
    if (pos === 'bottom')      { x = rect.left + rect.width / 2 - elRect.width / 2; y = rect.bottom + margin; }
    else if (pos === 'left')   { x = rect.left - elRect.width - margin; y = rect.top + rect.height / 2 - elRect.height / 2; }
    else if (pos === 'right')  { x = rect.right + margin; y = rect.top + rect.height / 2 - elRect.height / 2; }
    else                       { x = rect.left + rect.width / 2 - elRect.width / 2; y = rect.top - elRect.height - margin; }
    x = Math.max(8, Math.min(window.innerWidth - elRect.width - 8, x));
    y = Math.max(8, Math.min(window.innerHeight - elRect.height - 8, y));
    el.style.left = x + 'px';
    el.style.top = y + 'px';
  }

  function show(trigger) {
    const text = trigger.getAttribute('data-tip');
    if (!text) return;
    const el = ensureEl();
    el.textContent = text;
    activeTrigger = trigger;
    requestAnimationFrame(() => {
      position(trigger, el);
      el.classList.add('visible');
    });
  }

  function hide() {
    clearTimeout(showTimer);
    activeTrigger = null;
    if (tooltipEl) tooltipEl.classList.remove('visible');
  }

  document.addEventListener('mouseover', e => {
    const trigger = e.target && e.target.closest && e.target.closest('[data-tip]');
    if (!trigger || trigger === activeTrigger) return;
    clearTimeout(showTimer);
    showTimer = setTimeout(() => show(trigger), 220);
  });
  document.addEventListener('mouseout', e => {
    const trigger = e.target && e.target.closest && e.target.closest('[data-tip]');
    if (!trigger) return;
    hide();
  });
  document.addEventListener('focusin', e => {
    const trigger = e.target && e.target.closest && e.target.closest('[data-tip]');
    if (trigger) show(trigger);
  });
  document.addEventListener('focusout', e => {
    const trigger = e.target && e.target.closest && e.target.closest('[data-tip]');
    if (trigger) hide();
  });
  // ESC dismisses.
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hide(); });
  // Hide while scrolling so position doesn't get stale.
  window.addEventListener('scroll', hide, true);
})();


// ----- Pro / license -----
// Injects a "Pro" button into the header on both pages, plus a modal for
// activating, viewing, or deactivating the license. Polls /api/license/status
// on open. The button shows ✓ Pro when active, otherwise "Pro" with an outline
// (subtle nudge, not nag).
const PRO_BUY_URL = 'https://bitaxeballer.com/pro';

function _proIconHtml() {
  return '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="currentColor"><path d="M12 2l2.39 6.95H22l-6 4.36L18.18 22 12 17.77 5.82 22 8 13.31l-6-4.36h7.61z"/></svg>';
}

function _injectProButton() {
  const meta = document.querySelector('header .meta');
  if (!meta || document.getElementById('pro-toggle')) return;
  const btn = document.createElement('button');
  btn.id = 'pro-toggle';
  btn.className = 'pro-toggle';
  btn.type = 'button';
  btn.setAttribute('data-tip',
    'Bitaxe Baller Pro — unlock bulk tuning, auto-tune sweeps, alerts, and long-term history. Click to activate a license or manage your activation.');
  btn.innerHTML = `${_proIconHtml()}<span class="lbl">Pro</span>`;
  // Insert before the theme toggle so it groups with header actions.
  const theme = document.getElementById('theme-toggle');
  if (theme) meta.insertBefore(btn, theme);
  else meta.appendChild(btn);
  btn.addEventListener('click', openProModal);
}

function _injectProModal() {
  if (document.getElementById('pro-modal')) return;
  const wrap = document.createElement('div');
  wrap.id = 'pro-modal';
  wrap.className = 'pro-modal';
  wrap.hidden = true;
  wrap.innerHTML = `
    <div class="pro-modal-backdrop"></div>
    <div class="pro-modal-panel" role="dialog" aria-modal="true" aria-labelledby="pro-modal-title">
      <button class="pro-modal-close" aria-label="Close">×</button>
      <div class="pro-modal-head">
        <span class="pro-badge">${_proIconHtml()}<span>PRO</span></span>
        <h2 id="pro-modal-title">Bitaxe Baller Pro</h2>
      </div>
      <div class="pro-modal-body" id="pro-modal-body">
        <div class="pro-loading">Loading…</div>
      </div>
    </div>
  `;
  document.body.appendChild(wrap);
  wrap.querySelector('.pro-modal-backdrop').addEventListener('click', closeProModal);
  wrap.querySelector('.pro-modal-close').addEventListener('click', closeProModal);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !wrap.hidden) closeProModal();
  });
}

async function openProModal() {
  _injectProModal();
  const wrap = document.getElementById('pro-modal');
  wrap.hidden = false;
  await _renderProModal();
}

function closeProModal() {
  const wrap = document.getElementById('pro-modal');
  if (wrap) wrap.hidden = true;
}

async function _renderProModal() {
  const body = document.getElementById('pro-modal-body');
  if (!body) return;
  body.innerHTML = '<div class="pro-loading">Checking license…</div>';
  let status;
  try {
    status = await api('/api/license/status');
  } catch (e) {
    body.innerHTML = `<div class="pro-error">Couldn't load license status: ${escapeHtml(e.message)}</div>`;
    return;
  }
  _updateProButton(status.active);
  if (status.active) {
    const isDev = !!status.dev_mode;
    // Fetch remote-access status in parallel-ish so the modal stays snappy. We
    // already know Pro is active so the endpoint will give back a real
    // configured/runtime block rather than a 402.
    let remote = null;
    try { remote = await api('/api/remote/status'); } catch (_) { /* show modal without it */ }
    body.innerHTML = `
      <div class="pro-active">
        <div class="pro-active-badge ${isDev ? 'dev' : ''}">${_proIconHtml()}<span>${isDev ? 'Dev override — Pro features unlocked locally' : 'Active on this machine'}</span></div>
        ${isDev ? '<p class="pro-fineprint" style="border:0;padding:0;margin-bottom:14px">Set by <code>BITAXE_BALLER_DEV_PRO=1</code>. Unset the env var to test the real activation flow.</p>' : ''}
        <dl class="pro-meta">
          ${status.email ? `<dt>Account</dt><dd>${escapeHtml(status.email)}</dd>` : ''}
          ${status.machine_label ? `<dt>Machine</dt><dd>${escapeHtml(status.machine_label)}</dd>` : ''}
          ${status.key_suffix ? `<dt>Key</dt><dd>••••-${escapeHtml(status.key_suffix)}</dd>` : ''}
          ${status.expires_at ? `<dt>Renews</dt><dd>${escapeHtml(new Date(status.expires_at).toLocaleDateString())}</dd>` : ''}
        </dl>
        ${remote ? _renderRemoteAccessSection(remote, isDev) : ''}
        ${isDev ? '' : `
          <p class="pro-fineprint">Deactivating frees this slot so you can move your license to another machine. Your subscription stays active.</p>
          <div class="pro-actions">
            <button class="pro-btn-ghost" id="pro-deactivate">Deactivate this machine</button>
          </div>
        `}
      </div>
    `;
    const deact = document.getElementById('pro-deactivate');
    if (deact) deact.addEventListener('click', _handleDeactivate);
    const remoteToggle = document.getElementById('remote-toggle');
    if (remoteToggle) remoteToggle.addEventListener('click', _handleRemoteToggle);
  } else {
    body.innerHTML = `
      <p class="pro-blurb">Paste the license key you received from Polar after purchase. Free tier features keep working either way.</p>
      <form class="pro-form" id="pro-activate-form" autocomplete="off">
        <label for="pro-key-input">License key</label>
        <input type="text" id="pro-key-input" placeholder="bb-xxxxxxxx-xxxxxxxx-…" spellcheck="false" autocapitalize="off" required>
        <div class="pro-actions">
          <button type="submit" class="primary" id="pro-activate-submit">Activate Pro</button>
          <a href="${PRO_BUY_URL}" target="_blank" rel="noopener" class="pro-btn-ghost">Don't have one? Get Pro →</a>
        </div>
        <div class="pro-feedback" id="pro-feedback"></div>
      </form>
    `;
    document.getElementById('pro-activate-form').addEventListener('submit', _handleActivate);
  }
}

function _renderRemoteAccessSection(remote, isDev) {
  // remote = { configured: {enabled, relay_url}, runtime: {connected, last_error, connected_since, ...} }
  const cfg = remote.configured || {};
  const rt = remote.runtime || {};
  const enabled = !!cfg.enabled;
  const connected = !!rt.connected;
  const stateClass = !enabled ? 'off' : (connected ? 'on' : 'pending');
  const stateLabel = !enabled ? 'Disabled' : (connected ? 'Connected' : (rt.last_error ? 'Disconnected' : 'Connecting…'));
  const btnLabel = enabled ? 'Turn off' : 'Turn on';
  // In dev mode the real license key isn't in config — the relay would reject
  // the empty bearer. Show the section but lock the toggle with an explanation.
  const lockedNote = isDev
    ? '<p class="remote-fineprint">Dev override mode — remote access needs a real activated license. Activate Pro for real to test this.</p>'
    : '';
  return `
    <section class="remote-section">
      <div class="remote-head">
        <div class="remote-title">Remote access</div>
        <div class="remote-state ${stateClass}">
          <span class="remote-dot"></span>${escapeHtml(stateLabel)}
        </div>
      </div>
      <p class="remote-blurb">Reach this dashboard from outside your LAN. The app opens an outbound connection to <code>${escapeHtml(cfg.relay_url || '')}</code> — no inbound port to forward.</p>
      ${enabled && rt.last_error ? `<p class="remote-error">${escapeHtml(rt.last_error)}</p>` : ''}
      ${lockedNote}
      <div class="pro-actions">
        <button class="${enabled ? 'pro-btn-ghost' : 'primary'}" id="remote-toggle" data-enabled="${enabled}" ${isDev ? 'disabled' : ''}>${btnLabel}</button>
      </div>
    </section>
  `;
}

async function _handleRemoteToggle(ev) {
  const btn = ev.currentTarget;
  const wasEnabled = btn.getAttribute('data-enabled') === 'true';
  btn.disabled = true;
  btn.textContent = wasEnabled ? 'Turning off…' : 'Turning on…';
  try {
    if (wasEnabled) {
      await api('/api/remote/disable', 'POST', {});
      toast('Remote access turned off', 'info');
    } else {
      await api('/api/remote/enable', 'POST', {});
      toast('Remote access enabled — connecting…', 'info');
    }
    await _renderProModal();
  } catch (e) {
    toast('Could not toggle remote access: ' + (e.message || e), 'error');
    btn.disabled = false;
    btn.textContent = wasEnabled ? 'Turn off' : 'Turn on';
  }
}

async function _handleActivate(ev) {
  ev.preventDefault();
  const input = document.getElementById('pro-key-input');
  const btn = document.getElementById('pro-activate-submit');
  const fb = document.getElementById('pro-feedback');
  const key = (input.value || '').trim();
  if (!key) return;
  btn.disabled = true; btn.textContent = 'Activating…';
  fb.className = 'pro-feedback'; fb.textContent = '';
  try {
    const res = await api('/api/license/activate', 'POST', { key });
    toast('Pro activated — thank you!', 'info');
    _updateProButton(true);
    await _renderProModal();
  } catch (e) {
    fb.className = 'pro-feedback error';
    fb.textContent = e.message || 'Activation failed';
    btn.disabled = false; btn.textContent = 'Activate Pro';
  }
}

async function _handleDeactivate() {
  if (!confirm('Deactivate Pro on this machine? Your subscription stays active and you can re-activate here or on another machine anytime.')) return;
  const btn = document.getElementById('pro-deactivate');
  btn.disabled = true; btn.textContent = 'Deactivating…';
  try {
    const res = await api('/api/license/deactivate', 'POST', {});
    if (res.warning) toast(res.warning, 'warn', 7000);
    else toast('Pro deactivated on this machine', 'info');
    _updateProButton(false);
    await _renderProModal();
  } catch (e) {
    toast('Deactivate failed: ' + e.message, 'error');
    btn.disabled = false; btn.textContent = 'Deactivate this machine';
  }
}

function _updateProButton(active) {
  const btn = document.getElementById('pro-toggle');
  if (!btn) return;
  btn.classList.toggle('active', !!active);
  const lbl = btn.querySelector('.lbl');
  if (lbl) lbl.textContent = active ? 'Pro' : 'Pro';
  btn.setAttribute('aria-pressed', active ? 'true' : 'false');
}

window.addEventListener('DOMContentLoaded', () => {
  _injectProButton();
  // Lazily check status on load so the button reflects state without forcing
  // the user to open the modal. Single request, fire-and-forget.
  api('/api/license/status').then(s => _updateProButton(s.active)).catch(() => {});
});

window.openProModal = openProModal;
window.closeProModal = closeProModal;


function drawTempChart(canvas, history) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr; ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  ctx.strokeStyle = 'rgba(120,140,135,0.18)';
  for (let i = 1; i < 4; i++) {
    const y = (h / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }

  const allTemps = [...history.map(p => p.asic), ...history.map(p => p.vr)];
  const lo = Math.min(...allTemps) * 0.95;
  const hi = Math.max(...allTemps) * 1.05;
  const range = hi - lo || 1;

  ['asic', 'vr'].forEach((key, idx) => {
    const color = idx === 0 ? '#4cc9f0' : '#ffb000';
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    history.forEach((p, i) => {
      const x = (i / (history.length - 1)) * w;
      const y = h - ((p[key] - lo) / range) * h;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
}
