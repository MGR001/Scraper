
  // ── Navigation ───────────────────────────────────────
  function showPage(name) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('page-' + name)?.classList.add('active');
    document.querySelector(`[data-page="${name}"]`)?.classList.add('active');
    if (name === 'dashboard')   loadDashboard();
    if (name === 'own')         loadOwnSources();
    if (name === 'sources')     loadSources();
    if (name === 'competitors') { loadCompetitors(); startStatusPolling(); }
    if (name === 'news')        loadNews();
    if (name === 'settings')    loadSettings();
    if (name === 'changes')     loadCompetitorChanges();
    if (name === 'chat')        loadPositioningCanvas();
    if (name === 'mentions')    loadMentions(true);
  }

  // ── Supabase auth ─────────────────────────────────────────
  let _supabase    = null;
  let _session     = null;
  let _me          = null;
  let _workspaceId = sessionStorage.getItem('sh_workspace_id') || null;

  async function initAuth() {
    const cfg = await fetch('/api/config').then(r => r.json()).catch(() => ({}));
    if (!cfg.supabase_url) { window.location.replace('/login'); return; }
    _supabase = supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key);

    const { data: { session } } = await _supabase.auth.getSession();
    if (!session) { window.location.replace('/login'); return; }
    _session = session;

    _supabase.auth.onAuthStateChange((event, s) => {
      if (event === 'SIGNED_OUT' || !s) { window.location.replace('/login'); }
      else { _session = s; }
    });

    _me = await api('/api/me').catch(() => null);
    if (!_me || !_me.workspaces.length) {
      // New user — no workspace yet; onboarding will create one
      _me = _me || { user: {}, workspaces: [] };
      _renderUserMenu();
      startOnboarding();
      return;
    }

    if (!_workspaceId || !_me.workspaces.find(w => w.id === _workspaceId)) {
      _workspaceId = _me.workspaces[0].id;
      sessionStorage.setItem('sh_workspace_id', _workspaceId);
    }

    _renderUserMenu();

    const ws = _me.workspaces.find(w => w.id === _workspaceId);
    if (ws && !ws.onboarded_at) startOnboarding();
    else showPage('dashboard');
  }

  function _renderUserMenu() {
    const ws = (_me.workspaces || []).find(w => w.id === _workspaceId) || {};
    const name = (_me.user || {}).full_name || (_me.user || {}).email || 'You';
    const el = document.getElementById('user-menu-area');
    if (!el) return;
    el.innerHTML = `<div style="padding:.45rem .9rem .55rem;border-top:1px solid #e2e8f0">
      <div style="font-size:.78rem;font-weight:600;color:#1e293b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${_esc(name)}</div>
      <div style="font-size:.72rem;color:#94a3b8;margin-top:.1rem">${_esc(ws.name || '')}</div>
    </div>`;
  }

  function _esc(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function signOut() {
    if (_supabase) _supabase.auth.signOut();
    sessionStorage.clear();
    window.location.href = '/login';
  }

  // ── API helpers ──────────────────────────────────────
  async function api(path, options = {}) {
    const token = _session ? _session.access_token : null;
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    if (_workspaceId) headers['X-Workspace-Id'] = _workspaceId;
    const res = await fetch(path, { headers, ...options });
    if (res.status === 401) {
      // Try to refresh once
      if (_supabase) {
        const { data } = await _supabase.auth.refreshSession();
        if (data.session) { _session = data.session; return api(path, options); }
      }
      signOut();
      throw new Error('Session expired.');
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Request failed');
    }
    return res.status === 204 ? null : res.json();
  }

  function fmtDate(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
  }

  function badgeHtml(cat) {
    return `<span class="badge badge-${cat ?? 'general'}">${cat ?? 'general'}</span>`;
  }

  // ── Dashboard ────────────────────────────────────────
  function relTime(iso) {
    if (!iso) return '—';
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1)  return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24)  return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }

  async function loadDashboard() {
    try {
      const [stats, sources, statuses] = await Promise.all([
        api('/api/scraper/stats'),
        api('/api/sources/'),
        api('/api/scraper/status').catch(() => ({})),
      ]);

      const dateStr = new Date().toLocaleDateString(undefined, { weekday:'long', year:'numeric', month:'long', day:'numeric' });
      document.getElementById('dash-subtitle').textContent =
        `${dateStr} · Intelligence refreshed ${relTime(stats.last_scrape)}`;

      // KPI cards
      const active = sources.filter(s => s.is_active).length;
      const paused = sources.length - active;
      const comps  = sources.filter(s => s.category === 'competitor');
      document.getElementById('stat-total-sources').textContent   = stats.total_sources;
      document.getElementById('stat-active-sub').textContent      = `${active} active  ·  ${paused} paused`;
      document.getElementById('stat-competitors').textContent     = comps.length;
      document.getElementById('stat-competitors-sub').textContent = `${comps.filter(s=>s.is_active).length} active`;
      document.getElementById('stat-chunks').textContent          = stats.total_chunks.toLocaleString();
      document.getElementById('stat-last-scrape').textContent     = fmtDate(stats.last_scrape);

      const freshnessEl = document.getElementById('stat-freshness');
      if (stats.last_scrape) {
        const hrs = (Date.now() - new Date(stats.last_scrape)) / 3600000;
        if (hrs < 6)       { freshnessEl.textContent = '✓ Fresh';              freshnessEl.className = 'text-xs mt-1 text-emerald-400'; }
        else if (hrs < 48) { freshnessEl.textContent = `${Math.round(hrs)}h old`; freshnessEl.className = 'text-xs mt-1 text-yellow-400'; }
        else               { freshnessEl.textContent = 'Stale — rescrape advised'; freshnessEl.className = 'text-xs mt-1 text-red-400'; }
      }

      // Coverage by category
      const cats = {
        own:        {count:0, chunks:0, pages:0},
        competitor: {count:0, chunks:0, pages:0},
        news:       {count:0, chunks:0, pages:0},
        market:     {count:0, chunks:0, pages:0},
        general:    {count:0, chunks:0, pages:0},
      };
      sources.forEach(s => {
        const c = cats[s.category] ?? cats.general;
        c.count++; c.chunks += s.chunks_stored || 0; c.pages += s.pages_scraped || 0;
      });
      const maxChunks = Math.max(...Object.values(cats).map(c => c.chunks), 1);
      const catColor = { own:'bg-orange-500', competitor:'bg-violet-500', news:'bg-sky-500', market:'bg-emerald-500', general:'bg-slate-400' };
      document.getElementById('coverage-list').innerHTML = Object.entries(cats).map(([cat, d]) => {
        const pct    = Math.round((d.chunks / maxChunks) * 100);
        const chunks = d.chunks >= 1000 ? `${(d.chunks/1000).toFixed(1)}k` : d.chunks;
        return `<div>
          <div class="flex items-center justify-between mb-1.5">
            <div class="flex items-center gap-2">
              ${badgeHtml(cat)}
              <span class="text-xs text-slate-500">${d.count} source${d.count!==1?'s':''} &middot; ${d.pages} pages</span>
            </div>
            <span class="text-xs text-slate-700 font-semibold">${chunks} chunks</span>
          </div>
          <div class="bg-surface rounded-full h-1.5">
            <div class="${catColor[cat]} rounded-full h-1.5" style="width:${Math.max(pct,2)}%"></div>
          </div>
        </div>`;
      }).join('');

      // Source health
      const sorted = [...sources].sort((a,b) => (b.last_scraped_at||'').localeCompare(a.last_scraped_at||''));
      document.getElementById('source-health-list').innerHTML = sorted.map(s => {
        const st  = statuses[s.id] || {};
        const cfg = statusConfig(st.state);
        const hrs = s.last_scraped_at ? (Date.now() - new Date(s.last_scraped_at)) / 3600000 : null;
        const fc  = hrs === null ? 'text-slate-600' : hrs < 12 ? 'text-emerald-400' : hrs < 72 ? 'text-yellow-400' : 'text-red-400';
        const newCount = (st.state === 'completed' && st.new_chunks > 0) ? st.new_chunks : 0;
        return `<div class="flex items-center gap-3 py-2.5 border-b border-border last:border-0">
          <span class="status-dot status-${cfg.cls}" title="${cfg.label}"></span>
          <span class="text-sm text-slate-800 flex-1 min-w-0 truncate">${esc(s.name)}</span>
          ${badgeHtml(s.category)}
          <span class="text-xs ${fc} shrink-0">${relTime(s.last_scraped_at)}</span>
          ${newCount > 0 ? `<span class="text-xs font-semibold text-emerald-400 shrink-0">+${newCount} new</span>` : ''}
          <span class="text-xs text-slate-600 shrink-0 w-16 text-right">${(s.chunks_stored||0).toLocaleString()} chunks</span>
        </div>`;
      }).join('');

    } catch (e) {
      document.getElementById('dash-subtitle').textContent = 'Error loading dashboard';
      console.error(e);
    }
  }

  // ── Sources sub-tabs ───────────────────────────────
  function showSrcTab(tab) {
    document.querySelectorAll('.src-tab-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.srcTab === tab)
    );
    document.getElementById('src-tab-list').classList.toggle('hidden', tab !== 'list');
    document.getElementById('src-tab-content').classList.toggle('hidden', tab !== 'content');
    if (tab === 'content') loadSrcContent();
  }

  async function loadSrcContent() {
    const list = document.getElementById('content-list');
    list.innerHTML = '<p class="text-slate-400 text-sm">Loading…</p>';
    try {
      const [content, sources] = await Promise.all([
        api('/api/scraper/content?limit=30'),
        api('/api/sources/'),
      ]);
      const srcMap = Object.fromEntries(sources.map(s => [s.id, s]));
      if (!content.length) {
        list.innerHTML = '<p class="text-slate-500 text-sm">No content yet. Add sources and scrape them.</p>';
        return;
      }
      list.innerHTML = content.map(c => {
        const src = srcMap[c.source_id] || {};
        const chunks = c.metadata?.total_chunks > 1
          ? `<span class="text-xs text-slate-500">chunk ${(c.metadata.chunk_index??0)+1}/${c.metadata.total_chunks}</span>` : '';
        return `<div class="chunk-item bg-card border border-border rounded-xl p-4 flex items-start justify-between gap-3"
             onclick="viewChunk('${c.id}')" title="Click to view full content">
          <div class="min-w-0 flex-1">
            <p class="font-medium text-slate-900 text-sm truncate">${esc(c.title || 'Untitled')}</p>
            <p class="text-xs text-slate-400 mt-0.5 truncate">${esc(c.url)}</p>
          </div>
          <div class="flex flex-col items-end gap-1.5 shrink-0">
            ${badgeHtml(src.category)}${chunks}
            <span class="text-xs text-slate-500">${fmtDate(c.scraped_at)}</span>
          </div>
        </div>`;
      }).join('');
    } catch (e) {
      list.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    }
  }

  async function showSourceUrls(sourceId, sourceName) {
    const modal = document.getElementById('urls-modal');
    const body  = document.getElementById('urls-modal-body');
    const title = document.getElementById('urls-modal-title');
    const count = document.getElementById('urls-modal-count');
    title.textContent = sourceName;
    count.textContent = 'Loading…';
    body.innerHTML    = '';
    modal.classList.add('open');
    try {
      const urls = await api(`/api/scraper/urls/${sourceId}`);
      count.textContent = `${urls.length} page${urls.length !== 1 ? 's' : ''} scraped`;
      body.innerHTML = urls.map(u => `
        <a href="${esc(u.url)}" target="_blank" rel="noopener noreferrer"
           class="flex items-start gap-2 px-3 py-2 rounded-lg hover:bg-surface transition group">
          <svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5 text-slate-500 group-hover:text-blue-400 shrink-0 mt-0.5 transition" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244"/>
          </svg>
          <div class="min-w-0">
            <p class="text-sm text-slate-800 group-hover:text-blue-300 truncate transition">${esc(u.title || u.url)}</p>
            <p class="text-xs text-slate-500 truncate">${esc(u.url)}</p>
          </div>
          <span class="text-xs text-slate-600 shrink-0 ml-auto">${fmtDate(u.scraped_at)}</span>
        </a>`).join('');
    } catch (e) {
      body.innerHTML = `<p class="text-red-400 text-sm p-3">Error: ${esc(e.message)}</p>`;
    }
  }

  function closeUrlsModal() {
    document.getElementById('urls-modal').classList.remove('open');
  }

  async function viewChunk(id) {
    const modal = document.getElementById('chunk-modal');
    const body  = document.getElementById('chunk-modal-body');
    const title = document.getElementById('chunk-modal-title');
    const meta  = document.getElementById('chunk-modal-meta');
    body.textContent  = 'Loading…';
    title.textContent = '';
    meta.textContent  = '';
    modal.classList.add('open');
    try {
      const c = await api(`/api/scraper/content/${id}`);
      title.textContent = c.title || 'Untitled';
      meta.textContent  = `${c.url}  ·  ${fmtDate(c.scraped_at)}  ·  ${c.metadata?.char_count ?? 0} chars`;
      body.textContent  = c.content;
    } catch (e) {
      body.textContent = 'Error: ' + e.message;
    }
  }

  function closeModal() {
    document.getElementById('chunk-modal').classList.remove('open');
  }

  async function scrapeAll() {
    try {
      const r = await api('/api/scraper/run-all', { method: 'POST' });
      showToast(r.message);
    } catch (e) {
      showToast(e.message, true);
    }
  }

  // ── Competitors ──────────────────────────────────────────────────────────
  async function loadCompetitors() {
    const el = document.getElementById('competitors-list');
    try {
      const all = await api('/api/sources/');
      const ownSources = all.filter(s => s.category === 'own');
      const sources    = all.filter(s => s.category === 'competitor');
      if (!sources.length && !ownSources.length) {
        el.innerHTML = '<p class="text-slate-500 text-sm">No competitor sources yet. Add sources with the "Competitor" category in the Sources tab.</p>';
        return;
      }
      // Fetch current scrape statuses
      const statuses = await api('/api/scraper/status').catch(() => ({}));
      const ownHtml = ownSources.length ? `
        <p class="text-xs font-bold text-orange-600 uppercase tracking-widest mb-2">Your Company</p>
        <div class="space-y-3 mb-6">${ownSources.map(s => buildCompetitorCard(s, statuses[s.id])).join('')}</div>
        <p class="text-xs font-bold text-slate-400 uppercase tracking-widest mb-2">Competitors</p>` : '';
      el.innerHTML = ownHtml + (sources.length
        ? sources.map(s => buildCompetitorCard(s, statuses[s.id])).join('')
        : '<p class="text-slate-500 text-sm">No competitor sources yet. Add sources with the "Competitor" category in the Sources tab.</p>');
      startStatusPolling();
    } catch (e) {
      el.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    }
  }

  let _pollTimer = null;
  let _prevStates = {};

  function startStatusPolling() {
    if (_pollTimer) return;
    _pollTimer = setInterval(async () => {
      try {
        const statuses = await api('/api/scraper/status');
        let anyRunning = false;

        for (const [sid, st] of Object.entries(statuses)) {
          // Update Competitors tab badge
          updateStatusBadge(sid, st);
          // Update Sources tab dot
          const srcDot = document.getElementById(`src-status-${sid}`);
          if (srcDot) {
            const cfg = statusConfig(st?.state);
            srcDot.className = `status-dot status-${cfg.cls}`;
            srcDot.title     = cfg.label;
          }
          // Detect running → done transition → refresh active tab data
          const prev = _prevStates[sid]?.state;
          if (prev === 'running' && st.state !== 'running') {
            if (document.getElementById('page-sources')?.classList.contains('active'))     loadSources();
            if (document.getElementById('page-dashboard')?.classList.contains('active'))   loadDashboard();
            if (document.getElementById('page-competitors')?.classList.contains('active')) loadCompetitors();
            if (document.getElementById('page-own')?.classList.contains('active'))         loadOwnSources();
          }
          if (st.state === 'running') anyRunning = true;
        }
        _prevStates = {...statuses};
        if (!anyRunning) { clearInterval(_pollTimer); _pollTimer = null; }
      } catch (_) {}
    }, 2000);
  }

  function updateStatusBadge(sourceId, st) {
    const badge  = document.getElementById(`status-badge-${sourceId}`);
    const detail = document.getElementById(`status-detail-${sourceId}`);
    if (!badge) return;
    const cfg = statusConfig(st?.state, st?.detail);
    badge.className  = `status-dot status-${cfg.cls}`;
    badge.title      = cfg.label;
    detail.textContent = cfg.detail;
    detail.className = `text-xs mt-1 ${cfg.textCls}`;
    const newBadge = document.getElementById(`new-chunks-badge-${sourceId}`);
    if (newBadge) {
      if (st?.state === 'completed' && st?.new_chunks > 0) {
        newBadge.className = 'inline-flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30';
        newBadge.title = 'New chunks from last scrape';
        newBadge.textContent = `↑ +${st.new_chunks} new`;
      } else if (st?.state !== 'running') {
        newBadge.className = 'hidden';
        newBadge.textContent = '';
      }
    }
  }

  function statusConfig(state, detail) {
    switch (state) {
      case 'running':   return { cls: 'running',   label: 'Scraping',  textCls: 'text-blue-400',   detail: detail || 'Running…' };
      case 'completed': return { cls: 'completed', label: 'Complete',  textCls: 'text-emerald-500', detail: detail || 'Completed' };
      case 'error':     return { cls: 'error',     label: 'Error',     textCls: 'text-red-400',     detail: detail || 'Error' };
      default:          return { cls: 'idle',      label: 'Not scraped yet', textCls: 'text-slate-500', detail: '' };
    }
  }

  function buildCompetitorCard(s, st) {
    const hasSummary = s.summary && s.summary.trim();
    const genDate    = s.summary_generated_at ? `Generated ${fmtDate(s.summary_generated_at)}` : '';
    const cfg        = statusConfig(st?.state, st?.detail);
    const newChunks  = (st?.state === 'completed' && st?.new_chunks > 0) ? st.new_chunks : 0;
    return `
      <div id="competitor-${s.id}" class="bg-card border border-border rounded-2xl overflow-hidden">
        <div class="flex items-center justify-between gap-4 p-4 border-b border-border">
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2.5">
              <span id="status-badge-${s.id}" class="status-dot status-${cfg.cls}" title="${cfg.label}"></span>
              <span class="font-semibold text-slate-900">${esc(s.name)}</span>
              ${badgeHtml(s.category)}
              ${newChunks > 0 ? `<span id="new-chunks-badge-${s.id}" class="inline-flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30" title="New chunks from last scrape">↑ +${newChunks} new</span>` : `<span id="new-chunks-badge-${s.id}" class="hidden"></span>`}
            </div>
            <a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer"
               class="text-xs text-slate-400 hover:text-blue-400 truncate block mt-0.5 ml-4">${esc(s.url)}</a>
            <p id="status-detail-${s.id}" class="text-xs mt-1 ml-4 ${cfg.textCls}">${esc(cfg.detail)}</p>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            ${genDate ? `<span class="text-xs text-slate-500 hidden sm:block">${genDate}</span>` : ''}
            <button onclick="generateSummary('${s.id}')" id="btn-${s.id}"
              class="flex items-center gap-1.5 bg-blue-600/20 hover:bg-blue-600/40 text-blue-300 text-xs font-medium px-3 py-1.5 rounded-lg transition border border-blue-500/30">
              <svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z"/>
              </svg>
              ${hasSummary ? 'Regenerate' : 'Generate Summary'}
            </button>
          </div>
        </div>
        <div id="summary-${s.id}" class="p-5 text-sm text-slate-700 leading-relaxed">
          ${hasSummary
            ? `<div class="prose-answer">${mdToHtml(s.summary)}</div>`
            : `<p class="text-slate-500 italic">${s.chunks_stored > 0
                ? 'No summary yet \u2014 click Generate Summary.'
                : 'No content scraped yet. Go to Sources and scrape this source first.'}</p>`
          }
        </div>
      </div>`;
  }

  async function generateSummary(sourceId) {
    const btn     = document.getElementById(`btn-${sourceId}`);
    const summDiv = document.getElementById(`summary-${sourceId}`);
    if (!btn || !summDiv) {
      // Source isn't rendered on the current page (e.g. a news/market source
      // while on the Competitors page) — nothing to update, just call the API.
      await api(`/api/insights/summary/${sourceId}`, { method: 'POST' });
      return;
    }
    btn.disabled  = true;
    btn.innerHTML = '<span class="spinner"></span> Generating\u2026';
    summDiv.innerHTML = '<div class="flex items-center gap-2 text-slate-500 text-sm"><span class="spinner"></span> Analysing content with GPT-5.6\u2026</div>';
    try {
      const res = await api(`/api/insights/summary/${sourceId}`, { method: 'POST' });
      summDiv.innerHTML = `<div class="prose-answer">${mdToHtml(res.summary)}</div>`;
      btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z"/></svg> Regenerate`;
    } catch (e) {
      summDiv.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
      btn.innerHTML = 'Retry';
    } finally {
      btn.disabled = false;
    }
  }

  async function generateAllSummaries() {
    const sources = await api('/api/sources/').catch(() => []);
    // Only the sources actually shown on this page (own + competitor) — generating
    // summaries for news/market/general sources here would silently skip them
    // anyway since they have no evidence content_summary in the same sense.
    const targets = sources.filter(s => (s.category === 'competitor' || s.category === 'own') && s.chunks_stored > 0);
    let failed = 0;
    for (const s of targets) {
      try {
        await generateSummary(s.id);
      } catch (e) {
        failed++;
        console.error(`Failed to generate summary for ${s.name}:`, e);
      }
    }
    if (failed > 0) {
      showToast(`Generated ${targets.length - failed}/${targets.length} summaries — ${failed} failed.`, true);
    } else if (targets.length > 0) {
      showToast(`Generated ${targets.length} summar${targets.length === 1 ? 'y' : 'ies'}.`);
    } else {
      showToast('No sources with scraped content yet.', true);
    }
  }

  // ── Competitive Landscape Matrix ─────────────────────
  async function generateComparison() {
    const btn  = document.getElementById('btn-comparison');
    const body = document.getElementById('comparison-body');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Analysing…';
    body.innerHTML = '<div class="flex items-center gap-2 text-slate-400 text-sm"><span class="spinner"></span> GPT-5.6 is analysing all competitor profiles…</div>';
    try {
      const data = await api('/api/insights/comparison', { method: 'POST' });
      renderComparison(data);
    } catch (e) {
      body.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    } finally {
      btn.disabled = false;
      btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5M9 11.25v1.5M12 9v3.75m3-6v6"/></svg> Regenerate`;
    }
  }

  function renderComparison(data) {
    const body = document.getElementById('comparison-body');
    const { strategic_context, competitors, uniqueness, strategic_implications } = data;
    if (!competitors?.length) {
      body.innerHTML = '<p class="text-slate-500 text-sm">No comparison data returned.</p>';
      return;
    }

    // ── Badge helpers ────────────────────────────────────
    const pricingBadge = m => {
      const map = {
        'Free':         'text-emerald-400 bg-emerald-500/10 border-emerald-500/30',
        'Freemium':     'text-violet-400 bg-violet-500/10 border-violet-500/30',
        'Subscription': 'text-blue-400 bg-blue-500/10 border-blue-500/30',
        'Usage-Based':  'text-cyan-400 bg-cyan-500/10 border-cyan-500/30',
        'Per-Seat':     'text-sky-400 bg-sky-500/10 border-sky-500/30',
        'Enterprise':   'text-amber-400 bg-amber-500/10 border-amber-500/30',
        'Open-Source':  'text-teal-400 bg-teal-500/10 border-teal-500/30',
        'Unknown':      'text-slate-400 bg-slate-500/10 border-slate-500/30',
      };
      const cls = map[m] || map['Unknown'];
      return `<span class="inline-block text-xs font-semibold px-2 py-0.5 rounded-full border ${cls}">${esc(m || 'Unknown')}</span>`;
    };

    const gtmBadge = m => {
      const map = {
        'PLG':         'text-indigo-400 bg-indigo-500/10 border-indigo-500/30',
        'SLG':         'text-orange-400 bg-orange-500/10 border-orange-500/30',
        'Channel':     'text-amber-400 bg-amber-500/10 border-amber-500/30',
        'Community':   'text-emerald-400 bg-emerald-500/10 border-emerald-500/30',
        'Marketplace': 'text-violet-400 bg-violet-500/10 border-violet-500/30',
        'Direct':      'text-blue-400 bg-blue-500/10 border-blue-500/30',
        'Hybrid':      'text-cyan-400 bg-cyan-500/10 border-cyan-500/30',
        'Unknown':     'text-slate-400 bg-slate-500/10 border-slate-500/30',
      };
      const cls = map[m] || map['Unknown'];
      return `<span class="inline-block text-xs font-semibold px-2 py-0.5 rounded-full border ${cls}">${esc(m || 'Unknown')}</span>`;
    };

    const pills = arr => (arr || []).map(o =>
      `<span class="inline-block bg-slate-700/70 text-slate-300 text-xs px-2 py-0.5 rounded mr-1 mb-1">${esc(o)}</span>`
    ).join('') || '—';

    const bullets = arr => (arr || []).map(s =>
      `<li class="flex items-start gap-1.5 text-sm text-slate-700"><span class="text-slate-500 mt-0.5">•</span>${esc(s)}</li>`
    ).join('');

    // ── Section groups ───────────────────────────────────
    const groups = [
      {
        label: 'POSITIONING',
        rows: [
          { label: 'Positioning',       render: c => `<span class="text-slate-800">${esc(c.positioning)}</span>` },
          { label: 'Target Market',     render: c => `<span class="text-slate-700">${esc(c.target_market)}</span>` },
          { label: 'Value Proposition', render: c => `<span class="text-blue-700">${esc(c.value_proposition)}</span>` },
        ],
      },
      {
        label: 'PRODUCTS & SERVICES',
        rows: [
          { label: 'Key Products',      render: c => pills(c.key_products) },
          { label: 'Differentiator',    render: c => `<span class="text-indigo-700">${esc(c.key_differentiator)}</span>` },
          { label: 'Strengths',         render: c => `<ul class="space-y-1">${bullets(c.strengths)}</ul>` },
        ],
      },
      {
        label: 'GO-TO-MARKET',
        rows: [
          { label: 'GTM Motion',     render: c => gtmBadge(c.gtm_motion) },
          { label: 'GTM Channels',   render: c => pills(c.gtm_channels) },
          { label: 'Pricing Model',  render: c => pricingBadge(c.pricing_model) },
          { label: 'Pricing Detail', render: c => `<span class="text-slate-400 text-xs">${esc(c.pricing_detail)}</span>` },
        ],
      },
      {
        label: 'KEY FINDINGS',
        rows: [
          { label: 'Key Findings', render: c => `<ol class="space-y-1.5">${(c.key_findings||[]).map((f,i) =>
            `<li class="flex items-start gap-2 text-sm text-slate-700"><span class="shrink-0 text-slate-500 tabular-nums">${i+1}.</span>${esc(f)}</li>`
          ).join('')}</ol>` },
        ],
      },
    ];

    const colW = 'min-w-[220px]';
    const dimW = 'w-36';

    const headerCols = competitors.map(c => `
      <th class="px-4 py-3 text-left font-semibold text-sm ${colW} border-b border-border ${c.is_own_company ? 'bg-orange-50 text-orange-700' : 'bg-surface/50 text-slate-900'}">
        ${esc(c.name)}${c.is_own_company ? ' <span class="text-[10px] font-normal text-orange-600">(you)</span>' : ''}
      </th>`
    ).join('');

    const tableBody = groups.map(g => {
      const groupHeader = `
        <tr>
          <td colspan="${competitors.length + 1}"
              class="px-4 pt-4 pb-1.5 text-xs font-bold text-slate-500 tracking-widest uppercase bg-surface/30 border-t border-border">${g.label}</td>
        </tr>`;
      const dataRows = g.rows.map(row => `
        <tr class="border-t border-border/40 hover:bg-surface/20 transition-colors">
          <td class="px-4 py-3 text-xs font-semibold text-slate-500 align-top whitespace-nowrap ${dimW} bg-surface/10">${row.label}</td>
          ${competitors.map(c => `<td class="px-4 py-3 align-top leading-relaxed${c.is_own_company ? ' bg-orange-50/40' : ''}">${row.render(c)}</td>`).join('')}
        </tr>`).join('');
      return groupHeader + dataRows;
    }).join('');

    // ── What Makes Each Unique section ───────────────────
    const uniquenessHtml = uniqueness?.length ? `
      <div class="mt-6 pt-5 border-t border-border">
        <p class="text-xs font-bold text-slate-400 uppercase tracking-widest mb-3">What Makes Each Unique</p>
        <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          ${uniqueness.map(u => `
            <div class="bg-surface/30 border border-border/50 rounded-xl p-4">
              <p class="text-xs font-semibold text-indigo-700 mb-1.5">${esc(u.name)}</p>
              <p class="text-sm text-slate-700 leading-relaxed">${esc(u.unique_angle)}</p>
            </div>`).join('')}
        </div>
      </div>` : '';

    // ── Strategic Implications section ───────────────────
    const implsHtml = strategic_implications?.length ? `
      <div class="mt-6 pt-5 border-t border-border">
        <p class="text-xs font-bold text-slate-400 uppercase tracking-widest mb-3">Strategic Implications</p>
        <ol class="space-y-2.5">
          ${strategic_implications.map((imp, i) => `
            <li class="flex items-start gap-3 text-sm text-slate-700">
              <span class="shrink-0 w-5 h-5 rounded-full bg-indigo-600/30 text-indigo-700 text-xs flex items-center justify-center font-bold mt-0.5">${i + 1}</span>
              <span>${esc(imp)}</span>
            </li>`).join('')}
        </ol>
      </div>` : '';

    body.innerHTML = `
      ${strategic_context ? `<p class="text-sm text-slate-700 leading-relaxed border-l-2 border-indigo-500 pl-3 mb-5">${esc(strategic_context)}</p>` : ''}
      <div class="overflow-x-auto rounded-lg border border-border/50">
        <table class="w-full text-left border-collapse text-sm">
          <thead>
            <tr class="bg-surface/60">
              <th class="px-4 py-3 text-xs text-slate-500 uppercase tracking-widest font-semibold ${dimW} border-b border-border">Dimension</th>
              ${headerCols}
            </tr>
          </thead>
          <tbody>${tableBody}</tbody>
        </table>
      </div>
      ${uniquenessHtml}
      ${implsHtml}`;
  }

  // ── News Feed ────────────────────────────────────────
  let _newsCategory = 'news';
  let _newsItems    = [];
  let _newsOffset   = 0;
  const _newsLimit  = 30;

  async function loadNews(reset = true) {
    if (reset) { _newsOffset = 0; _newsItems = []; }
    const feed = document.getElementById('news-feed');
    if (reset) feed.innerHTML = '<p class="text-slate-400 text-sm">Loading…</p>';
    try {
      const qs = { limit: _newsLimit, offset: _newsOffset };
      if (_newsCategory) qs.category = _newsCategory;
      if (_newsSourcesSelected) qs.source_ids = [..._newsSourcesSelected].join(',');
      const tasks = [api(`/api/scraper/news?${new URLSearchParams(qs)}`)];
      if (reset) tasks.push(loadNewsSources());
      const [items] = await Promise.all(tasks);
      _newsItems  = reset ? items : [..._newsItems, ...items];
      _newsOffset += items.length;
      renderNewsSourcePills();
      applyNewsFilters();
      document.getElementById('news-load-more').classList.toggle('hidden', items.length < _newsLimit);
    } catch (e) {
      feed.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    }
  }

  // ── News source filter ────────────────────────────────
  let _newsSources          = [];   // every active source in this category, not just loaded items
  let _newsSourcesSelected  = null; // null = show all sources

  async function loadNewsSources() {
    try {
      const all = await api('/api/sources/');
      _newsSources = all.filter(s => s.is_active && (!_newsCategory || s.category === _newsCategory));
    } catch (e) {
      _newsSources = [];
    }
  }

  function renderNewsSourcePills() {
    const el = document.getElementById('news-source-pills');
    if (_newsSources.length < 2) { el.innerHTML = ''; return; }

    const allActive = _newsSourcesSelected === null;
    const pills = [`<button onclick="toggleNewsSourceFilter(null)" class="news-cat-btn${allActive ? ' active' : ''}">All sources</button>`];
    for (const src of _newsSources) {
      const active = !allActive && _newsSourcesSelected.has(src.id);
      pills.push(`<button onclick="toggleNewsSourceFilter('${src.id}')" class="news-cat-btn${active ? ' active' : ''}">${esc(src.name)}</button>`);
    }
    el.innerHTML = pills.join('');
  }

  function toggleNewsSourceFilter(sourceId) {
    if (sourceId === null) {
      _newsSourcesSelected = null;
    } else {
      const current = new Set(_newsSourcesSelected || []);
      if (current.has(sourceId)) current.delete(sourceId); else current.add(sourceId);
      _newsSourcesSelected = current.size ? current : null;
    }
    // Re-fetch from the server scoped to the selected source(s), rather than
    // filtering whatever happened to be in the already-loaded page - a
    // less-recently-scraped source's articles may not be in that page at all.
    loadNews(true);
  }

  async function loadNewsDigest() {
    const el  = document.getElementById('news-digest');
    const btn = document.getElementById('digest-gen-btn');
    btn.disabled = true;
    btn.textContent = 'Generating…';
    el.innerHTML = '<p class="text-slate-400 text-sm animate-pulse">Analysing the last 5 days of news…</p>';
    try {
      const data = await api('/api/scraper/news/digest');
      if (data.error) {
        el.innerHTML = `<p class="text-yellow-500 text-sm">${esc(data.error)}</p>`;
        return;
      }
      renderNewsDigest(data);
      const dt = document.getElementById('digest-date');
      if (dt && data.generated_at) dt.textContent = '· ' + fmtDate(data.generated_at);
    } catch(e) {
      el.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Regenerate';
    }
  }

  function renderNewsDigest(data) {
    const { digest, articles } = data;
    if (!digest) return;
    const artMap = {};
    (articles || []).forEach(a => { artMap[a.index] = a; });
    const themesHtml = (digest.themes || []).map(t => {
      const links = (t.article_indices || [])
        .map(i => artMap[i]).filter(Boolean).slice(0, 4)
        .map(a => `<a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer"
          class="text-xs text-blue-400 hover:text-blue-300 hover:underline truncate block">${esc(a.title || a.url)}</a>`)
        .join('');
      return `<div class="border border-border rounded-lg p-3.5">
        <p class="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1.5">${esc(t.theme)}</p>
        <p class="text-sm text-slate-400 leading-relaxed mb-2">${esc(t.summary)}</p>
        ${links ? `<div class="space-y-0.5 mt-2 border-t border-border pt-2">${links}</div>` : ''}
      </div>`;
    }).join('');
    document.getElementById('news-digest').innerHTML = `
      <div class="mb-4">
        <p class="text-sm font-semibold text-slate-900 mb-1">${esc(digest.headline)}</p>
        <p class="text-sm text-slate-400 leading-relaxed">${esc(digest.overview)}</p>
      </div>
      ${themesHtml ? `<div class="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">${themesHtml}</div>` : ''}
      ${digest.strategic_takeaway ? `
      <div class="bg-blue-950/30 border border-blue-800/30 rounded-lg px-4 py-3">
        <p class="text-xs font-semibold text-blue-400 uppercase tracking-wide mb-1">Strategic Takeaway</p>
        <p class="text-sm text-slate-700">${esc(digest.strategic_takeaway)}</p>
      </div>` : ''}`;
  }

  function applyNewsFilters() {
    const q = document.getElementById('news-search').value.trim().toLowerCase();
    let items = _newsItems;
    if (_newsSourcesSelected) {
      items = items.filter(item => _newsSourcesSelected.has(item.source_id));
    }
    if (q) {
      items = items.filter(item =>
        (item.title       || '').toLowerCase().includes(q) ||
        (item.url         || '').toLowerCase().includes(q) ||
        (item.snippet     || '').toLowerCase().includes(q) ||
        (item.source_name || '').toLowerCase().includes(q)
      );
    }
    renderNewsItems(items, !!(q || _newsSourcesSelected));
  }

  function renderNewsItems(items, filtered = false) {
    const feed = document.getElementById('news-feed');
    if (!items.length) {
      feed.innerHTML = `<p class="text-slate-500 text-sm italic">${filtered
        ? 'No results match your filter.'
        : 'No content scraped yet — add sources and scrape them.'}</p>`;
      return;
    }
    const catDot = { competitor:'bg-violet-500', news:'bg-sky-500', market:'bg-emerald-500', general:'bg-slate-500' };
    feed.innerHTML = items.map(item => `
      <div class="bg-card border border-border rounded-xl p-4 hover:border-slate-600 transition group">
        <div class="flex items-start gap-3">
          <span class="shrink-0 w-2 h-2 rounded-full mt-2 ${catDot[item.category] || 'bg-slate-500'}"></span>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2 mb-1 flex-wrap">
              <span class="text-xs font-semibold text-slate-400">${esc(item.source_name)}</span>
              ${badgeHtml(item.category)}
              <span class="text-xs text-slate-600 ml-auto shrink-0">${fmtDate(item.scraped_at)}</span>
            </div>
            <a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer"
               class="font-medium text-slate-900 text-sm hover:text-blue-300 transition block mb-1 line-clamp-2">
              ${esc(item.title || item.url)}
            </a>
            <p class="text-xs text-slate-500 truncate mb-2">${esc(item.url)}</p>
            ${item.snippet ? `<p class="text-sm text-slate-400 leading-relaxed line-clamp-3">${esc(item.snippet)}</p>` : ''}
          </div>
          <button onclick="viewChunk('${item.id}')" title="View full content"
            class="shrink-0 text-slate-600 hover:text-blue-400 transition opacity-0 group-hover:opacity-100 mt-0.5">
            <svg xmlns="http://www.w3.org/2000/svg" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z"/>
              <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
            </svg>
          </button>
        </div>
      </div>`).join('');
  }

  async function loadMoreNews() { await loadNews(false); }

  // ── Reddit Mentions ──────────────────────────────────
  let _mentionsItems           = [];
  let _mentionsOffset          = 0;
  const _mentionsLimit         = 30;
  let _mentionsSources         = [];   // competitor sources with mentions tracking enabled
  let _mentionsSourceSelected  = null; // null = all competitors
  let _mentionsSwitchingOnly   = false;

  async function loadMentions(reset = true) {
    if (reset) { _mentionsOffset = 0; _mentionsItems = []; }
    const feed = document.getElementById('mentions-feed');
    if (reset) feed.innerHTML = '<p class="text-slate-400 text-sm">Loading…</p>';
    try {
      const qs = { limit: _mentionsLimit, offset: _mentionsOffset };
      if (_mentionsSourceSelected) qs.source_id = _mentionsSourceSelected;
      if (_mentionsSwitchingOnly) {
        qs.signal_type = 'switching_intent';
      } else {
        const signal = document.getElementById('mentions-signal-filter').value;
        if (signal) qs.signal_type = signal;
      }

      const tasks = [api(`/api/mentions/?${new URLSearchParams(qs)}`)];
      if (reset) tasks.push(loadMentionsSummary(), loadMentionsSourcePills());
      const [items] = await Promise.all(tasks);
      _mentionsItems  = reset ? items : [..._mentionsItems, ...items];
      _mentionsOffset += items.length;
      renderMentionsFeed(_mentionsItems);
      document.getElementById('mentions-load-more').classList.toggle('hidden', items.length < _mentionsLimit);
    } catch (e) {
      feed.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    }
  }

  async function loadMoreMentions() { await loadMentions(false); }

  function applyMentionsFilters() { loadMentions(true); }

  function toggleSwitchingIntentFilter() {
    _mentionsSwitchingOnly = !_mentionsSwitchingOnly;
    const btn = document.getElementById('mentions-switching-btn');
    btn.className = _mentionsSwitchingOnly
      ? 'text-xs font-semibold px-3 py-1.5 rounded-lg border border-amber-500 bg-amber-500 text-white transition'
      : 'text-xs font-medium px-3 py-1.5 rounded-lg border border-amber-300 text-amber-700 hover:bg-amber-50 transition';
    loadMentions(true);
  }

  async function loadMentionsSourcePills() {
    try {
      const all = await api('/api/sources/');
      _mentionsSources = all.filter(s => (s.category === 'competitor' || s.category === 'market') && s.mentions_enabled);
    } catch (e) {
      _mentionsSources = [];
    }
    renderMentionsSourcePills();
  }

  function renderMentionsSourcePills() {
    const el = document.getElementById('mentions-source-pills');
    if (_mentionsSources.length < 2) { el.innerHTML = ''; return; }
    const allActive = _mentionsSourceSelected === null;
    const pills = [`<button onclick="setMentionsSource(null)" class="news-cat-btn${allActive ? ' active' : ''}">All competitors</button>`];
    for (const src of _mentionsSources) {
      const active = _mentionsSourceSelected === src.id;
      pills.push(`<button onclick="setMentionsSource('${src.id}')" class="news-cat-btn${active ? ' active' : ''}">${esc(src.name)}</button>`);
    }
    el.innerHTML = pills.join('');
  }

  function setMentionsSource(sourceId) {
    _mentionsSourceSelected = sourceId;
    renderMentionsSourcePills();
    loadMentions(true);
  }

  async function loadMentionsSummary() {
    try {
      const data = await api('/api/mentions/summary');
      renderMentionsSummary(data.results || []);
    } catch (e) {
      document.getElementById('mentions-summary').innerHTML = '';
    }
  }

  function renderMentionsSummary(results) {
    const el = document.getElementById('mentions-summary');
    if (!results.length) {
      el.innerHTML = `<p class="text-slate-500 text-sm sm:col-span-2">No competitor or market sources yet. Add one, then enable "Track Reddit mentions" from its Edit Source form.</p>`;
      return;
    }
    el.innerHTML = results.map(r => {
      if (r.insufficient) {
        return `<div class="bg-card border border-border rounded-xl p-4">
          <p class="font-semibold text-slate-900 text-sm mb-1">${esc(r.source_name)}</p>
          <p class="text-xs text-slate-500">Not enough signal yet (${r.n} mention${r.n !== 1 ? 's' : ''} so far — need 5+)</p>
        </div>`;
      }
      const sentiment    = r.weighted_sentiment;
      const sentimentStr = sentiment == null ? '—' : sentiment.toFixed(2);
      const sentimentCol = sentiment == null ? 'text-slate-500'
        : sentiment > 0.15 ? 'text-emerald-600' : sentiment < -0.15 ? 'text-red-600' : 'text-slate-600';
      return `<div class="bg-card border border-border rounded-xl p-4">
        <div class="flex items-center justify-between mb-1">
          <p class="font-semibold text-slate-900 text-sm flex items-center gap-1.5">
            ${esc(r.source_name)}
            ${r.spike ? '<span class="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded-full bg-orange-500/15 text-orange-600 border border-orange-500/30" title="Relevant mentions in the last 24h are more than 5x the trailing 7-day daily average">🔺 Spike</span>' : ''}
          </p>
          <span class="text-xs text-slate-500">${r.n} mention${r.n !== 1 ? 's' : ''}</span>
        </div>
        <div class="flex items-center gap-3 mt-1.5 flex-wrap">
          <span class="text-xs font-medium ${sentimentCol}">Sentiment ${sentimentStr}</span>
          ${r.switching_intent_count > 0 ? `<span class="text-xs font-semibold text-amber-600">⚠ ${r.switching_intent_count} switching</span>` : ''}
          ${r.top_negative_aspect ? `<span class="text-xs text-slate-500">Top complaint: ${esc(r.top_negative_aspect)}</span>` : ''}
        </div>
      </div>`;
    }).join('');
  }

  const _MENTIONS_SIGNAL_LABELS = {
    complaint: 'Complaint', praise: 'Praise', question: 'Question',
    comparison: 'Comparison', switching_intent: 'Switching intent', other: 'Other',
  };

  function _sentimentChip(sentiment) {
    if (sentiment == null) return '';
    const label = sentiment > 0.15 ? 'Positive' : sentiment < -0.15 ? 'Negative' : 'Neutral';
    const cls = sentiment > 0.15
      ? 'bg-emerald-500/15 text-emerald-600 border-emerald-500/30'
      : sentiment < -0.15
        ? 'bg-red-500/15 text-red-600 border-red-500/30'
        : 'bg-slate-500/15 text-slate-600 border-slate-500/30';
    return `<span class="inline-flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded-full border ${cls}">${label} ${sentiment.toFixed(2)}</span>`;
  }

  function _signalChip(signalType) {
    if (!signalType) return '';
    const cls = signalType === 'switching_intent'
      ? 'bg-amber-500/15 text-amber-700 border-amber-500/30'
      : signalType === 'complaint'
        ? 'bg-red-500/10 text-red-600 border-red-500/25'
        : signalType === 'praise'
          ? 'bg-emerald-500/10 text-emerald-600 border-emerald-500/25'
          : 'bg-slate-500/10 text-slate-600 border-slate-500/25';
    return `<span class="inline-flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded-full border ${cls}">${_MENTIONS_SIGNAL_LABELS[signalType] || signalType}</span>`;
  }

  function renderMentionsFeed(items) {
    const feed = document.getElementById('mentions-feed');
    if (!items.length) {
      feed.innerHTML = '<p class="text-slate-500 text-sm">No mentions yet. Enable mention tracking on a competitor and run a sweep.</p>';
      return;
    }
    const srcMap = Object.fromEntries(_mentionsSources.map(s => [s.id, s]));
    feed.innerHTML = items.map(m => {
      const srcName = srcMap[m.source_id]?.name || '';
      return `<a href="${esc(m.url)}" target="_blank" rel="noopener noreferrer"
        class="block bg-card border border-border rounded-xl p-4 hover:border-blue-400 transition">
        <div class="flex items-center gap-2 flex-wrap mb-1.5">
          ${srcName ? `<span class="text-xs font-semibold text-slate-500">${esc(srcName)}</span>` : ''}
          <span class="text-xs text-slate-400">r/${esc(m.subreddit || '')}</span>
          ${_sentimentChip(m.sentiment)}
          ${_signalChip(m.signal_type)}
          ${m.aspect ? `<span class="text-xs text-slate-400">${esc(m.aspect)}</span>` : ''}
          <span class="text-xs text-slate-400 ml-auto">${fmtDate(m.published_at)}</span>
        </div>
        <p class="text-sm text-slate-900 font-medium truncate">${esc(m.title || '')}</p>
        ${m.summary ? `<p class="text-sm text-slate-500 mt-1 leading-relaxed">${esc(m.summary)}</p>` : ''}
      </a>`;
    }).join('');
  }

  // ── My Company ──────────────────────────────────────
  async function loadOwnSources() {
    const el       = document.getElementById('own-sources-list');
    const statsEl  = document.getElementById('own-stats');
    try {
      const [all, statuses] = await Promise.all([
        api('/api/sources/'),
        api('/api/scraper/status').catch(() => ({})),
      ]);
      _sourcesCache = all;
      const sources = all.filter(s => s.category === 'own');

      // Stats strip
      const totalChunks = sources.reduce((a, s) => a + (s.chunks_stored || 0), 0);
      const totalPages  = sources.reduce((a, s) => a + (s.pages_scraped || 0), 0);
      statsEl.innerHTML = [
        { label: 'Sources',       value: sources.length,             color: 'text-orange-600' },
        { label: 'Pages indexed', value: totalPages.toLocaleString(), color: 'text-slate-900'  },
        { label: 'Chunks stored', value: totalChunks.toLocaleString(),color: 'text-slate-900'  },
      ].map(s => `<div class="bg-card border border-border rounded-xl p-4 text-center">
        <p class="text-2xl font-bold ${s.color}">${s.value}</p>
        <p class="text-xs text-slate-500 mt-1">${s.label}</p>
      </div>`).join('');

      if (!sources.length) {
        el.innerHTML = '<p class="text-slate-500 text-sm">No company sources yet. Add your website or key pages above.</p>';
        return;
      }
      el.innerHTML = sources.map(s => {
        const st  = statuses[s.id] || {};
        const cfg = statusConfig(st.state, st.detail);
        const newChunks = (st.state === 'completed' && st.new_chunks > 0) ? st.new_chunks : 0;
        return `<div class="bg-card border border-border rounded-xl p-4 flex items-center gap-4">
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-0.5">
              <span class="font-semibold text-slate-900 text-sm">${esc(s.name)}</span>
              ${badgeHtml(s.category)}
              ${s.is_active ? '' : '<span class="badge badge-general">paused</span>'}
              ${newChunks > 0 ? `<span class="inline-flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30" title="New chunks from last scrape">↑ +${newChunks} new</span>` : ''}
            </div>
            <p class="text-xs text-slate-400 truncate">${esc(s.url)}</p>
            <div class="flex items-center gap-3 mt-1.5">
              <span class="text-xs text-slate-500">Every ${s.scrape_interval}h</span>
              <span class="text-xs text-slate-500">&middot;</span>
              <span class="text-xs ${s.last_scraped_at ? 'text-slate-400' : 'text-slate-600'}">Last scraped: ${fmtDate(s.last_scraped_at)}</span>
              ${s.pages_scraped > 0 ? `<span class="text-xs text-slate-500">&middot;</span>
              <button onclick="showSourceUrls('${s.id}','${esc(s.name)}')"
                class="text-xs text-orange-500 hover:text-orange-400 hover:underline transition">
                ${s.pages_scraped} page${s.pages_scraped !== 1 ? 's' : ''}</button>
              <span class="text-xs text-slate-600">(${s.chunks_stored} chunks)</span>` : ''}
            </div>
            <p id="status-detail-own-${s.id}" class="text-xs mt-1 ${cfg.textCls || 'text-slate-500'}">${cfg.detail || ''}</p>
          </div>
          <div class="flex gap-2 shrink-0 items-center">
            <span id="src-status-${s.id}" class="status-dot status-${cfg.cls}" title="${cfg.label}"></span>
            <button onclick="openAddUrlModal('${s.id}', '${esc(s.name)}')"
              class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 px-3 py-1.5 rounded-lg transition" title="Add a specific URL">
              + URL
            </button>
            <button onclick="scrapeOne('${s.id}', '${esc(s.name)}')"
              class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 px-3 py-1.5 rounded-lg transition">
              Scrape
            </button>
            <button onclick="openEditSourceModal('${s.id}')"
              class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 px-3 py-1.5 rounded-lg transition">
              Edit
            </button>
            <button onclick="toggleSource('${s.id}', ${!s.is_active})"
              class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 px-3 py-1.5 rounded-lg transition">
              ${s.is_active ? 'Pause' : 'Resume'}
            </button>
            <button onclick="deleteSource('${s.id}', '${esc(s.name)}');loadOwnSources()"
              class="text-xs bg-red-900/50 hover:bg-red-800/70 text-red-300 px-3 py-1.5 rounded-lg transition">
              Delete
            </button>
          </div>
        </div>`;
      }).join('');
    } catch (e) {
      el.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    }
  }

  async function addOwnSource(e) {
    e.preventDefault();
    const msg = document.getElementById('add-own-msg');
    msg.textContent = '';
    try {
      await api('/api/sources/', {
        method: 'POST',
        body: JSON.stringify({
          name:             document.getElementById('own-src-name').value.trim(),
          url:              document.getElementById('own-src-url').value.trim(),
          category:         'own',
          scrape_interval:  parseInt(document.getElementById('own-src-interval').value, 10) || 24,
          crawl_scope:      document.getElementById('own-src-scope').value,
        }),
      });
      document.getElementById('add-own-form').reset();
      msg.className = 'text-sm text-emerald-400';
      msg.textContent = 'Source added.';
      loadOwnSources();
    } catch (err) {
      msg.className = 'text-sm text-red-400';
      msg.textContent = err.message;
    }
  }

  async function scrapeAllOwn() {
    try {
      const all  = await api('/api/sources/');
      const own  = all.filter(s => s.category === 'own' && s.is_active);
      await Promise.all(own.map(s => api(`/api/scraper/run/${s.id}`, { method: 'POST' })));
      showToast(`Scraping ${own.length} company source${own.length !== 1 ? 's' : ''}…`);
      startStatusPolling();
    } catch (e) {
      showToast(e.message, true);
    }
  }

  // ── Sources ──────────────────────────────────────────
  let _srcCategory = '';
  let _sourcesCache = [];

  function setSrcCategory(cat) {
    _srcCategory = cat;
    document.querySelectorAll('#src-category-pills .news-cat-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.cat === cat)
    );
    loadSources();
  }

  async function loadSources() {
    const el = document.getElementById('sources-table');
    try {
      const all      = await api('/api/sources/');
      _sourcesCache  = all;
      const sources  = _srcCategory ? all.filter(s => s.category === _srcCategory) : all;
      const statuses = await api('/api/scraper/status').catch(() => ({}));
      if (!sources.length) {
        el.innerHTML = '<p class="text-slate-500 text-sm">No sources in this category yet.</p>';
        return;
      }
      el.innerHTML = sources.map(s => `
        <div class="bg-card border border-border rounded-xl p-4 flex items-center gap-4">
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-0.5">
              <span class="font-semibold text-slate-900 text-sm">${esc(s.name)}</span>
              ${badgeHtml(s.category)}
              ${s.is_active ? '' : '<span class="badge badge-general">paused</span>'}
            </div>
            <p class="text-xs text-slate-400 truncate">${esc(s.url)}</p>
              <div class="flex items-center gap-3 mt-1.5">
                <span class="text-xs text-slate-500">Every ${s.scrape_interval}h</span>
                <span class="text-xs text-slate-500">&middot;</span>
                <span class="text-xs ${s.last_scraped_at ? 'text-slate-400' : 'text-slate-600'}">
                  Last scraped: ${fmtDate(s.last_scraped_at)}
                </span>
                ${s.pages_scraped > 0 ? `
                <span class="text-xs text-slate-500">&middot;</span>
                <button onclick="showSourceUrls('${s.id}','${esc(s.name)}')"
                  class="text-xs text-emerald-500 hover:text-emerald-400 hover:underline transition">
                  ${s.pages_scraped} page${s.pages_scraped !== 1 ? 's' : ''}
                </button>
                <span class="text-xs text-slate-600">(${s.chunks_stored} chunks)</span>` : ''}
                ${s.new_or_changed_pages > 0 ? `
                <span class="text-xs text-slate-500">&middot;</span>
                <span class="text-xs font-medium text-amber-600">${s.new_or_changed_pages} new/changed</span>` : ''}
              </div>              ${s.sitemap_url ? `
              <div class="flex items-center gap-1.5 mt-1">
                <svg xmlns="http://www.w3.org/2000/svg" class="w-3 h-3 text-blue-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M9 6.75V15m6-6v8.25m.503-10.498l4.875 2.437c.381.19.622.58.622 1.006V17.25a.75.75 0 01-.437.688l-4.875 2.25a.75.75 0 01-.626 0l-4.875-2.25A.75.75 0 014.5 17.25V9.375c0-.426.24-.816.622-1.006l4.875-2.437a.75.75 0 01.756 0z"/>
                </svg>
                <a href="${esc(s.sitemap_url)}" target="_blank" rel="noopener noreferrer"
                   class="text-xs text-blue-400 hover:text-blue-300 truncate" title="${esc(s.sitemap_url)}">
                  Sitemap found
                </a>
              </div>` : `
              <div class="flex items-center gap-1.5 mt-1">
                <svg xmlns="http://www.w3.org/2000/svg" class="w-3 h-3 text-slate-600 shrink-0" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636"/>
                </svg>
                <span class="text-xs text-slate-600">No sitemap</span>
              </div>`}          </div>
          <div class="flex gap-2 shrink-0 items-center">
            <span id="src-status-${s.id}" class="status-dot status-${statusConfig(statuses[s.id]?.state).cls}"
              title="${statusConfig(statuses[s.id]?.state).label}"></span>
            <button onclick="openAddUrlModal('${s.id}', '${esc(s.name)}')"
              class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 px-3 py-1.5 rounded-lg transition" title="Add a specific URL to this source">
              + URL
            </button>
            <button onclick="scrapeOne('${s.id}', '${esc(s.name)}')"
              class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 px-3 py-1.5 rounded-lg transition">
              Scrape
            </button>
            <button onclick="openEditSourceModal('${s.id}')"
              class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 px-3 py-1.5 rounded-lg transition">
              Edit
            </button>
            <button onclick="toggleSource('${s.id}', ${!s.is_active})"
              class="text-xs bg-slate-700 hover:bg-slate-600 text-slate-200 px-3 py-1.5 rounded-lg transition">
              ${s.is_active ? 'Pause' : 'Resume'}
            </button>
            <button onclick="deleteSource('${s.id}', '${esc(s.name)}')"
              class="text-xs bg-red-900/50 hover:bg-red-800/70 text-red-300 px-3 py-1.5 rounded-lg transition">
              Delete
            </button>
          </div>
        </div>`).join('');
    } catch (e) {
      el.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    }
  }

  async function addSource(e) {
    e.preventDefault();
    const msg = document.getElementById('add-source-msg');
    msg.textContent = '';
    try {
      await api('/api/sources/', {
        method: 'POST',
        body: JSON.stringify({
          name:             document.getElementById('src-name').value.trim(),
          url:              document.getElementById('src-url').value.trim(),
          category:         document.getElementById('src-category').value,
          scrape_interval:  parseInt(document.getElementById('src-interval').value, 10),
          crawl_scope:      document.getElementById('src-scope').value,
        }),
      });
      document.getElementById('add-source-form').reset();
      msg.className = 'text-sm text-emerald-400';
      msg.textContent = 'Source added!';
      loadSources();
      setTimeout(() => msg.textContent = '', 3000);
    } catch (err) {
      msg.className = 'text-sm text-red-400';
      msg.textContent = err.message;
    }
  }

  async function scrapeOne(id, name) {
    try {
      const r = await api(`/api/scraper/run/${id}`, { method: 'POST' });
      showToast(r.message);
      // Optimistically show running dot immediately
      const dot = document.getElementById(`src-status-${id}`);
      if (dot) { dot.className = 'status-dot status-running'; dot.title = 'Scraping…'; }
      startStatusPolling();
    } catch (e) {
      showToast(e.message, true);
    }
  }

  async function toggleSource(id, active) {
    try {
      await api(`/api/sources/${id}`, { method: 'PUT', body: JSON.stringify({ is_active: active }) });
      showToast(active ? 'Source resumed — will be included in scheduled scrapes.' : 'Source paused — scheduler will skip it.');
      loadSources();
    } catch (e) {
      showToast(e.message, true);
    }
  }

  async function deleteSource(id, name) {
    if (!confirm(`Delete source "${name}" and all its content?`)) return;
    try {
      await api(`/api/sources/${id}`, { method: 'DELETE' });
      loadSources();
    } catch (e) {
      showToast(e.message, true);
    }
  }

  // ── Add URL to source ────────────────────────────────
  let _addUrlSourceId = null;

  let _editSourceId = null;

  async function openFeatureCategoriesModal() {
    const ta = document.getElementById('feature-categories-input');
    const st = document.getElementById('feature-categories-status');
    st.className = 'text-sm mt-2 hidden';
    st.textContent = '';
    ta.value = '…loading…';
    document.getElementById('feature-categories-modal').classList.add('open');
    try {
      const data = await api('/api/settings');
      ta.value = (data.workspace && data.workspace.feature_matrix_categories) || '';
    } catch (e) {
      ta.value = '';
      st.className = 'text-sm mt-2 text-red-500';
      st.textContent = 'Could not load current categories: ' + e.message;
    }
  }

  function closeFeatureCategoriesModal() {
    document.getElementById('feature-categories-modal').classList.remove('open');
  }

  async function submitFeatureCategories() {
    const btn = document.getElementById('feature-categories-submit');
    const st  = document.getElementById('feature-categories-status');
    const value = document.getElementById('feature-categories-input').value;
    btn.disabled = true;
    try {
      await api('/api/settings/workspace', {
        method: 'PATCH',
        body: JSON.stringify({ feature_matrix_categories: value }),
      });
      closeFeatureCategoriesModal();
      showToast('Feature Comparison categories updated.');
      loadFeatureMatrix(true);
    } catch (e) {
      st.className = 'text-sm mt-2 text-red-500';
      st.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  }

  function toggleMentionsSection() {
    const category = document.getElementById('edit-src-category').value;
    const show = category === 'competitor' || category === 'market';
    document.getElementById('edit-src-mentions-section').classList.toggle('hidden', !show);
  }

  function openEditSourceModal(sourceId) {
    const src = _sourcesCache.find(s => s.id === sourceId);
    if (!src) { showToast('Source not found — try reloading the list.', true); return; }
    _editSourceId = sourceId;
    document.getElementById('edit-src-name').value = src.name || '';
    document.getElementById('edit-src-url').value = src.url || '';
    document.getElementById('edit-src-category').value = src.category || 'general';
    document.getElementById('edit-src-interval').value = src.scrape_interval || 24;
    document.getElementById('edit-src-scope').value = src.crawl_scope || 'domain';
    document.getElementById('edit-src-sitemap').value = src.sitemap_url || '';
    document.getElementById('edit-src-mentions-enabled').checked = !!src.mentions_enabled;
    document.getElementById('edit-src-mention-terms').value = (src.mention_terms || []).join(', ');
    document.getElementById('edit-src-mention-subreddits').value = (src.mention_subreddits || []).join(', ');
    toggleMentionsSection();
    const st = document.getElementById('edit-src-status');
    st.className = 'text-sm hidden';
    st.textContent = '';
    document.getElementById('edit-src-submit').disabled = false;
    document.getElementById('edit-source-modal').classList.add('open');
  }

  function closeEditSourceModal() {
    document.getElementById('edit-source-modal').classList.remove('open');
    _editSourceId = null;
  }

  async function submitEditSource() {
    const name     = document.getElementById('edit-src-name').value.trim();
    const url      = document.getElementById('edit-src-url').value.trim();
    const category = document.getElementById('edit-src-category').value;
    const interval = parseInt(document.getElementById('edit-src-interval').value, 10);
    const scope    = document.getElementById('edit-src-scope').value;
    const sitemap  = document.getElementById('edit-src-sitemap').value.trim();
    const st       = document.getElementById('edit-src-submit');
    const statusEl = document.getElementById('edit-src-status');
    if (!name || !url) {
      statusEl.className = 'text-sm text-red-500';
      statusEl.textContent = 'Name and URL are required.';
      return;
    }
    st.disabled = true;
    try {
      await api(`/api/sources/${_editSourceId}`, {
        method: 'PUT',
        body: JSON.stringify({ name, url, category, scrape_interval: interval || 24, crawl_scope: scope, sitemap_url: sitemap }),
      });
      if (category === 'competitor' || category === 'market') {
        const mentionsEnabled = document.getElementById('edit-src-mentions-enabled').checked;
        const terms = document.getElementById('edit-src-mention-terms').value
          .split(',').map(t => t.trim()).filter(Boolean);
        const subreddits = document.getElementById('edit-src-mention-subreddits').value
          .split(',').map(s => s.trim().replace(/^r\//i, '')).filter(Boolean);
        await api(`/api/sources/${_editSourceId}/mentions-config`, {
          method: 'PATCH',
          body: JSON.stringify({
            mentions_enabled: mentionsEnabled,
            mention_terms: terms,
            mention_subreddits: subreddits,
          }),
        });
      }
      closeEditSourceModal();
      showToast('Source updated.');
      loadSources();
      loadOwnSources();
    } catch (e) {
      statusEl.className = 'text-sm text-red-500';
      statusEl.textContent = e.message;
    } finally {
      st.disabled = false;
    }
  }

  function openAddUrlModal(sourceId, sourceName) {
    _addUrlSourceId = sourceId;
    document.getElementById('add-url-source-label').textContent = sourceName;
    document.getElementById('add-url-input').value = '';
    const st = document.getElementById('add-url-status');
    st.className = 'text-sm mb-3 hidden';
    st.textContent = '';
    document.getElementById('add-url-submit').disabled = false;
    document.getElementById('add-url-modal').classList.add('open');
  }

  function closeAddUrlModal() {
    document.getElementById('add-url-modal').classList.remove('open');
    _addUrlSourceId = null;
  }

  async function submitAddUrl() {
    const url = document.getElementById('add-url-input').value.trim();
    if (!url) return;
    const st  = document.getElementById('add-url-status');
    const btn = document.getElementById('add-url-submit');
    btn.disabled = true;
    st.className = 'text-sm mb-3 text-slate-400';
    st.textContent = 'Fetching and indexing…';
    try {
      const r = await api(`/api/sources/${_addUrlSourceId}/add-url`, {
        method: 'POST',
        body: JSON.stringify({ url }),
      });
      st.className = 'text-sm mb-3 text-emerald-400';
      st.textContent = `Indexed — ${r.new_chunks} new chunk(s) added for “${r.title || url}”`;
      loadSources();
    } catch (e) {
      st.className = 'text-sm mb-3 text-red-400';
      st.textContent = e.message;
      btn.disabled = false;
    }
  }

  let _changesData = null;

  // ── Insights tabs ──────────────────────────────────────
  function showInsightsTab(tab) {
    ['canvas', 'matrix', 'kano'].forEach(t => {
      document.getElementById('ins-sub-' + t).classList.toggle('hidden', t !== tab);
      const btn = document.querySelector('[data-ins-tab="' + t + '"]');
      btn.classList.toggle('active', t === tab);
      btn.setAttribute('aria-selected', t === tab ? 'true' : 'false');
    });
    if (tab === 'canvas') loadPositioningCanvas();
    if (tab === 'matrix') loadFeatureMatrix();
    if (tab === 'kano')   loadKanoAnalysis();
  }

  // ── Competitor change detection ───────────────────────
  // Loads once per session (fresh on every new login / page reload) and shows
  // immediately when the tab is opened; the button forces a manual re-fetch.
  async function loadCompetitorChanges(force = false) {
    const panel   = document.getElementById('changes-panel');
    const content = document.getElementById('changes-content');
    const btn     = document.getElementById('changes-btn');
    panel.classList.remove('hidden');
    if (_changesData && !force) return;
    content.innerHTML = '<p class="text-slate-400 text-sm animate-pulse">Analysing competitor changes…</p>';
    btn.disabled = true;
    try {
      const data = await api('/api/insights/competitor-changes');
      renderCompetitorChanges(data);
      _changesData = data;
    } catch (e) {
      content.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    } finally {
      btn.disabled = false;
    }
  }

  function renderCompetitorChanges(data) {
    const content = document.getElementById('changes-content');
    const results = data.results || [];
    if (!results.length) {
      content.innerHTML = '<p class="text-slate-500 text-sm">No active competitor or company sources found.</p>';
      return;
    }
    const cards = results.map(r => {
      const dot   = r.has_changes ? 'bg-amber-400' : 'bg-emerald-500';
      const label = r.has_changes ? 'Changes detected' : 'No significant changes';
      const labelCol = r.has_changes ? 'text-amber-400' : 'text-emerald-400';
      const scrapeInfo = r.latest_scrape
        ? `<span class="text-xs text-slate-600">Latest: ${fmtDate(r.latest_scrape)}${
            r.previous_scrape ? ` &nbsp;&middot;&nbsp; Prev: ${fmtDate(r.previous_scrape)}` : ' &nbsp;&middot;&nbsp; <em>no previous scrape</em>'
          }${
            r.new_or_changed_pages > 0
              ? ` &nbsp;&middot;&nbsp; <span class="text-amber-600 font-medium">${r.new_or_changed_pages} new/changed page${r.new_or_changed_pages !== 1 ? 's' : ''}</span>`
              : ''
          }</span>` : '';
      const changesHtml = (r.changes || []).length
        ? `<ul class="mt-2 space-y-1">${r.changes.map(c =>
            `<li class="flex items-start gap-1.5 text-xs text-amber-700"><span class="mt-1 shrink-0 w-1.5 h-1.5 rounded-full bg-amber-400"></span>${esc(c)}</li>`
          ).join('')}</ul>` : '';
      const stableHtml = (r.stable || []).length
        ? `<ul class="mt-1 space-y-0.5">${r.stable.map(s =>
            `<li class="flex items-start gap-1.5 text-xs text-slate-500"><span class="mt-1 shrink-0 w-1.5 h-1.5 rounded-full bg-slate-600"></span>${esc(s)}</li>`
          ).join('')}</ul>` : '';
      return `<div class="bg-surface border ${r.is_own_company ? 'border-orange-300' : 'border-border'} rounded-xl p-4">
        <div class="flex items-center gap-2 mb-1">
          <span class="w-2 h-2 rounded-full ${dot} shrink-0"></span>
          <span class="font-semibold text-slate-900 text-sm">${esc(r.name)}</span>
          ${r.is_own_company ? '<span class="text-[10px] font-normal text-orange-600">(you)</span>' : ''}
          <span class="text-xs ${labelCol} ml-auto">${label}</span>
        </div>
        ${scrapeInfo ? `<div class="mb-2">${scrapeInfo}</div>` : ''}
        <p class="text-sm text-slate-400 leading-relaxed">${esc(r.summary)}</p>
        ${changesHtml}${stableHtml}
      </div>`;
    }).join('');
    content.innerHTML = `
      <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold text-slate-900 text-sm">Competitor Change Analysis <span class="text-xs font-normal text-slate-500">(current vs. previous scrape)</span></h3>
        <div class="flex items-center gap-3">
          <button onclick="exportChanges()" class="text-xs text-slate-400 hover:text-blue-400 transition">Export</button>
          <button onclick="loadCompetitorChanges(true)" class="text-xs text-slate-400 hover:text-blue-400 transition">Refresh</button>
        </div>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">${cards}</div>`;
  }

  // ── Export changes ─────────────────────────────────
  function exportChanges() {
    if (!_changesData) return;
    const results = _changesData.results || [];
    const dateStr = new Date().toLocaleDateString(undefined, {
      weekday:'long', year:'numeric', month:'long', day:'numeric'
    });
    const sep  = '='.repeat(64);
    const dash = '-'.repeat(64);
    const lines = [];

    lines.push('COMPETITOR INTELLIGENCE UPDATE');
    lines.push(`Generated: ${dateStr}`);
    lines.push('');
    lines.push(sep);
    lines.push('');
    const withChanges = results.filter(r => r.has_changes).length;
    lines.push('EXECUTIVE SUMMARY');
    lines.push(`${results.length} competitor${results.length !== 1 ? 's' : ''} analysed. ` +
      `${withChanges} showing meaningful changes since the previous scrape.`);
    lines.push('');
    lines.push(sep);

    for (const r of results) {
      lines.push('');
      const status = r.has_changes ? '⚠ CHANGES DETECTED' : '✓ NO SIGNIFICANT CHANGES';
      lines.push(`${r.name.toUpperCase()}  —  ${status}`);
      if (r.latest_scrape || r.previous_scrape) {
        const ls = r.latest_scrape   ? fmtDate(r.latest_scrape)   : '—';
        const ps = r.previous_scrape ? fmtDate(r.previous_scrape) : 'no previous scrape';
        lines.push(`Scrapes compared:  ${ls}  vs  ${ps}`);
      }
      lines.push('');
      lines.push('Summary:');
      lines.push(r.summary || '—');
      if ((r.changes || []).length) {
        lines.push('');
        lines.push('What changed:');
        r.changes.forEach(c => lines.push(`  • ${c}`));
      }
      if ((r.stable || []).length) {
        lines.push('');
        lines.push('Stable areas:');
        r.stable.forEach(s => lines.push(`  • ${s}`));
      }
      lines.push('');
      lines.push(dash);
    }

    lines.push('');
    lines.push('Generated by RIvals');

    document.getElementById('export-text').value = lines.join('\n');
    const btn = document.getElementById('copy-export-btn');
    btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M15.666 3.888A2.25 2.25 0 0013.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 01-.75.75H9a.75.75 0 01-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 01-2.25 2.25H6.75A2.25 2.25 0 014.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 011.927-.184"/></svg> Copy to Clipboard`;
    document.getElementById('export-modal').classList.add('open');
  }

  function closeExportModal() {
    document.getElementById('export-modal').classList.remove('open');
  }

  async function copyExport() {
    const ta  = document.getElementById('export-text');
    const btn = document.getElementById('copy-export-btn');
    try {
      await navigator.clipboard.writeText(ta.value);
      btn.textContent = '✓ Copied!';
      setTimeout(() => {
        btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M15.666 3.888A2.25 2.25 0 0013.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 01-.75.75H9a.75.75 0 01-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 01-2.25 2.25H6.75A2.25 2.25 0 014.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 011.927-.184"/></svg> Copy to Clipboard`;
      }, 2000);
    } catch (_) {
      ta.select();
    }
  }

  // ── Positioning Canvas ───────────────────────────────
  let _canvasData = null;

  async function loadPositioningCanvas(force = false) {
    const btn     = document.getElementById('canvas-refresh-btn');
    const content = document.getElementById('canvas-content');
    if (_canvasData && !force) { renderPositioningCanvas(_canvasData); return; }
    btn.disabled = true;
    content.innerHTML = '<p class="text-slate-400 text-sm animate-pulse">Mapping the competitive landscape…</p>';
    try {
      const data = await api('/api/insights/positioning-canvas');
      _canvasData = data;
      renderPositioningCanvas(data);
    } catch (e) {
      content.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    } finally {
      btn.disabled = false;
    }
  }

  function renderPositioningCanvas(data) {
    const content = document.getElementById('canvas-content');
    const companies = data.companies || [];
    const xa = data.x_axis || {};
    const ya = data.y_axis || {};
    if (!companies.length) {
      content.innerHTML = '<p class="text-slate-500 text-sm">No positioning data yet.</p>';
      return;
    }
    const dots = companies.map(c => {
      const left = Math.max(3, Math.min(97, c.x));
      const bottom = Math.max(3, Math.min(97, c.y));
      const own = c.is_own;
      return `<div class="absolute flex flex-col items-center gap-1 z-10" style="left:${left}%; bottom:${bottom}%; transform:translate(-50%, 50%)" title="${esc(c.rationale || '')}">
        <span class="w-3 h-3 rounded-full shrink-0 border-2 border-white shadow ${own ? 'bg-orange-500' : 'bg-blue-500'}"></span>
        <span class="text-[11px] font-semibold whitespace-nowrap px-1.5 py-0.5 rounded border shadow-sm ${own ? 'bg-orange-50 text-orange-700 border-orange-200' : 'bg-white text-slate-800 border-border'}">${esc(c.name)}</span>
      </div>`;
    }).join('');

    content.innerHTML = `
      <div class="bg-card border border-border rounded-xl p-6">
        <p class="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Y-axis &middot; ${esc(ya.label || '')}</p>
        <p class="text-xs text-slate-400 mb-4">${esc(ya.low || '')} (bottom) &rarr; ${esc(ya.high || '')} (top)</p>
        <div class="relative mx-auto bg-surface border border-border rounded-lg" style="width:100%; max-width:640px; aspect-ratio:1/1;">
          <div class="absolute left-1/2 top-0 bottom-0 w-px bg-border"></div>
          <div class="absolute top-1/2 left-0 right-0 h-px bg-border"></div>
          ${dots}
        </div>
        <p class="text-xs font-semibold text-slate-500 uppercase tracking-wide mt-4 mb-1">X-axis &middot; ${esc(xa.label || '')}</p>
        <p class="text-xs text-slate-400">${esc(xa.low || '')} (left) &rarr; ${esc(xa.high || '')} (right)</p>
      </div>
      <div class="grid sm:grid-cols-2 gap-3 mt-4">
        ${companies.map(c => `
          <div class="bg-card border border-border rounded-lg p-3 flex items-start gap-2">
            <span class="w-2.5 h-2.5 rounded-full mt-1 shrink-0 ${c.is_own ? 'bg-orange-500' : 'bg-blue-500'}"></span>
            <div class="min-w-0">
              <p class="text-sm font-semibold text-slate-900">${esc(c.name)}${c.is_own ? ' <span class="text-[10px] font-normal text-orange-600">(you)</span>' : ''}</p>
              <p class="text-xs text-slate-500 mt-0.5">${esc(c.rationale || '')}</p>
            </div>
          </div>`).join('')}
      </div>`;
  }

  // ── Feature / Claim Comparison Matrix ─────────────────
  let _matrixData = null;

  async function loadFeatureMatrix(force = false) {
    const btn     = document.getElementById('matrix-refresh-btn');
    const content = document.getElementById('matrix-content');
    if (_matrixData && !force) { renderFeatureMatrix(_matrixData); return; }
    btn.disabled = true;
    content.innerHTML = '<p class="text-slate-400 text-sm animate-pulse">Comparing feature claims…</p>';
    try {
      const data = await api('/api/insights/feature-matrix');
      _matrixData = data;
      renderFeatureMatrix(data);
    } catch (e) {
      content.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    } finally {
      btn.disabled = false;
    }
  }

  function renderFeatureMatrix(data) {
    const content = document.getElementById('matrix-content');
    const features = data.features || [];
    const companies = data.companies || [];
    if (!features.length || !companies.length) {
      content.innerHTML = '<p class="text-slate-500 text-sm">No feature comparison data yet.</p>';
      return;
    }
    const cellMap = {};
    (data.cells || []).forEach(c => { cellMap[c.feature + '::' + c.company] = c; });

    const STATUS = {
      yes:     { icon: '✓', cls: 'text-emerald-600 bg-emerald-50' },
      partial: { icon: '~', cls: 'text-amber-600 bg-amber-50' },
      no:      { icon: '—', cls: 'text-slate-300' },
    };

    const headerCols = companies.map(name =>
      `<th class="px-3 py-2.5 text-center text-xs font-semibold text-slate-700 border-b border-border min-w-[110px]">${esc(name)}</th>`
    ).join('');

    const rows = features.map(feature => {
      const cells = companies.map(company => {
        const cell = cellMap[feature + '::' + company] || { status: 'no' };
        const s = STATUS[cell.status] || STATUS.no;
        const tooltip = [cell.evidence, cell.url ? `Source: ${cell.url}` : ''].filter(Boolean).join('\n\n');
        return `<td class="px-3 py-2.5 text-center border-b border-border/60 ${s.cls} cursor-help" title="${esc(tooltip)}">
          <span class="font-bold">${s.icon}</span>
        </td>`;
      }).join('');
      return `<tr>
        <td class="px-3 py-2.5 text-sm text-slate-800 border-b border-border/60 border-r border-border/60 sticky left-0 bg-card whitespace-nowrap">${esc(feature)}</td>
        ${cells}
      </tr>`;
    }).join('');

    content.innerHTML = `
      <div class="overflow-x-auto rounded-lg border border-border">
        <table class="w-full text-left border-collapse text-sm">
          <thead><tr>
            <th class="px-3 py-2.5 text-left text-xs font-semibold text-slate-700 border-b border-border sticky left-0 bg-card">Feature / Claim</th>
            ${headerCols}
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="flex items-center gap-4 mt-3 text-xs text-slate-500">
        <span><span class="font-bold text-emerald-600">✓</span> Yes</span>
        <span><span class="font-bold text-amber-600">~</span> Partial</span>
        <span><span class="font-bold text-slate-300">—</span> No / not mentioned</span>
      </div>`;
  }

  // ── Kano-style Aspect Analysis ─────────────────────────
  let _kanoData = null;

  async function loadKanoAnalysis(force = false) {
    const btn     = document.getElementById('kano-refresh-btn');
    const content = document.getElementById('kano-content');
    if (_kanoData && !force) { renderKanoAnalysis(_kanoData); return; }
    btn.disabled = true;
    content.innerHTML = '<p class="text-slate-400 text-sm animate-pulse">Classifying product aspects…</p>';
    try {
      const data = await api('/api/insights/kano-analysis');
      _kanoData = data;
      renderKanoAnalysis(data);
    } catch (e) {
      content.innerHTML = `<p class="text-red-400 text-sm">Error: ${esc(e.message)}</p>`;
    } finally {
      btn.disabled = false;
    }
  }

  function renderKanoAnalysis(data) {
    const content = document.getElementById('kano-content');
    const aspects = data.aspects || [];
    if (!aspects.length) {
      content.innerHTML = '<p class="text-slate-500 text-sm">No Kano analysis data yet.</p>';
      return;
    }
    const GROUPS = [
      { key: 'must-be',     title: 'Must-be',     sub: 'Baseline — expected by every buyer',         border: 'border-slate-300',  text: 'text-slate-600' },
      { key: 'performance', title: 'Performance', sub: 'More-is-better — where they differentiate',  border: 'border-blue-300',   text: 'text-blue-600' },
      { key: 'delighter',   title: 'Delighter',   sub: 'Exciters — rare, unexpected wins',            border: 'border-violet-300', text: 'text-violet-600' },
    ];

    const cols = GROUPS.map(g => {
      const items = aspects.filter(a => a.category === g.key);
      const cards = items.map(a => `
        <div class="bg-card border border-border rounded-lg p-3.5">
          <p class="text-sm font-semibold text-slate-900">${esc(a.name)}</p>
          <p class="text-xs text-slate-500 mt-1 leading-relaxed">${esc(a.rationale || '')}</p>
          <div class="flex flex-wrap gap-1 mt-2">
            ${(a.offered_by || []).map(name => `<span class="text-[10px] font-medium bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded">${esc(name)}</span>`).join('')}
          </div>
        </div>`).join('') || '<p class="text-xs text-slate-400">None identified.</p>';
      return `
        <div>
          <div class="border-t-2 ${g.border} pt-2 mb-3">
            <p class="text-sm font-bold ${g.text}">${g.title}</p>
            <p class="text-xs text-slate-400">${g.sub}</p>
          </div>
          <div class="space-y-2.5">${cards}</div>
        </div>`;
    }).join('');

    content.innerHTML = `<div class="grid grid-cols-1 lg:grid-cols-3 gap-5">${cols}</div>`;
  }

  // ── GTM Heatmap ─────────────────────────────────────
  let _gtmData = null;
  let _gtmLens  = 'both';
  const GTM_STRENGTH_BG    = ['rgba(30,41,59,0.25)','rgba(71,85,105,0.65)','rgba(146,64,14,0.60)','rgba(194,65,12,0.80)','rgba(185,28,28,0.90)'];
  const GTM_STRENGTH_LABEL = ['','Weak','Moderate','Strong','Dominant'];
  const GTM_STRENGTH_TEXT  = ['#475569','#94a3b8','#fcd34d','#fb923c','#fca5a5'];

  async function loadGtmHeatmap() {
    const btn = document.getElementById('gtm-refresh-btn');
    const loading = document.getElementById('gtm-loading');
    const errEl   = document.getElementById('gtm-error');
    const empty   = document.getElementById('gtm-empty');
    const tWrap   = document.getElementById('gtm-table-wrap');
    const legend  = document.getElementById('gtm-legend');
    const cardsEl = document.getElementById('gtm-cards');
    btn.disabled = true;
    loading.classList.remove('hidden');
    [errEl, empty, tWrap, legend, cardsEl].forEach(el => el.classList.add('hidden'));
    try {
      const data = await api('/api/insights/gtm-heatmap');
      _gtmData = data;
      renderGtmHeatmap(data);
    } catch (e) {
      errEl.textContent = 'Error: ' + e.message;
      errEl.classList.remove('hidden');
      empty.classList.remove('hidden');
    } finally {
      loading.classList.add('hidden');
      btn.disabled = false;
    }
  }

  function renderGtmHeatmap(data) {
    const tWrap   = document.getElementById('gtm-table-wrap');
    const legend  = document.getElementById('gtm-legend');
    const cardsEl = document.getElementById('gtm-cards');
    const empty   = document.getElementById('gtm-empty');
    const segments    = data.segments    || [];
    const competitors = data.competitors || [];
    if (!segments.length || !competitors.length) {
      empty.textContent = 'Not enough competitor data. Scrape your competitors first.';
      empty.classList.remove('hidden');
      return;
    }
    const cellMap = {};
    for (const c of (data.cells || [])) cellMap[c.segment_id + '::' + c.competitor_id] = c;

    // Rebuild thead
    const theadRow = document.getElementById('gtm-thead-row');
    while (theadRow.children.length > 1) theadRow.removeChild(theadRow.lastChild);
    for (const comp of competitors) {
      const th = document.createElement('th');
      th.scope = 'col';
      if (comp.is_own) {
        th.className = 'text-center text-xs font-semibold uppercase tracking-wide px-2 py-3 min-w-[90px]';
        th.style.cssText = 'color:#fb923c';
        th.innerHTML = esc(comp.name) + '<br><span style="font-size:9px;opacity:.75;text-transform:none;letter-spacing:0">Your Company</span>';
      } else {
        th.className = 'text-center text-xs font-semibold text-slate-400 uppercase tracking-wide px-2 py-3 min-w-[90px]';
        th.textContent = comp.name;
      }
      theadRow.appendChild(th);
    }
    const stTh = document.createElement('th');
    stTh.scope = 'col';
    stTh.className = 'text-center text-xs font-semibold text-slate-400 uppercase tracking-wide px-3 py-3 min-w-[100px] border-l border-border/30';
    stTh.textContent = 'Status';
    theadRow.appendChild(stTh);

    // Build rows
    const tbody = document.getElementById('gtm-tbody');
    tbody.innerHTML = '';
    const STATUS_MAP = {
      safe:      { cls: 'bg-emerald-900/40 text-emerald-400 border-emerald-800/50', label: 'Safe' },
      contested: { cls: 'bg-amber-900/40 text-amber-400 border-amber-800/50',       label: 'Contested' },
      'at-risk': { cls: 'bg-red-900/40 text-red-400 border-red-800/50',             label: 'At Risk' },
    };
    for (const seg of segments) {
      const tr = document.createElement('tr');
      tr.className = 'border-t border-border/40 hover:bg-slate-800/20 transition';

      const nameTd = document.createElement('td');
      nameTd.className = 'px-4 py-3 sticky left-0 bg-surface z-10 border-r border-border/30 align-top';
      nameTd.innerHTML = '<div class="font-medium text-slate-900 text-sm leading-snug">' + esc(seg.name) + '</div>' +
        (seg.description ? '<div class="text-xs text-slate-500 mt-0.5 max-w-xs leading-snug">' + esc(seg.description) + '</div>' : '');
      tr.appendChild(nameTd);

      for (const comp of competitors) {
        const cell     = cellMap[seg.id + '::' + comp.id] || { strength:0, trajectory:'flat', encroachment:0 };
        const strength = Math.min(4, Math.max(0, cell.strength     || 0));
        const encroach = Math.min(2, Math.max(0, cell.encroachment || 0));
        const traj     = cell.trajectory || 'flat';
        const ring     = encroach === 0 ? '' : encroach === 1
          ? 'box-shadow:0 0 0 2px rgba(251,191,36,0.75);'
          : 'box-shadow:0 0 0 3px rgba(239,68,68,0.90);';
        const arrowCol = traj === 'up' ? '#4ade80' : traj === 'down' ? '#f87171' : '#64748b';
        const arrowSym = traj === 'up' ? '&#8593;' : traj === 'down' ? '&#8595;' : '&#8594;';
        const td = document.createElement('td');
        td.className = 'p-1.5 border-l border-border/20 align-middle';
        const div = document.createElement('div');
        div.className = 'gtm-cell relative rounded-lg flex items-center justify-center transition-opacity';
        div.style.cssText = 'min-width:80px;height:56px;background:' + GTM_STRENGTH_BG[strength] + ';' + ring;
        div.dataset.strength     = strength;
        div.dataset.encroachment = encroach;
        div.dataset.trajectory   = traj;
        div.title = comp.name + ' × ' + seg.name + ': ' + (GTM_STRENGTH_LABEL[strength]||'No presence') + ' | ' + traj + ' | encroach ' + encroach;
        if (strength > 0 || encroach > 0) {
          div.innerHTML = '<span class="absolute top-1 right-1.5 text-xs font-bold" style="color:' + arrowCol + '">' + arrowSym + '</span>' +
            (strength > 0 ? '<span class="text-xs font-semibold" style="color:' + GTM_STRENGTH_TEXT[strength] + '">' + GTM_STRENGTH_LABEL[strength] + '</span>' : '');
        }
        td.appendChild(div);
        tr.appendChild(td);
      }

      const stTd = document.createElement('td');
      stTd.className = 'px-3 py-3 text-center border-l border-border/20 align-middle';
      const st = STATUS_MAP[seg.status] || STATUS_MAP.contested;
      stTd.innerHTML = '<span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold border ' + st.cls + '">' + st.label + '</span>';
      tr.appendChild(stTd);
      tbody.appendChild(tr);
    }

    tWrap.classList.remove('hidden');
    legend.classList.remove('hidden');
    cardsEl.classList.remove('hidden');
    empty.classList.add('hidden');
    _renderGtmCards(data);
    applyGtmLens();
  }

  function _renderGtmCards(data) {
    function buildCard(id, cfg, accentCls, dotCls, iconHtml) {
      const segs = (cfg.segments||[]).map(s =>
        '<span class="text-xs ' + accentCls + ' px-2 py-0.5 rounded-full border">' + esc(s) + '</span>'
      ).join('');
      const acts = (cfg.actions||[]).map(a =>
        '<li class="flex items-start gap-2 text-xs text-slate-700"><span class="mt-1 shrink-0 w-1.5 h-1.5 rounded-full ' + dotCls + '"></span>' + esc(a) + '</li>'
      ).join('');
      document.getElementById(id).innerHTML =
        '<div class="flex items-start gap-3 mb-3">' + iconHtml +
        '<div><p class="text-sm font-semibold text-slate-900 leading-snug">' + esc(cfg.headline||'No recommendation.') + '</p></div></div>' +
        (segs ? '<div class="flex flex-wrap gap-1.5 mb-3">' + segs + '</div>' : '') +
        (cfg.rationale ? '<p class="text-sm text-slate-400 leading-relaxed mb-3">' + esc(cfg.rationale) + '</p>' : '') +
        (acts ? '<ul class="space-y-1.5">' + acts + '</ul>' : '');
    }
    buildCard('gtm-defend-card', data.defend||{},
      'text-red-300 bg-red-900/30 border-red-800/40', 'bg-red-500',
      '<div class="w-8 h-8 shrink-0 rounded-lg bg-red-900/40 border border-red-800/50 flex items-center justify-center"><svg xmlns="http://www.w3.org/2000/svg" class="w-4 h-4 text-red-400" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.955 11.955 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z"/></svg></div>'
    );
    buildCard('gtm-attack-card', data.attack||{},
      'text-emerald-300 bg-emerald-900/30 border-emerald-800/40', 'bg-emerald-500',
      '<div class="w-8 h-8 shrink-0 rounded-lg bg-emerald-900/40 border border-emerald-800/50 flex items-center justify-center"><svg xmlns="http://www.w3.org/2000/svg" class="w-4 h-4 text-emerald-400" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M15.59 14.37a6 6 0 01-5.84 7.38v-4.8m5.84-2.58a14.98 14.98 0 006.16-12.12A14.98 14.98 0 009.631 8.41m5.96 5.96a14.926 14.926 0 01-5.841 2.58m-.119-8.54a6 6 0 00-7.381 5.84h4.8m2.581-5.84a14.927 14.927 0 00-2.58 5.84m2.699 2.7c-.103.021-.207.041-.311.06a15.09 15.09 0 01-2.448-2.448 14.9 14.9 0 01.06-.312m-2.24 2.39a4.493 4.493 0 00-1.757 4.306 4.493 4.493 0 004.306-1.758M16.5 9a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0z"/></svg></div>'
    );
  }

  function setGtmLens(lens) {
    _gtmLens = lens;
    document.querySelectorAll('.gtm-lens-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.lens === lens)
    );
    applyGtmLens();
  }

  function applyGtmLens() {
    document.querySelectorAll('.gtm-cell').forEach(cell => {
      const s = parseInt(cell.dataset.strength,     10) || 0;
      const e = parseInt(cell.dataset.encroachment, 10) || 0;
      const dim = _gtmLens === 'defense' ? e === 0 : _gtmLens === 'offense' ? s >= 3 : false;
      cell.style.opacity = dim ? '0.12' : '1';
    });
  }

  // ── Positioning Teardown ─────────────────────────────
  let _teardownData = null;

  function showGtmTab(tab) {
    ['heatmap','positioning','messaging','house','battlecards'].forEach(t => {
      document.getElementById('gtm-sub-' + t).classList.toggle('hidden', t !== tab);
      const btn = document.querySelector('[data-gtm-tab="' + t + '"]');
      btn.classList.toggle('active', t === tab);
      btn.setAttribute('aria-selected', t === tab ? 'true' : 'false');
    });
  }

  async function loadPositioningTeardown() {
    const btn     = document.getElementById('td-refresh-btn');
    const loading = document.getElementById('td-loading');
    const errEl   = document.getElementById('td-error');
    const grid    = document.getElementById('td-grid');
    const empty   = document.getElementById('td-empty');
    btn.disabled = true;
    loading.classList.remove('hidden');
    [errEl, grid, empty].forEach(el => el.classList.add('hidden'));
    try {
      const data = await api('/api/insights/positioning-teardown');
      _teardownData = data;
      renderPositioningTeardown(data);
    } catch (e) {
      errEl.textContent = 'Error: ' + e.message;
      errEl.classList.remove('hidden');
      empty.classList.remove('hidden');
    } finally {
      loading.classList.add('hidden');
      btn.disabled = false;
    }
  }

  function renderPositioningTeardown(data) {
    const grid  = document.getElementById('td-grid');
    const empty = document.getElementById('td-empty');
    const list  = (data.competitors || []).slice();
    if (!list.length) { empty.classList.remove('hidden'); return; }
    list.sort((a, b) => (a.type === 'you' ? -1 : b.type === 'you' ? 1 : 0));
    grid.innerHTML = list.map(c => buildTeardownCard(c)).join('');
    grid.classList.remove('hidden');
    empty.classList.add('hidden');
  }

  function buildTeardownCard(c) {
    const isOwn      = c.type === 'you';
    const cardStyle  = isOwn
      ? 'background:rgba(251,146,60,0.06);border:1px solid rgba(251,146,60,0.4);'
      : 'background:rgba(30,41,59,0.45);border:1px solid rgba(51,65,85,0.8);';
    const hairline   = isOwn ? 'border-color:rgba(251,146,60,0.2)' : 'border-color:rgba(255,255,255,0.07)';

    const FIELDS = [
      { key:'against', label:'AGAINST' },
      { key:'for',     label:'FOR'     },
      { key:'claim',   label:'CLAIM'   },
      { key:'proof',   label:'PROOF'   },
    ];
    const rows = FIELDS.map((f, i) => {
      const raw = c[f.key] || null;
      const valHtml = !raw
        ? '<span style="color:#4b5563">&mdash;</span>'
        : f.key === 'claim'
          ? '<span class="td-claim">&ldquo;' + esc(raw) + '&rdquo;</span>'
          : '<span class="td-value">' + esc(raw) + '</span>';
      return '<div class="flex gap-3 py-3' + (i > 0 ? ' border-t' : '') + '"'
        + (i > 0 ? ' style="' + hairline + '"' : '') + ' role="row">'
        + '<span class="td-label w-[4.5rem] shrink-0 pt-0.5" aria-label="' + f.label + '">' + f.label + '</span>'
        + '<div class="flex-1 min-w-0">' + valHtml + '</div></div>';
    }).join('');

    const ownBadge = isOwn
      ? '<span class="ml-2 text-[10px] font-semibold px-1.5 py-0.5 rounded" style="background:rgba(251,146,60,0.18);color:#fb923c">YOUR COMPANY</span>'
      : '';

    return '<article class="rounded-xl p-5 flex flex-col focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500"'
      + ' style="' + cardStyle + '" tabindex="0" aria-label="' + esc(c.name) + ' positioning teardown">'
      + '<header class="flex items-center gap-2 pb-3 mb-1" style="border-bottom:1px solid ' + (isOwn ? 'rgba(251,146,60,0.25)' : 'rgba(255,255,255,0.07)') + '">'
        + '<span class="font-bold text-sm">' + esc(c.name) + '</span>'
        + ownBadge
      + '</header>'
      + '<div role="table" aria-label="' + esc(c.name) + ' positioning fields">' + rows + '</div>'
      + '</article>';
  }

  // ── Campaign Messaging ────────────────────────────────
  let _messagingData  = null;
  let _activeChannel  = null;

  async function loadCampaignMessaging() {
    const btn     = document.getElementById('cm-refresh-btn');
    const loading = document.getElementById('cm-loading');
    const errEl   = document.getElementById('cm-error');
    const content = document.getElementById('cm-content');
    const empty   = document.getElementById('cm-empty');
    btn.disabled = true;
    loading.classList.remove('hidden');
    [errEl, content, empty].forEach(el => el.classList.add('hidden'));
    try {
      const data = await api('/api/insights/campaign-messaging');
      _messagingData = data;
      renderCampaignMessaging(data);
    } catch (e) {
      errEl.textContent = 'Error: ' + e.message;
      errEl.classList.remove('hidden');
      empty.classList.remove('hidden');
    } finally {
      loading.classList.add('hidden');
      btn.disabled = false;
    }
  }

  function renderCampaignMessaging(data) {
    const content  = document.getElementById('cm-content');
    const empty    = document.getElementById('cm-empty');
    const channels = data.channels || [];
    if (!channels.length) { empty.classList.remove('hidden'); return; }

    // Strategic summary
    const summaryEl = document.getElementById('cm-summary');
    summaryEl.innerHTML = data.strategic_summary
      ? '<p class="text-sm text-blue-900 leading-relaxed">'
        + '<span class="font-semibold text-blue-700 mr-1.5">Strategic opportunity:</span>'
        + esc(data.strategic_summary) + '</p>'
      : '';

    // Channel pills
    const navEl = document.getElementById('cm-channel-nav');
    navEl.innerHTML = channels.map((ch, i) =>
      '<button class="cm-channel-pill px-3 py-1.5 text-xs font-medium rounded-full border transition'
      + (i === 0 ? ' active' : '') + '" data-ch-idx="' + i + '" onclick="switchCmChannel(' + i + ')">'
      + esc(ch.name) + '</button>'
    ).join('');

    _activeChannel = 0;
    renderCmCards(channels[0]);
    content.classList.remove('hidden');
    empty.classList.add('hidden');
  }

  function switchCmChannel(idx) {
    document.querySelectorAll('.cm-channel-pill').forEach((b, i) =>
      b.classList.toggle('active', i === idx)
    );
    _activeChannel = idx;
    renderCmCards((_messagingData.channels || [])[idx]);
  }

  function renderCmCards(channel) {
    const cardsEl = document.getElementById('cm-cards');
    if (!channel) { cardsEl.innerHTML = ''; return; }
    cardsEl.innerHTML = (channel.messages || []).map(m => buildCmCard(m)).join('');
  }

  function buildCmCard(m) {
    return '<article class="rounded-xl p-5 flex flex-col gap-3" style="background:rgba(30,41,59,0.55);border:1px solid rgba(51,65,85,0.8);">'
      + '<div class="flex flex-wrap gap-1.5">'
        + '<span class="text-[10px] font-semibold px-2 py-0.5 rounded-full" style="background:rgba(99,102,241,0.18);color:#a5b4fc">'
          + esc(m.icp || '') + '</span>'
        + (m.angle ? '<span class="text-[10px] font-medium px-2 py-0.5 rounded-full" style="background:rgba(16,185,129,0.12);color:#6ee7b7">'
          + esc(m.angle) + '</span>' : '')
      + '</div>'
      + '<h3 class="font-bold text-white leading-snug" style="font-size:.9375rem">' + esc(m.headline || '') + '</h3>'
      + '<p class="text-sm text-slate-400 leading-relaxed flex-1">' + esc(m.body || '') + '</p>'
      + (m.cta ? '<div class="pt-1"><span class="inline-block text-xs font-semibold px-3 py-1.5 rounded-lg" style="background:rgba(59,130,246,0.18);color:#93c5fd">'
        + esc(m.cta) + '</span></div>' : '')
      + '</article>';
  }

  // ── Messaging House ──────────────────────────────────
  let _messagingHouseData = null;

  async function loadMessagingHouse() {
    const btn     = document.getElementById('mh-refresh-btn');
    const loading = document.getElementById('mh-loading');
    const errEl   = document.getElementById('mh-error');
    const content = document.getElementById('mh-content');
    const empty   = document.getElementById('mh-empty');
    btn.disabled = true;
    loading.classList.remove('hidden');
    [errEl, content, empty].forEach(el => el.classList.add('hidden'));
    try {
      const data = await api('/api/insights/messaging-house');
      _messagingHouseData = data;
      renderMessagingHouse(data);
    } catch (e) {
      errEl.textContent = 'Error: ' + e.message;
      errEl.classList.remove('hidden');
      empty.classList.remove('hidden');
    } finally {
      loading.classList.add('hidden');
      btn.disabled = false;
    }
  }

  function renderMessagingHouse(data) {
    const content = document.getElementById('mh-content');
    const empty   = document.getElementById('mh-empty');
    const pillars = data.pillars || [];
    if (!pillars.length && !data.tagline) { empty.classList.remove('hidden'); return; }

    const pillarCards = pillars.map(p => `
      <div class="bg-card border border-border rounded-xl p-5">
        <p class="text-xs font-bold text-blue-600 uppercase tracking-wide mb-2">${esc(p.name)}</p>
        <p class="text-sm font-semibold text-slate-900 mb-3">${esc(p.message)}</p>
        <ul class="space-y-1.5">
          ${(p.proof_points || []).map(pt => `<li class="flex items-start gap-2 text-xs text-slate-500"><span class="text-blue-400 mt-0.5">&#9679;</span>${esc(pt)}</li>`).join('')}
        </ul>
      </div>`).join('');

    content.innerHTML = `
      <div class="rounded-xl p-6 mb-5 text-center" style="background:linear-gradient(135deg,#12314f,#1B4370)">
        <p class="text-xs font-semibold text-blue-300 uppercase tracking-widest mb-2">Tagline</p>
        <p class="text-2xl font-bold text-white mb-4">${esc(data.tagline || '')}</p>
        <p class="text-sm text-blue-100 max-w-2xl mx-auto leading-relaxed">${esc(data.positioning_statement || '')}</p>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">${pillarCards}</div>`;
    content.classList.remove('hidden');
  }

  // ── Battlecards ──────────────────────────────────────
  let _battlecardsData = null;

  async function loadBattlecards() {
    const btn     = document.getElementById('bc-refresh-btn');
    const loading = document.getElementById('bc-loading');
    const errEl   = document.getElementById('bc-error');
    const content = document.getElementById('bc-content');
    const empty   = document.getElementById('bc-empty');
    btn.disabled = true;
    loading.classList.remove('hidden');
    [errEl, content, empty].forEach(el => el.classList.add('hidden'));
    try {
      const data = await api('/api/insights/battlecards');
      _battlecardsData = data;
      renderBattlecards(data);
    } catch (e) {
      errEl.textContent = 'Error: ' + e.message;
      errEl.classList.remove('hidden');
      empty.classList.remove('hidden');
    } finally {
      loading.classList.add('hidden');
      btn.disabled = false;
    }
  }

  function renderBattlecards(data) {
    const content = document.getElementById('bc-content');
    const empty   = document.getElementById('bc-empty');
    const cards   = data.battlecards || [];
    if (!cards.length) { empty.classList.remove('hidden'); return; }
    const ownName = data.own_company_name || 'Your company';

    const header = `
      <div class="lg:col-span-2 flex items-center gap-2 mb-1">
        <span class="text-sm font-bold text-blue-700">${esc(ownName)}</span>
        <span class="text-xs font-semibold text-slate-400 uppercase tracking-wide">vs the competition</span>
      </div>`;

    const cardsHtml = cards.map(c => `
      <div class="bg-card border border-border rounded-xl overflow-hidden">
        <div class="px-5 py-3.5 border-b border-border bg-surface">
          <p class="text-xs text-slate-400 mb-0.5"><span class="font-semibold text-blue-700">${esc(ownName)}</span> vs</p>
          <p class="font-bold text-slate-900 text-sm">${esc(c.competitor)}</p>
          <p class="text-xs text-slate-500 mt-0.5">${esc(c.overview || '')}</p>
        </div>
        <div class="p-5 space-y-4">
          <div class="grid grid-cols-2 gap-3">
            <div>
              <p class="text-[10px] font-bold text-blue-600 uppercase tracking-wide mb-1.5">${esc(ownName)} strengths</p>
              <ul class="space-y-1">${(c.your_strengths || []).map(s => `<li class="text-xs text-slate-600 flex items-start gap-1.5"><span class="text-blue-400 mt-0.5">&#9679;</span>${esc(s)}</li>`).join('')}</ul>
            </div>
            <div>
              <p class="text-[10px] font-bold text-red-600 uppercase tracking-wide mb-1.5">${esc(c.competitor)} strengths</p>
              <ul class="space-y-1">${(c.their_strengths || []).map(s => `<li class="text-xs text-slate-600 flex items-start gap-1.5"><span class="text-red-400 mt-0.5">&#9679;</span>${esc(s)}</li>`).join('')}</ul>
            </div>
          </div>
          <div>
            <p class="text-[10px] font-bold text-emerald-600 uppercase tracking-wide mb-1.5">${esc(c.competitor)} weaknesses</p>
            <ul class="space-y-1">${(c.their_weaknesses || []).map(s => `<li class="text-xs text-slate-600 flex items-start gap-1.5"><span class="text-emerald-400 mt-0.5">&#9679;</span>${esc(s)}</li>`).join('')}</ul>
          </div>
          <div>
            <p class="text-[10px] font-bold text-slate-500 uppercase tracking-wide mb-1.5">Objection handling</p>
            <div class="space-y-2">
              ${(c.objections || []).map(o => `
                <div class="bg-surface border border-border rounded-lg p-2.5">
                  <p class="text-xs font-semibold text-slate-700">&ldquo;${esc(o.objection)}&rdquo;</p>
                  <p class="text-xs text-slate-500 mt-1">&#8594; ${esc(o.response)}</p>
                </div>`).join('')}
            </div>
          </div>
          <div class="bg-blue-50 border border-blue-100 rounded-lg p-3">
            <p class="text-[10px] font-bold text-blue-700 uppercase tracking-wide mb-1.5">Why ${esc(ownName)} wins</p>
            <ul class="space-y-1">${(c.why_we_win || []).map(s => `<li class="text-xs text-slate-700 flex items-start gap-1.5"><span class="text-blue-500 mt-0.5">&#9679;</span>${esc(s)}</li>`).join('')}</ul>
          </div>
          <div>
            <p class="text-[10px] font-bold text-amber-600 uppercase tracking-wide mb-1.5">Landmines to plant</p>
            <ul class="space-y-1">${(c.landmines || []).map(s => `<li class="text-xs text-slate-600 flex items-start gap-1.5"><span class="text-amber-400 mt-0.5">&#9679;</span>${esc(s)}</li>`).join('')}</ul>
          </div>
        </div>
      </div>`).join('');

    content.innerHTML = header + cardsHtml;
    content.classList.remove('hidden');
  }

  // ── Utilities ────────────────────────────────────────
  function esc(s) {
    return String(s ?? '')
      .replace(/&/g,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  }

  /** Minimal markdown → HTML (bold, italic, inline code, line breaks) */
  function mdToHtml(text) {
    return esc(text)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g,     '<em>$1</em>')
      .replace(/`(.+?)`/g,       '<code class="bg-slate-700 px-1 rounded text-xs">$1</code>')
      .replace(/\[Source (\d+)\]/g, '<span class="text-blue-400 font-medium">[Source $1]</span>')
      .replace(/\n/g, '<br/>');
  }

  let _toastTimer;
  function showToast(msg, error = false) {
    let el = document.getElementById('toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toast';
      el.style.cssText = 'position:fixed;bottom:1.5rem;right:1.5rem;z-index:99;padding:.6rem 1.1rem;border-radius:.75rem;font-size:.85rem;font-weight:500;transition:opacity .3s;';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.background = error ? '#7f1d1d' : '#14532d';
    el.style.color       = error ? '#fca5a5' : '#6ee7b7';
    el.style.opacity     = '1';
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { el.style.opacity = '0'; }, 3500);
  }

  // ── Boot ─────────────────────────────────────────────
  // ── Onboarding (Task 7) ───────────────────────────────
  const OB_STEPS = [
    { title: 'Create your workspace',    sub: 'Give your team a home.' },
    { title: 'Your company',             sub: 'Index your own web presence as a baseline.' },
    { title: 'Add your first rival',     sub: 'Add up to three competitors to watch.' },
    { title: 'Invite your team',         sub: 'Optional — you can do this later in Settings.' },
    { title: 'Start your first sweep',   sub: 'Kick off the initial crawl and watch it run.' },
  ];
  let _obStep = 0;
  let _obState = {}; // persisted to sessionStorage

  function _obLoad() {
    try { _obState = JSON.parse(sessionStorage.getItem('ob_state') || '{}'); } catch { _obState = {}; }
    _obStep = parseInt(_obState.step || '0', 10);
  }
  function _obSave(patch = {}) {
    Object.assign(_obState, patch, { step: _obStep });
    sessionStorage.setItem('ob_state', JSON.stringify(_obState));
  }

  function startOnboarding() {
    _obLoad();
    document.getElementById('ob-modal').style.display = 'flex';
    _obRender();
  }

  function _obClose() {
    document.getElementById('ob-modal').style.display = 'none';
    sessionStorage.removeItem('ob_state');
    showPage('dashboard');
  }

  function _obRender() {
    const s = OB_STEPS[_obStep];
    document.getElementById('ob-title').textContent = s.title;
    document.getElementById('ob-sub').textContent   = s.sub;

    // Progress dots
    document.getElementById('ob-steps').innerHTML = OB_STEPS.map((_, i) => {
      const cls = i < _obStep  ? 'w-2 h-2 rounded-full bg-blue-500'
                : i === _obStep ? 'w-2 h-2 rounded-full bg-blue-600 ring-2 ring-blue-200'
                :                  'w-2 h-2 rounded-full bg-slate-200';
      return `<span class="${cls}"></span>`;
    }).join('');

    const renders = [_obRender1, _obRender2, _obRender3, _obRender4, _obRender5];
    renders[_obStep]();
  }

  // Step 1 — Create workspace
  function _obRender1() {
    document.getElementById('ob-body').innerHTML = `
      <div class="space-y-3">
        <div><label class="block text-xs font-semibold text-slate-500 mb-1">Workspace name</label>
          <input id="ob-ws-name" type="text" placeholder="e.g. Acme Corp Intelligence"
            class="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            value="${_esc(_obState.ws_name || '')}"/>
        </div>
        <p id="ob-err1" class="text-xs text-red-500 hidden"></p>
      </div>`;
    document.getElementById('ob-footer').innerHTML = `
      <span></span>
      <button onclick="_obNext1()" class="bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium px-5 py-2 rounded-lg transition">Continue →</button>`;
  }

  async function _obNext1() {
    const name = document.getElementById('ob-ws-name').value.trim();
    const err  = document.getElementById('ob-err1');
    if (!name) { err.textContent = 'Please enter a workspace name.'; err.classList.remove('hidden'); return; }
    err.classList.add('hidden');
    try {
      const ws = await api('/api/workspaces', { method: 'POST', body: JSON.stringify({ name }) });
      _workspaceId = ws.id;
      sessionStorage.setItem('sh_workspace_id', ws.id);
      // Refresh _me to include the new workspace
      _me = await api('/api/me');
      _renderUserMenu();
      _obSave({ ws_name: name, ws_id: ws.id });
      _obStep = 1; _obRender();
    } catch(e) {
      document.getElementById('ob-err1').textContent = e.message;
      document.getElementById('ob-err1').classList.remove('hidden');
    }
  }

  // Step 2 — Your company
  function _obRender2() {
    document.getElementById('ob-body').innerHTML = `
      <div class="space-y-3">
        <div><label class="block text-xs font-semibold text-slate-500 mb-1">Company name</label>
          <input id="ob-co-name" type="text" placeholder="Acme Corp"
            class="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            value="${_esc(_obState.co_name || '')}"/></div>
        <div><label class="block text-xs font-semibold text-slate-500 mb-1">Company website</label>
          <input id="ob-co-url" type="url" placeholder="https://acmecorp.com"
            class="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            value="${_esc(_obState.co_url || '')}"/></div>
        <p class="text-xs text-slate-400">We'll index your website as a baseline for comparisons.</p>
        <p id="ob-err2" class="text-xs text-red-500 hidden"></p>
      </div>`;
    document.getElementById('ob-footer').innerHTML = `
      <button onclick="_obBack()" class="text-sm text-slate-500 hover:text-slate-800 px-4 py-2 rounded-lg transition">← Back</button>
      <button onclick="_obNext2()" class="bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium px-5 py-2 rounded-lg transition">Continue →</button>`;
  }

  async function _obNext2() {
    const name = document.getElementById('ob-co-name').value.trim();
    const url  = document.getElementById('ob-co-url').value.trim();
    const err  = document.getElementById('ob-err2');
    if (!name || !url) { err.textContent = 'Please fill in both fields.'; err.classList.remove('hidden'); return; }
    err.classList.add('hidden');
    try {
      await api(`/api/workspaces/${_workspaceId}`, {
        method: 'PATCH',
        body: JSON.stringify({ company_name: name, company_url: url }),
      });
      await api('/api/sources/', {
        method: 'POST',
        body: JSON.stringify({ name, url, category: 'own', scrape_interval: 24 }),
      });
      _obSave({ co_name: name, co_url: url });
      _obStep = 2; _obRender();
    } catch(e) {
      err.textContent = e.message; err.classList.remove('hidden');
    }
  }

  // Step 3 — Add rivals
  function _obRender3() {
    const rivals = _obState.rivals || [{ name: '', url: '' }];
    const rows = rivals.map((r, i) => `
      <div class="flex gap-2 items-start" id="ob-rival-row-${i}">
        <input type="text" placeholder="Name" value="${_esc(r.name)}"
          class="w-1/3 border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          oninput="_obUpdateRival(${i},'name',this.value)"/>
        <input type="url" placeholder="https://competitor.com" value="${_esc(r.url)}"
          class="flex-1 border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          oninput="_obUpdateRival(${i},'url',this.value)"/>
        ${rivals.length > 1 ? `<button onclick="_obRemoveRival(${i})" class="text-slate-400 hover:text-red-500 transition pt-2">✕</button>` : ''}
      </div>`).join('');
    document.getElementById('ob-body').innerHTML = `
      <div class="space-y-3">
        ${rows}
        ${rivals.length < 3 ? `<button onclick="_obAddRival()" class="text-xs text-blue-600 hover:underline">+ Add another rival</button>` : ''}
        <p id="ob-err3" class="text-xs text-red-500 hidden"></p>
      </div>`;
    document.getElementById('ob-footer').innerHTML = `
      <button onclick="_obBack()" class="text-sm text-slate-500 hover:text-slate-800 px-4 py-2 rounded-lg transition">← Back</button>
      <div class="flex gap-2">
        <button onclick="_obStep=3;_obSave();_obRender()" class="text-sm text-slate-400 hover:text-slate-700 px-4 py-2 rounded-lg transition">Skip for now</button>
        <button onclick="_obNext3()" class="bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium px-5 py-2 rounded-lg transition">Continue →</button>
      </div>`;
  }

  function _obUpdateRival(i, key, val) {
    const rivals = _obState.rivals || [{ name: '', url: '' }];
    rivals[i] = { ...rivals[i], [key]: val };
    _obSave({ rivals });
  }
  function _obAddRival() {
    const rivals = _obState.rivals || [{ name: '', url: '' }];
    rivals.push({ name: '', url: '' });
    _obSave({ rivals }); _obRender();
  }
  function _obRemoveRival(i) {
    const rivals = (_obState.rivals || []).filter((_, j) => j !== i);
    _obSave({ rivals }); _obRender();
  }

  async function _obNext3() {
    const rivals = (_obState.rivals || []).filter(r => r.url.trim());
    if (!rivals.length) {
      document.getElementById('ob-err3').textContent = 'Add at least one rival, or click Skip.';
      document.getElementById('ob-err3').classList.remove('hidden'); return;
    }
    document.getElementById('ob-err3').classList.add('hidden');
    for (const r of rivals) {
      try {
        await api('/api/sources/', {
          method: 'POST',
          body: JSON.stringify({ name: r.name || r.url, url: r.url, category: 'competitor', scrape_interval: 24 }),
        });
      } catch { /* skip duplicates */ }
    }
    _obSave({ rivals_added: true });
    _obStep = 3; _obRender();
  }

  // Step 4 — Invite team (optional)
  function _obRender4() {
    document.getElementById('ob-body').innerHTML = `
      <div class="space-y-3">
        <div class="flex gap-2">
          <input id="ob-invite-email" type="email" placeholder="colleague@company.com"
            class="flex-1 border border-slate-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
          <button onclick="_obSendInvite()" class="bg-blue-600 hover:bg-blue-500 text-white text-sm px-4 py-2 rounded-lg transition">Invite</button>
        </div>
        <div id="ob-invited-list" class="space-y-1 text-xs text-slate-500"></div>
        <p id="ob-invite-msg" class="text-xs hidden"></p>
      </div>`;
    document.getElementById('ob-footer').innerHTML = `
      <button onclick="_obBack()" class="text-sm text-slate-500 hover:text-slate-800 px-4 py-2 rounded-lg transition">← Back</button>
      <button onclick="_obStep=4;_obSave();_obRender()" class="bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium px-5 py-2 rounded-lg transition">Continue →</button>`;
    _obRefreshInvited();
  }

  function _obRefreshInvited() {
    const list = _obState.invited || [];
    document.getElementById('ob-invited-list').innerHTML = list.map(e =>
      `<span class="inline-flex items-center gap-1 bg-blue-50 text-blue-600 px-2 py-0.5 rounded-full">${_esc(e)} ✓</span>`
    ).join(' ');
  }

  async function _obSendInvite() {
    const email = document.getElementById('ob-invite-email').value.trim();
    const msg   = document.getElementById('ob-invite-msg');
    if (!email) return;
    try {
      await api(`/api/workspaces/${_workspaceId}/invites`, {
        method: 'POST', body: JSON.stringify({ email, role: 'member' }),
      });
      const invited = [...(_obState.invited || []), email];
      _obSave({ invited });
      document.getElementById('ob-invite-email').value = '';
      msg.className = 'text-xs text-emerald-600'; msg.textContent = `Invite sent to ${email}`;
      msg.classList.remove('hidden');
      _obRefreshInvited();
    } catch(e) {
      msg.className = 'text-xs text-red-500'; msg.textContent = e.message; msg.classList.remove('hidden');
    }
  }

  // Step 5 — First sweep
  function _obRender5() {
    document.getElementById('ob-body').innerHTML = `
      <div class="space-y-4">
        <p class="text-sm text-slate-600">We'll kick off the first crawl now. You can close this and check the Dashboard while it runs.</p>
        <div id="ob-sweep-status" class="text-sm text-slate-500"></div>
      </div>`;
    document.getElementById('ob-footer').innerHTML = `
      <span></span>
      <button onclick="_obFinish()" id="ob-finish-btn" class="bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium px-5 py-2 rounded-lg transition">Start sweep &amp; open app</button>`;
  }

  async function _obFinish() {
    document.getElementById('ob-finish-btn').disabled = true;
    document.getElementById('ob-sweep-status').textContent = 'Starting scrapes…';
    try {
      await api('/api/scraper/run-all', { method: 'POST' });
      // Mark workspace as onboarded
      await api(`/api/workspaces/${_workspaceId}`, {
        method: 'PATCH',
        body: JSON.stringify({ onboarded_at: new Date().toISOString() }),
      });
      startStatusPolling();
    } catch { /* ignore — scrape errors don't block completion */ }
    _obClose();
  }

  function _obBack() { if (_obStep > 0) { _obStep--; _obRender(); } }

  // ── Settings (Task 10) ────────────────────────────────
  function showSettingsTab(tab) {
    ['account','workspace','team','monitoring'].forEach(t => {
      document.getElementById('st-' + t)?.classList.toggle('hidden', t !== tab);
      document.querySelector(`[data-st="${t}"]`)?.classList.toggle('active', t === tab);
    });
  }

  async function loadSettings() {
    try {
      const data = await api('/api/settings');
      const ws   = data.workspace || {};
      const pref = data.preferences || {};
      const role = data.role || 'viewer';
      const isAdmin = ['owner','admin'].includes(role);

      // Account tab
      document.getElementById('st-full-name').value = _me?.user?.full_name || '';
      document.getElementById('st-email').value     = _me?.user?.email || '';

      // Workspace tab
      document.getElementById('st-ws-name').value      = ws.name || '';
      document.getElementById('st-company-name').value = ws.company_name || '';
      document.getElementById('st-company-url').value  = ws.company_url || '';
      const wsForm  = document.getElementById('st-ws-form');
      const wsNote  = document.getElementById('st-ws-readonly-note');
      if (!isAdmin && wsForm) {
        wsForm?.querySelectorAll('input').forEach(el => el.readOnly = true);
        wsNote?.classList.remove('hidden');
      }

      // Monitoring tab
      const seEl = document.getElementById('st-scrape-enabled');
      if (seEl) seEl.checked = ws.scrape_enabled !== false;
      const sfEl = document.getElementById('st-scrape-freq');
      if (sfEl) sfEl.value = ws.scrape_frequency || 'daily';
      const mpEl = document.getElementById('st-max-pages');
      if (mpEl) mpEl.value = ws.crawl_max_pages || 50;
      const slEl = document.getElementById('st-slack-url');
      if (slEl) slEl.value = ws.slack_webhook_url || '';

      // Team tab
      await _loadTeam(isAdmin);
    } catch(e) {
      console.error('Settings load error', e);
    }
  }

  async function _loadTeam(isAdmin) {
    const list = document.getElementById('st-members-list');
    const form = document.getElementById('st-invite-form');
    try {
      const members = await api(`/api/workspaces/${_workspaceId}/members`);
      list.innerHTML = (members || []).map(m => {
        const profile = m.profiles || {};
        const name = profile.full_name || profile.email || m.user_id;
        return `<div class="flex items-center justify-between gap-3 py-2 border-b border-slate-100 last:border-0">
          <div>
            <p class="text-sm font-medium text-slate-800">${_esc(name)}</p>
            <p class="text-xs text-slate-400">${_esc(profile.email || '')}</p>
          </div>
          <div class="flex items-center gap-2">
            <span class="text-xs text-slate-400 capitalize">${_esc(m.role)}</span>
          </div>
        </div>`;
      }).join('');
      if (isAdmin) form?.classList.remove('hidden');
    } catch(e) {
      list.innerHTML = `<p class="text-red-400 text-sm">${_esc(e.message)}</p>`;
    }
  }

  async function saveAccountField(field, value) {
    const msg = document.getElementById('st-account-msg');
    try {
      await api('/api/me', { method: 'PATCH', body: JSON.stringify({ [field]: value }) });
      if (_me) _me.user = { ..._me.user, [field]: value };
      _renderUserMenu();
      msg.textContent = 'Saved'; msg.className = 'text-xs text-emerald-600'; msg.classList.remove('hidden');
      setTimeout(() => msg.classList.add('hidden'), 2000);
    } catch(e) {
      msg.textContent = e.message; msg.className = 'text-xs text-red-500'; msg.classList.remove('hidden');
    }
  }

  async function saveWsSetting(field, value) {
    const msgIds = { name: 'st-ws-msg', company_name: 'st-ws-msg', company_url: 'st-ws-msg',
                     scrape_enabled: 'st-monitoring-msg', scrape_frequency: 'st-monitoring-msg',
                     crawl_max_pages: 'st-monitoring-msg', slack_webhook_url: 'st-slack-msg' };
    const msgId = msgIds[field] || 'st-monitoring-msg';
    const msg = document.getElementById(msgId);
    try {
      await api('/api/settings/workspace', { method: 'PATCH', body: JSON.stringify({ [field]: value }) });
      msg.textContent = 'Saved'; msg.className = 'text-xs text-emerald-600'; msg.classList.remove('hidden');
      setTimeout(() => msg.classList.add('hidden'), 2000);
    } catch(e) {
      msg.textContent = e.message; msg.className = 'text-xs text-red-500'; msg.classList.remove('hidden');
    }
  }

  async function sendInvite() {
    const email = document.getElementById('st-invite-email').value.trim();
    const role  = document.getElementById('st-invite-role').value;
    const msg   = document.getElementById('st-invite-msg');
    if (!email) return;
    try {
      await api(`/api/workspaces/${_workspaceId}/invites`, {
        method: 'POST', body: JSON.stringify({ email, role }),
      });
      document.getElementById('st-invite-email').value = '';
      msg.textContent = `Invite sent to ${email}`; msg.className = 'text-xs text-emerald-600'; msg.classList.remove('hidden');
      setTimeout(() => msg.classList.add('hidden'), 3000);
    } catch(e) {
      msg.textContent = e.message; msg.className = 'text-xs text-red-500'; msg.classList.remove('hidden');
    }
  }

  async function testSlack() {
    const msg = document.getElementById('st-slack-msg');
    try {
      await api('/api/settings/slack/test', { method: 'POST' });
      msg.textContent = '✓ Slack message sent!'; msg.className = 'text-xs text-emerald-600'; msg.classList.remove('hidden');
    } catch(e) {
      msg.textContent = e.message; msg.className = 'text-xs text-red-500'; msg.classList.remove('hidden');
    }
  }

  initAuth();
  