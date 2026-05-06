/* pyquotex Telegram-signals dashboard — vanilla JS, no build step.
 *
 * The whole UI talks to the same FastAPI host this file is served
 * from, so we only need a relative base URL. The API key is held in
 * localStorage and sent as ``X-API-Key`` on every fetch.
 */

(() => {
  'use strict';

  const $  = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

  const KEY_STORAGE = 'pyquotex.apikey';
  let apiKey = localStorage.getItem(KEY_STORAGE) || '';
  let dialogs = [];          // last fetched
  let selectedChats = new Set();
  let pollHandle = null;

  // ──────────────────────────────────────────────────────────────
  // tiny fetch helper
  // ──────────────────────────────────────────────────────────────
  async function api(path, { method = 'GET', body, query } = {}) {
    const url = new URL(path, location.origin);
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== null) url.searchParams.set(k, v);
      }
    }
    const opts = {
      method,
      headers: {
        'Accept': 'application/json',
        'X-API-Key': apiKey,
      },
    };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(url, opts);
    let data;
    try { data = await r.json(); } catch { data = null; }
    if (!r.ok) {
      const msg = (data && (data.detail || data.error)) || r.statusText;
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      err.status = r.status;
      err.payload = data;
      throw err;
    }
    return data;
  }

  // ──────────────────────────────────────────────────────────────
  // tabs
  // ──────────────────────────────────────────────────────────────
  $$('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('.tab').forEach(b => b.classList.toggle('tab-active', b === btn));
      const target = btn.dataset.tab;
      $$('.tab-pane').forEach(p => p.classList.toggle('tab-active', p.id === target));
    });
  });

  // ──────────────────────────────────────────────────────────────
  // API key gate
  // ──────────────────────────────────────────────────────────────
  function showGate(show) {
    $('#apikey-gate').classList.toggle('show', show);
    $$('.tab-pane').forEach(p => p.classList.toggle('hidden', show));
    document.querySelector('nav.tabs').classList.toggle('hidden', show);
  }

  $('#apikey-save').addEventListener('click', async () => {
    const v = $('#apikey-input').value.trim();
    if (!v) { $('#apikey-error').textContent = 'Required.'; return; }
    apiKey = v;
    try {
      // Quick probe — /signals/config requires auth.
      await api('/signals/config');
      localStorage.setItem(KEY_STORAGE, apiKey);
      $('#apikey-error').textContent = '';
      showGate(false);
      bootstrap();
    } catch (e) {
      $('#apikey-error').textContent = `Rejected: ${e.message}`;
    }
  });

  // ──────────────────────────────────────────────────────────────
  // Quotex pane
  // ──────────────────────────────────────────────────────────────
  async function refreshHealth() {
    try {
      const h = await fetch('/health').then(r => r.json());
      $('#quotex-status').textContent = h.connected ? 'connected' : 'disconnected';
      $('#quotex-auth').textContent = h.auth_status || '—';
      const pill = $('#quotex-pill');
      pill.textContent = `Quotex: ${h.connected ? 'on' : 'off'}`;
      pill.className = 'pill ' + (h.connected ? 'pill-good' : 'pill-bad');
    } catch (e) {
      $('#quotex-status').textContent = 'unreachable';
    }
  }

  async function refreshAccount() {
    try {
      const [bal, prof] = await Promise.all([
        api('/account/balance'),
        api('/account/profile'),
      ]);
      const balText = `${bal.balance.toFixed(2)} ${bal.currency || ''}`.trim();
      $('#quotex-balance').textContent = balText;
      $('#balance-pill').textContent = `Balance: ${balText}`;
      $('#balance-pill').className = 'pill pill-good';
      $('#quotex-profile').textContent =
        [prof.nickname, prof.country].filter(Boolean).join(' · ') || `id ${prof.profile_id || '—'}`;
    } catch (e) {
      $('#quotex-balance').textContent = '—';
      $('#quotex-profile').textContent = '—';
      $('#balance-pill').textContent = 'Balance: —';
      $('#balance-pill').className = 'pill pill-muted';
    }
  }

  $('#btn-quotex-refresh').addEventListener('click', async () => {
    await refreshHealth();
    await refreshAccount();
  });

  $('#btn-quotex-connect').addEventListener('click', async () => {
    $('#quotex-error').textContent = '';
    const btn = $('#btn-quotex-connect');
    btn.disabled = true;
    try {
      const r = await api('/auth/connect', { method: 'POST' });
      if (r.connected) {
        $('#quotex-otp').classList.add('hidden');
      } else if (r.otp_required) {
        $('#quotex-otp').classList.remove('hidden');
        $('#quotex-otp-prompt').textContent = r.otp_prompt || 'Broker requested an OTP.';
      } else {
        $('#quotex-error').textContent = r.message || 'Still connecting…';
      }
    } catch (e) {
      $('#quotex-error').textContent = e.message;
    } finally {
      btn.disabled = false;
      await refreshHealth(); await refreshAccount();
    }
  });

  $('#btn-quotex-otp').addEventListener('click', async () => {
    const code = $('#quotex-otp-code').value.trim();
    if (!code) return;
    $('#quotex-error').textContent = '';
    try {
      const r = await api('/auth/otp', { method: 'POST', body: { code } });
      if (r.connected) {
        $('#quotex-otp').classList.add('hidden');
        $('#quotex-otp-code').value = '';
      }
    } catch (e) {
      $('#quotex-error').textContent = e.message;
    }
    await refreshHealth(); await refreshAccount();
  });

  // ──────────────────────────────────────────────────────────────
  // Telegram pane
  // ──────────────────────────────────────────────────────────────
  function applyTgStatus(s) {
    const pill = $('#telegram-pill');
    let label = 'Telegram: —';
    let cls = 'pill pill-muted';
    if (!s) {
      $('#tg-status').textContent = 'unknown';
    } else if (s.authorised) {
      label = 'Telegram: on'; cls = 'pill pill-good';
      $('#tg-status').textContent = 'authorised';
      $('#tg-step-phone').classList.add('hidden');
      $('#tg-step-code').classList.add('hidden');
      $('#tg-step-password').classList.add('hidden');
      $('#tg-step-authed').classList.remove('hidden');
      const me = s.me || {};
      $('#tg-me').textContent =
        [me.first_name, me.username ? `@${me.username}` : null, me.phone].filter(Boolean).join(' · ') || '—';
    } else if (s.awaiting_password) {
      label = 'Telegram: 2FA'; cls = 'pill pill-warn';
      $('#tg-status').textContent = 'awaiting 2FA password';
      $('#tg-step-phone').classList.add('hidden');
      $('#tg-step-code').classList.add('hidden');
      $('#tg-step-password').classList.remove('hidden');
      $('#tg-step-authed').classList.add('hidden');
    } else if (s.awaiting_code) {
      label = 'Telegram: code'; cls = 'pill pill-warn';
      $('#tg-status').textContent = 'awaiting login code';
      $('#tg-step-phone').classList.add('hidden');
      $('#tg-step-code').classList.remove('hidden');
      $('#tg-step-password').classList.add('hidden');
      $('#tg-step-authed').classList.add('hidden');
    } else if (s.credentials_set) {
      label = 'Telegram: idle'; cls = 'pill pill-muted';
      $('#tg-status').textContent = 'connected, not signed in';
      $('#tg-step-phone').classList.remove('hidden');
      $('#tg-step-code').classList.add('hidden');
      $('#tg-step-password').classList.add('hidden');
      $('#tg-step-authed').classList.add('hidden');
    } else {
      $('#tg-status').textContent = 'no credentials';
      $('#tg-step-phone').classList.remove('hidden');
      $('#tg-step-code').classList.add('hidden');
      $('#tg-step-password').classList.add('hidden');
      $('#tg-step-authed').classList.add('hidden');
    }
    if (s && s.error) {
      $('#tg-error').textContent = s.error;
    }
    pill.textContent = label;
    pill.className = cls;
  }

  async function refreshTelegram() {
    try {
      const s = await api('/telegram/status');
      applyTgStatus(s);
    } catch (e) {
      $('#tg-status').textContent = 'unreachable';
    }
  }

  $('#btn-tg-creds').addEventListener('click', async () => {
    const api_id = parseInt($('#tg-api-id').value.trim(), 10);
    const api_hash = $('#tg-api-hash').value.trim();
    if (!api_id || !api_hash) { $('#tg-error').textContent = 'api_id and api_hash required.'; return; }
    $('#tg-error').textContent = '';
    try {
      const s = await api('/telegram/credentials', { method: 'POST', body: { api_id, api_hash } });
      applyTgStatus(s);
      // Collapse the panel — credentials are saved and rarely change.
      $('#tg-creds-panel').open = false;
    } catch (e) {
      $('#tg-error').textContent = e.message;
    }
  });

  $('#btn-tg-login').addEventListener('click', async () => {
    const phone = $('#tg-phone').value.trim();
    if (!phone) return;
    $('#tg-error').textContent = '';
    try {
      const s = await api('/telegram/login', { method: 'POST', body: { phone } });
      applyTgStatus(s);
    } catch (e) {
      $('#tg-error').textContent = e.message;
    }
  });

  $('#btn-tg-code').addEventListener('click', async () => {
    const code = $('#tg-code').value.trim();
    if (!code) return;
    $('#tg-error').textContent = '';
    try {
      const s = await api('/telegram/code', { method: 'POST', body: { code } });
      applyTgStatus(s);
    } catch (e) {
      $('#tg-error').textContent = e.message;
    }
  });

  $('#btn-tg-password').addEventListener('click', async () => {
    const password = $('#tg-password').value;
    if (!password) return;
    $('#tg-error').textContent = '';
    try {
      const s = await api('/telegram/password', { method: 'POST', body: { password } });
      applyTgStatus(s);
      $('#tg-password').value = '';
    } catch (e) {
      $('#tg-error').textContent = e.message;
    }
  });

  $('#btn-tg-logout').addEventListener('click', async () => {
    if (!confirm('Log out of Telegram and forget the local session?')) return;
    try {
      const s = await api('/telegram/logout', { method: 'POST' });
      applyTgStatus(s);
    } catch (e) {
      $('#tg-error').textContent = e.message;
    }
  });

  // ──────────────────────────────────────────────────────────────
  // Channels pane
  // ──────────────────────────────────────────────────────────────
  function renderDialogs(filter = '') {
    const list = $('#dialog-list');
    list.innerHTML = '';
    const filt = filter.trim().toLowerCase();
    let visible = 0;
    for (const d of dialogs) {
      if (filt && !(`${d.title}`.toLowerCase().includes(filt))) continue;
      visible += 1;
      const row = document.createElement('label');
      row.className = 'dialog-row' + (selectedChats.has(d.id) ? ' checked' : '');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = selectedChats.has(d.id);
      cb.addEventListener('change', () => {
        if (cb.checked) selectedChats.add(d.id); else selectedChats.delete(d.id);
        row.classList.toggle('checked', cb.checked);
      });
      const title = document.createElement('span');
      title.className = 'title';
      title.textContent = d.title;
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent =
        d.is_channel ? 'channel' :
        d.is_group   ? 'group'   :
        d.is_user    ? 'chat'    : '—';
      row.appendChild(cb); row.appendChild(title); row.appendChild(badge);
      list.appendChild(row);
    }
    $('#dialog-count').textContent = `${visible} / ${dialogs.length} dialogs · ${selectedChats.size} selected`;
    if (!dialogs.length) {
      list.innerHTML = '<p class="muted">No dialogs yet — connect Telegram and refresh.</p>';
    }
  }

  $('#btn-dialogs-refresh').addEventListener('click', async () => {
    try {
      const r = await api('/telegram/dialogs', { query: { limit: 200 } });
      dialogs = r.dialogs || [];
      renderDialogs($('#dialog-filter').value);
    } catch (e) {
      $('#dialog-list').innerHTML = `<p class="error">${e.message}</p>`;
    }
  });

  $('#dialog-filter').addEventListener('input', e => renderDialogs(e.target.value));

  $('#btn-channels-save').addEventListener('click', async () => {
    try {
      await api('/signals/config', {
        method: 'PATCH',
        body: { monitored_chat_ids: Array.from(selectedChats) },
      });
      $('#dialog-count').textContent = `Saved · ${selectedChats.size} chat(s) monitored`;
    } catch (e) {
      $('#dialog-list').insertAdjacentHTML('afterend', `<p class="error">${e.message}</p>`);
    }
  });

  // ──────────────────────────────────────────────────────────────
  // Settings pane
  // ──────────────────────────────────────────────────────────────
  function applyConfig(c) {
    $('#cfg-amount').value = c.amount;
    $('#cfg-expiry').value = c.default_expiry_seconds;
    $('#cfg-tz').value = c.default_tz_offset;
    $('#cfg-mtg-levels').value = c.mtg_levels;
    $('#cfg-mtg-mult').value = c.mtg_multiplier;
    $('#cfg-p-live').checked   = !!(c.parsers_enabled && c.parsers_enabled.live_single);
    $('#cfg-p-future').checked = !!(c.parsers_enabled && c.parsers_enabled.future_bulk);
    $('#cfg-p-prep').checked   = !!(c.parsers_enabled && c.parsers_enabled.live_prep);
    $('#cfg-max-age').value = c.max_signal_age_seconds;
    $('#cfg-imm').value = c.immediate_threshold_seconds;
    selectedChats = new Set(c.monitored_chat_ids || []);
    setPauseUI(c.paused);
  }

  function setPauseUI(paused) {
    const btn = $('#pause-btn');
    btn.textContent = paused ? 'Resume' : 'Pause';
    btn.classList.toggle('btn-danger', !paused);
    btn.classList.toggle('btn-primary', paused);
  }

  $('#btn-cfg-save').addEventListener('click', async () => {
    $('#cfg-error').textContent = '';
    const patch = {
      amount: Number($('#cfg-amount').value),
      default_expiry_seconds: Number($('#cfg-expiry').value),
      default_tz_offset: Number($('#cfg-tz').value),
      mtg_levels: Number($('#cfg-mtg-levels').value),
      mtg_multiplier: Number($('#cfg-mtg-mult').value),
      parsers_enabled: {
        live_single: $('#cfg-p-live').checked,
        future_bulk: $('#cfg-p-future').checked,
        live_prep:   $('#cfg-p-prep').checked,
      },
      max_signal_age_seconds: Number($('#cfg-max-age').value),
      immediate_threshold_seconds: Number($('#cfg-imm').value),
    };
    try {
      const c = await api('/signals/config', { method: 'PATCH', body: patch });
      applyConfig(c);
      $('#cfg-error').textContent = 'Saved.';
      setTimeout(() => { if ($('#cfg-error').textContent === 'Saved.') $('#cfg-error').textContent = ''; }, 1500);
    } catch (e) {
      $('#cfg-error').textContent = e.message;
    }
  });

  $('#btn-test-parse').addEventListener('click', async () => {
    const text = $('#test-input').value;
    if (!text.trim()) return;
    try {
      const r = await api('/signals/test-parse', {
        method: 'POST', body: { text },
      });
      const pill = $('#test-matched');
      pill.textContent = r.matched ? `match: ${r.matched}` : 'no match';
      pill.className = 'pill ' + (r.matched ? 'pill-good' : 'pill-muted');
      $('#test-output').textContent = r.detail
        ? JSON.stringify(r.detail, null, 2)
        : '(no parser matched — check the format)';
    } catch (e) {
      $('#test-output').textContent = `Error: ${e.message}`;
    }
  });

  $('#pause-btn').addEventListener('click', async () => {
    const want = $('#pause-btn').textContent === 'Pause';
    try {
      const c = await api('/signals/config', { method: 'PATCH', body: { paused: want } });
      setPauseUI(c.paused);
    } catch (e) {
      console.error(e);
    }
  });

  // ──────────────────────────────────────────────────────────────
  // History pane
  // ──────────────────────────────────────────────────────────────
  function fmtTime(iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleTimeString();
    } catch { return iso; }
  }
  function fmtMs(ms) {
    if (!ms) return '—';
    return new Date(ms).toLocaleTimeString();
  }
  function dirClass(d) { return d === 'put' ? 'dir-put' : 'dir-call'; }

  async function refreshHistory() {
    try {
      const h = await api('/signals/history', { query: { limit: 80 } });
      const sigBody = $('#tbl-signals tbody');
      sigBody.innerHTML = h.signals.map(s => `
        <tr>
          <td>${fmtTime(s.received_at)}</td>
          <td>${s.kind}</td>
          <td>${escapeHtml(s.asset_display)} <small class="muted">(${escapeHtml(s.asset_symbol)})</small></td>
          <td class="${dirClass(s.direction)}">${s.direction.toUpperCase()}</td>
          <td>${s.expiry_seconds}s</td>
          <td>${escapeHtml(s.source_chat_title || (s.source_chat_id ?? '—'))}</td>
        </tr>
      `).join('') || `<tr><td colspan="6" class="muted">No signals yet.</td></tr>`;

      const trBody = $('#tbl-trades tbody');
      trBody.innerHTML = h.trades.map(t => `
        <tr>
          <td>${fmtMs(t.placed_at_ms)}</td>
          <td>${escapeHtml(t.asset_display)}</td>
          <td class="${dirClass(t.direction)}">${t.direction.toUpperCase()}</td>
          <td>${(+t.amount).toFixed(2)}</td>
          <td>${t.mtg_step}</td>
          <td class="state-${t.state}">${t.state}${t.error ? ` <small class="muted" title="${escapeHtml(t.error)}">⚠</small>` : ''}</td>
          <td>${(+t.profit).toFixed(2)}</td>
        </tr>
      `).join('') || `<tr><td colspan="7" class="muted">No trades yet.</td></tr>`;
    } catch (e) {
      // silent — likely lost connection or auth flicker
    }
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ──────────────────────────────────────────────────────────────
  // Bootstrap
  // ──────────────────────────────────────────────────────────────
  async function bootstrap() {
    showGate(false);
    await refreshHealth();
    await refreshAccount();
    await refreshTelegram();
    try {
      const c = await api('/signals/config');
      applyConfig(c);
    } catch (e) {
      console.warn('config load failed', e);
    }
    if (pollHandle) clearInterval(pollHandle);
    pollHandle = setInterval(async () => {
      await refreshHistory();
      await refreshHealth();
      await refreshTelegram();
    }, 2000);
    refreshHistory();
  }

  if (!apiKey) {
    showGate(true);
  } else {
    bootstrap();
  }
})();
