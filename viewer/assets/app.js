/* LameStreet viewer.
 *
 * Reads one JSON file and renders it. When the collector is running it talks to
 * /api/dashboard and the buttons work; when the page is hosted statically it
 * falls back to a dashboard.json sitting next to it and hides the buttons. Same
 * page either way.
 */

const money = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const compact = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
const qtyFmt = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 4 });

let DATA = null;
let SETUP = null;
let LIVE = false;
let active = decodeURIComponent(location.hash.slice(1)) || 'overview';
let busy = false;

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* Switch tab and keep the URL hash in sync, so views are linkable. */
function go(id, { scroll = false } = {}) {
  active = id;
  history.replaceState(null, '', `#${encodeURIComponent(id)}`);
  render();
  if (scroll) window.scrollTo(0, 0);
}

const rupees = (v, dp = true) =>
  v === null || v === undefined ? '<span class="muted">—</span>'
    : `${v < 0 ? '−' : ''}₹${(dp ? money : compact).format(Math.abs(v))}`;

const signed = (v) => {
  if (v === null || v === undefined) return '<span class="muted">—</span>';
  const cls = v > 0 ? 'gain' : v < 0 ? 'loss' : 'muted';
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  return `<span class="${cls}">${sign}₹${money.format(Math.abs(v))}</span>`;
};

const pct = (v) => {
  if (v === null || v === undefined) return '';
  const cls = v > 0 ? 'gain' : v < 0 ? 'loss' : 'muted';
  return `<span class="${cls}">${v > 0 ? '+' : ''}${v.toFixed(2)}%</span>`;
};

/* Pill-style percentage for the stat cards. */
const pctBadge = (v) => {
  if (v === null || v === undefined) return '';
  const dir = v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
  const icon = v > 0 ? 'bi-arrow-up-right' : v < 0 ? 'bi-arrow-down-right' : 'bi-dash';
  return `<span class="delta ${dir}"><i class="bi ${icon}"></i>${v > 0 ? '+' : ''}${v.toFixed(2)}%</span>`;
};

function ago(iso) {
  if (!iso) return 'never';
  const then = new Date(iso);
  if (isNaN(then)) return 'never';
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hr ago`;
  const days = Math.round(hrs / 24);
  return days === 1 ? 'yesterday' : `${days} days ago`;
}

/* ------------------------------------------------------------------ loading */

async function load() {
  try {
    const res = await fetch('/api/dashboard', { cache: 'no-store' });
    if (res.status === 401) { location.replace('/login'); return; }
    if (!res.ok) throw new Error('api unavailable');
    DATA = await res.json();
    LIVE = true;
  } catch {
    try {
      const res = await fetch('dashboard.json', { cache: 'no-store' });
      DATA = await res.json();
      LIVE = false;
    } catch {
      DATA = { empty: true, message: 'Could not load dashboard data.' };
      LIVE = false;
    }
  }
  if (LIVE) {
    try { SETUP = await (await fetch('/api/setup', { cache: 'no-store' })).json(); } catch { SETUP = null; }
  } else {
    $('.actions').style.display = 'none';
  }
  $('#btn-logout').hidden = !SETUP?.auth_enabled;
  counted = false;
  render();
}

/* Calls that change something. Returns the parsed body, or throws with the
 * server's own message so the UI can show why it refused. */
async function send(url, { method = 'POST', body = null, form = null } = {}) {
  const init = { method };
  if (form) {
    init.body = form;
  } else if (body) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  // Session expired mid-use: back to the login page rather than a dead error.
  if (res.status === 401) { location.replace('/login'); throw new Error('login required'); }
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = payload.detail;
    throw new Error(
      typeof detail === 'string' ? detail
        : Array.isArray(detail) ? detail.map((d) => d.msg || '').join('; ')
          : `request failed (${res.status})`);
  }
  if (payload.setup) SETUP = payload.setup;
  return payload;
}

/* ----------------------------------------------------------------- renderers */

function render() {
  const blank = !DATA || DATA.empty || !(DATA.members || []).length;
  if (blank && active !== 'setup') return renderEmpty();
  renderStamps();
  $('#main').innerHTML = (active === 'setup' ? '' : renderStats())
    + renderTabs() + renderPanel() + (active === 'setup' ? '' : renderAttention());
  wire();
  countUp();
}

/* Count the stat values up from zero — once per data load, not per tab switch. */
let counted = false;
function countUp() {
  if (counted || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  counted = true;
  document.querySelectorAll('.stat-value').forEach((el) => {
    const node = el.querySelector('.gain, .loss, .muted') || el;
    const m = node.textContent.match(/\d[\d,]*(?:\.\d+)?/);
    if (!m) return;
    const target = parseFloat(m[0].replace(/,/g, ''));
    if (!isFinite(target) || target === 0) return;
    const prefix = node.textContent.slice(0, m.index);
    const suffix = node.textContent.slice(m.index + m[0].length);
    const decimals = (m[0].split('.')[1] || '').length;
    const fmt = new Intl.NumberFormat('en-IN',
      { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    const t0 = performance.now();
    const step = (t) => {
      const p = Math.min(1, (t - t0) / 700);
      const eased = 1 - (1 - p) ** 3;
      node.textContent = prefix + fmt.format(target * eased) + suffix;
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

function renderEmpty() {
  $('#stamps').innerHTML = '';
  $('#main').innerHTML = renderTabs() + `
    <div class="empty">
      <p>${esc(DATA?.message || 'Nobody has been added yet.')}</p>
      ${LIVE
        ? '<p><button class="btn btn-primary" id="go-setup"><i class="bi bi-person-plus"></i> Add User</button></p>'
        : '<p class="muted">This is a static snapshot — add people on the machine running the collector.</p>'}
    </div>`;
  const goBtn = $('#go-setup');
  if (goBtn) goBtn.addEventListener('click', () => go('setup'));
  document.querySelectorAll('.tab').forEach((el) =>
    el.addEventListener('click', () => go(el.dataset.tab)));
}

function renderStamps() {
  const a = DATA.as_of || {};
  const holdings = (DATA.members || [])
    .map((m) => m.holdings_as_of).filter(Boolean).sort().pop();
  $('#stamps').innerHTML = [
    `<span><b>Prices</b> ${ago(a.prices)}${a.prices_stale ? ' <span class="chip plain">stale</span>' : ''}</span>`,
    `<span><b>Holdings</b> ${ago(holdings)}</span>`,
    `<span><b>Mail synced</b> ${ago(a.last_mail_sync)}</span>`,
  ].join('');
}

function renderStats() {
  const t = DATA.totals || {};
  const cards = [
    { icon: 'bi-wallet2', tint: 't1', label: 'Invested', value: rupees(t.invested, false),
      sub: `<span class="stat-sub">${t.positions || 0} positions · ${t.members || 0} people</span>` },
    { icon: 'bi-cash-stack', tint: 't2', label: 'Current value', value: rupees(t.current, false),
      sub: '' },
    { icon: 'bi-graph-up-arrow', tint: 't3', label: 'Total P&L', value: signed(t.pnl),
      sub: pctBadge(t.pnl_pct) },
    { icon: 'bi-lightning-charge', tint: 't4', label: 'Today', value: signed(t.day_change),
      sub: pctBadge(t.day_change_pct) },
  ];
  return `<section class="stats">${cards.map((c) => `
    <div class="card stat">
      <div class="card-body">
        <span class="stat-icon ${c.tint}"><i class="bi ${c.icon}"></i></span>
        <div class="stat-body">
          <span class="stat-label">${c.label}</span>
          <span class="stat-value">${c.value}</span>
          ${c.sub}
        </div>
      </div>
    </div>`).join('')}</section>`;
}

function renderTabs() {
  const tabs = [['overview', '<i class="bi bi-grid-1x2"></i>All Family', '']]
    .concat((DATA?.members || []).map((m) =>
      [m.id, esc(m.name), m.positions ? String(m.positions) : '—']))
    .concat([['activity', '<i class="bi bi-activity"></i>Activity', '']]);
  return `<nav class="tabs card" role="tablist"><div class="nav nav-pills">${tabs.map(([id, label, sub]) => `
    <button class="tab nav-link${active === id ? ' active' : ''}" role="tab"
      data-tab="${esc(id)}" aria-selected="${active === id}">
      ${label}${sub ? `<span class="tab-sub">${sub}</span>` : ''}
    </button>`).join('')}</div></nav>`;
}

function renderPanel() {
  if (active === 'setup') return renderSetup();
  if (active === 'overview') return renderOverview();
  if (active === 'activity') return renderActivity();
  const member = (DATA.members || []).find((m) => m.id === active);
  return member ? renderMember(member) : '<div class="empty">Nothing here.</div>';
}

function renderOverview() {
  const members = DATA.members || [];
  const totalCurrent = (DATA.totals || {}).current || 0;
  const rows = members.map((m) => {
    const share = totalCurrent > 0 && m.current ? (m.current / totalCurrent) * 100 : 0;
    return `
    <tr class="clickable" data-goto="${esc(m.id)}">
      <td class="left"><span class="sym">${esc(m.name)}</span>
        <span class="sub">${m.positions} positions · holdings ${ago(m.holdings_as_of)}</span></td>
      <td>${rupees(m.invested, false)}</td>
      <td>${rupees(m.current, false)}</td>
      <td><span class="share">
        <span class="progress"><span class="progress-bar" style="width:${share.toFixed(1)}%"></span></span>
        <span class="share-num">${share.toFixed(1)}%</span></span></td>
      <td>${signed(m.pnl)} <span class="sub">${pct(m.pnl_pct)}</span></td>
      <td>${signed(m.day_change)}</td>
    </tr>`;
  }).join('');

  return `
  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-people"></i>By person</h2>
      <span class="hint">Click a row to open that portfolio</span></div>
    <div class="table-responsive"><table class="table table-hover align-middle">
      <thead><tr><th class="left">Member</th><th>Invested</th><th>Value</th><th>Share</th><th>P&amp;L</th><th>Today</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="muted">No members yet.</td></tr>'}</tbody>
    </table></div>
  </section>
  ${renderConsolidated()}`;
}

function renderConsolidated() {
  const rows = (DATA.consolidated || []).map((r, i) => {
    const holders = r.holders.map((h) => `
      <tr><td class="left">${esc(h.member_name)}</td>
        <td>${qtyFmt.format(h.qty)}</td><td>${rupees(h.avg)}</td>
        <td>${rupees(h.value, false)}</td><td>${signed(h.pnl)}</td></tr>`).join('');

    const missing = r.not_held_by.length
      ? `<div class="not-held">Not held by <b>${r.not_held_by.map((m) => esc(m.name)).join(', ')}</b></div>`
      : `<div class="not-held">Held by everyone.</div>`;

    return `
    <tr class="clickable" data-row="${i}">
      <td class="left"><span class="caret" data-caret="${i}">▸</span>
        <span class="sym">${esc(r.symbol)}</span>
        <span class="chip plain">${r.held_by_count} of ${(DATA.members || []).length}</span>
        <span class="sub">${esc(r.name || '')}</span></td>
      <td>${qtyFmt.format(r.qty)}</td>
      <td>${rupees(r.avg)}</td>
      <td>${r.priced ? rupees(r.price) : '<span class="muted">no price</span>'}</td>
      <td>${rupees(r.value, false)}</td>
      <td>${signed(r.pnl)} <span class="sub">${pct(r.pnl_pct)}</span></td>
      <td>${signed(r.day_change)}</td>
    </tr>
    <tr class="detail" data-detail="${i}" hidden><td colspan="7"><div class="detail-inner">
      <table class="detail-grid table">
        <thead><tr><th class="left">Holder</th><th>Qty</th><th>Avg cost</th><th>Value</th><th>P&amp;L</th></tr></thead>
        <tbody>${holders}</tbody>
      </table>${missing}
    </div></td></tr>`;
  }).join('');

  return `
  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-collection"></i>Every stock the family holds</h2>
      <span class="hint">Expand a row to see who holds it — and who doesn't</span></div>
    <div class="table-responsive"><table class="table table-hover align-middle">
      <thead><tr><th class="left">Stock</th><th>Total qty</th><th>Avg cost</th><th>Price</th>
        <th>Value</th><th>P&amp;L</th><th>Today</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7" class="muted">No holdings yet.</td></tr>'}</tbody>
    </table></div>
  </section>`;
}

function renderMember(m) {
  const rows = m.holdings.map((h) => `
    <tr>
      <td class="left"><span class="sym">${esc(h.symbol)}</span>
        ${h.cost_known ? '' : '<span class="chip plain">cost unknown</span>'}
        <span class="sub">${esc(h.name || '')}</span></td>
      <td>${qtyFmt.format(h.qty)}</td>
      <td>${rupees(h.avg)}</td>
      <td>${h.priced ? rupees(h.price) : '<span class="muted">no price</span>'}</td>
      <td>${rupees(h.cost, false)}</td>
      <td>${rupees(h.value, false)}</td>
      <td>${signed(h.pnl)} <span class="sub">${pct(h.pnl_pct)}</span></td>
      <td>${signed(h.day_change)}</td>
    </tr>`).join('');

  return `
  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-person-circle"></i>${esc(m.name)}</h2>
      <span class="hint">Holdings ${ago(m.holdings_as_of)}${m.snapshot_source ? ` · from ${esc(m.snapshot_source.replace(/_/g, ' '))}` : ''}
        · last trade ${ago(m.last_trade)}</span></div>
    <div class="table-responsive"><table class="table table-hover align-middle">
      <thead><tr><th class="left">Stock</th><th>Qty</th><th>Avg cost</th><th>Price</th>
        <th>Invested</th><th>Value</th><th>P&amp;L</th><th>Today</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8" class="muted">No holdings recorded yet.</td></tr>'}</tbody>
    </table></div>
  </section>`;
}

function renderActivity() {
  const names = Object.fromEntries((DATA.members || []).map((m) => [m.id, m.name]));
  const rows = (DATA.activity || []).map((a) => `
    <div class="feed-row">
      <span class="when">${esc((a.ts || '').replace('T', ' ').slice(0, 16))}</span>
      <span class="who">${esc(names[a.member] || a.member)}</span>
      <span>${esc(a.text)}</span>
      <span class="src">${esc((a.source || '').replace(/_/g, ' '))}</span>
    </div>`).join('');
  return `
  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-clock-history"></i>Activity</h2>
      <span class="hint">Every event in the log, newest first</span></div>
    <div class="feed">${rows || '<div class="feed-row"><span class="muted">Nothing logged yet.</span></div>'}</div>
  </section>`;
}

/* ------------------------------------------------------------------- setup */

function renderSetup() {
  if (!SETUP) return '<div class="empty">Setup is unavailable.</div>';
  const positions = Object.fromEntries((DATA?.members || []).map((m) => [m.id, m.positions]));

  const memberRows = SETUP.members.map((m) => `
    <tr>
      <td class="left"><span class="sym">${esc(m.name)}</span>
        <span class="sub">${m.mail_user ? esc(m.mail_user) : 'no inbox connected'}</span></td>
      <td class="left">${m.has_password
        ? `<span class="ok-dot"><i class="bi bi-check-lg"></i> set</span>${m.password_count > 1
            ? `<span class="chip plain">${m.password_count}</span>` : ''}`
        : '<span class="flag">no password</span>'}</td>
      <td class="left">${m.mail_ready
        ? '<span class="ok-dot"><i class="bi bi-check-lg"></i> connected</span>'
        : m.mail_user ? '<span class="flag">no app password</span>'
          : '<span class="muted">—</span>'}</td>
      <td class="left">${esc((m.broker_labels || []).join(', ') || '—')}
  </td>
      <td>${positions[m.id] ?? 0}</td>
      <td><button class="btn btn-sm btn-outline-danger" data-remove="${esc(m.id)}">Remove</button></td>
    </tr>`).join('');

  const unmapped = SETUP.instruments.unmapped.map((i) => `
    <tr>
      <td class="left"><code class="mono">${esc(i.key)}</code>
        <span class="sub">${esc(i.name || i.symbol || '')}</span></td>
      <td class="left">
        <span class="input-group input-group-sm map-group">
          <input class="form-control" data-map-key="${esc(i.key)}" placeholder="NSE symbol">
          <button class="btn btn-primary" data-map="${esc(i.key)}" type="button">Map</button>
        </span>
      </td>
    </tr>`).join('');

  const memberOptions = SETUP.members
    .map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');

  return `
  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-person-plus"></i>Add a person</h2>
      <span class="hint">One password opens their statements; their inbox is where those statements arrive</span></div>
    <div class="card-body">
    <p class="help">
      The <b>statement password</b> is whatever opens this person's broker PDFs — usually their
      PAN, though some brokers use a client code or date of birth. It also identifies whose
      statement is whose, so <b>it must be different for each person</b>.
      The <b>app password</b> is a separate 16-character code from
      <em>myaccount.google.com/apppasswords</em> for reading their mail — a Google account
      password will not work there.
    </p>
    <form class="form-grid" id="form-member" autocomplete="off">
      <label>Full name<input class="form-control" name="name" placeholder="Ravi" required></label>
      <label>Statement password<input class="form-control" name="doc_password" type="password"
        placeholder="usually their PAN"></label>
      <label>Their Gmail<input class="form-control" name="mail_user" type="email"
        placeholder="ravi@gmail.com"></label>
      <label>App password<input class="form-control" name="mail_password" type="password"
        placeholder="16 characters"></label>
      <label>Broker<select class="form-select" name="broker">${(SETUP.broker_choices || []).map((b) => `
        <option value="${esc(b.id)}" ${b.id === 'groww' ? 'selected' : ''}>${esc(b.label)}${b.verified ? '' : ' (unverified)'}</option>`).join('')}
      </select><span class="opt">more brokers can be added per person later, under
        “Brokers &amp; statement passwords”</span></label>
      <div class="form-actions"><button class="btn btn-primary" type="submit">
        <i class="bi bi-person-plus"></i>Add person</button></div>
    </form>
    <p class="msg" id="msg-member"></p>
    </div>
  </section>

  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-bank"></i>Integrate a new broker</h2>
      <span class="hint">Rarely needed — only when someone uses a broker missing from the list above</span></div>
    <div class="card-body">
    <p class="help">
      Type the broker's name and scan the connected inboxes. The scan finds every address
      that broker mails from, and Claude picks out the ones carrying real documents —
      contract notes and holding statements — from the marketing noise. Nothing is saved
      until you review the proposal and click <b>Add broker</b>. New brokers start
      <em>unverified</em> until their first document parses.
    </p>
    <form class="form-row" id="form-broker-scan" autocomplete="off">
      <input class="form-control" name="name" placeholder="Angel One" required>
      <button class="btn btn-primary" type="submit"><i class="bi bi-search"></i>Scan inboxes</button>
      <button class="btn btn-ghost" type="button" id="btn-broker-manual">Enter manually</button>
    </form>
    <div id="broker-proposal"></div>
    <p class="msg" id="msg-broker"></p>
    ${renderLlmAssist(SETUP.llm || {})}
    ${(SETUP.broker_choices || []).some((b) => b.custom) ? `
    <p class="hint">Added by you: ${SETUP.broker_choices.filter((b) => b.custom).map((b) => `
      <span class="chip plain">${esc(b.label)}
        <button class="chip-x" data-broker-remove="${esc(b.id)}" title="Remove this broker">×</button></span>`).join(' ')}</p>` : ''}
    </div>
  </section>

  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-people"></i>People</h2>
      <span class="hint">${SETUP.members.length} added ·
        ${SETUP.members.filter((m) => m.mail_ready).length} inbox(es) connected</span></div>
    <div class="table-responsive"><table class="table table-hover align-middle">
      <thead><tr><th class="left">Name</th><th class="left">Statement pw</th>
        <th class="left">Inbox</th><th class="left">Brokers</th>
        <th>Positions</th><th></th></tr></thead>
      <tbody>${memberRows || '<tr><td colspan="6" class="muted">Nobody added yet.</td></tr>'}</tbody>
    </table></div>
    <div class="card-body border-top">
    <p class="m-0"><button class="btn btn-ghost" id="btn-test" ${SETUP.members.length ? '' : 'disabled'}>
      <i class="bi bi-envelope-check"></i>Test all inboxes</button></p>
    <p class="msg" id="msg-mailbox"></p>
    </div>
  </section>

  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-envelope-at"></i>Update someone's inbox</h2>
      <span class="hint">To fix a wrong address or replace an app password</span></div>
    <div class="card-body">
    <form class="form-row" id="form-member-mail" autocomplete="off">
      <select class="form-select" name="member" required>
        <option value="">Whose inbox…</option>${memberOptions}
      </select>
      <input class="form-control" name="mail_user" type="email" placeholder="their@gmail.com">
      <input class="form-control" name="mail_password" type="password" placeholder="app password">
      <button class="btn btn-primary" type="submit">Save</button>
    </form>
    <p class="msg" id="msg-member-mail"></p>
    </div>
  </section>

  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-shield-lock"></i>Brokers &amp; statement passwords</h2>
      <span class="hint">Each broker mails different documents from a different address</span></div>
    <div class="card-body">
    <p class="help">
      A person's brokers decide which sending addresses get read — and <b>only</b> those
      addresses are ever read, so nothing unrelated is downloaded. Brokers marked
      <em>unverified</em> have a profile but no document has been parsed yet.
      Add an extra password here if one broker locks its PDFs differently from another.
    </p>
    <form class="form-row" id="form-docs" autocomplete="off">
      <select class="form-select" name="member" required>
        <option value="">Who…</option>${memberOptions}
      </select>
      <span class="checks">${(SETUP.broker_choices || []).map((b) => `
        <label class="check"><input type="checkbox" class="form-check-input" name="brokers" value="${esc(b.id)}">
          ${esc(b.label)}${b.verified ? '' : '<span class="opt"> unverified</span>'}</label>`).join('')}</span>
      <input class="form-control" name="doc_password" type="password" placeholder="extra password (optional)">
      <button class="btn btn-primary" type="submit">Save</button>
    </form>
    <p class="msg" id="msg-docs"></p>
    </div>
  </section>

  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-funnel"></i>Which emails to read</h2>
      <span class="hint">Matched by the mail server, before anything is downloaded</span></div>
    <div class="card-body">
    <p class="help">
      Only mail from these addresses is read — that is what keeps a sync fast and stops
      anything unrelated coming in. A personal Gmail can hold twenty thousand messages a year;
      this narrows it to the broker's few dozen. The brokers ticked above already contribute
      their addresses; add extras here only for something they don't cover — for example the
      address a statement gets <em>forwarded</em> from, since a forward arrives from the
      forwarder, not the broker. Subjects are opt-in and match across all senders, so leave
      them empty unless you need them.
    </p>
    <form class="form-grid" id="form-sources">
      <label>Sender addresses<textarea class="form-control" name="senders" rows="4"
        placeholder="noreply@groww.in">${esc((SETUP.sources?.senders || []).join('\n'))}</textarea></label>
      <label>Subject contains<textarea class="form-control" name="subjects" rows="4"
        placeholder="contract note">${esc((SETUP.sources?.subjects || []).join('\n'))}</textarea></label>
      <label>In effect right now <span class="opt">from the brokers above, plus your extras</span>
        <span class="readout">${esc([...(SETUP.effective_sources?.senders || []),
          ...(SETUP.effective_sources?.subjects || []).map((x) => `subject: ${x}`)].join('\n'))}</span></label>
      <div class="form-actions">
        <button class="btn btn-primary" type="submit">Save</button>
        <button class="btn btn-ghost" type="button" id="btn-preview">Count matches</button>
      </div>
    </form>
    <p class="msg" id="msg-sources"></p>
    </div>
  </section>

  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-file-earmark-arrow-up"></i>Sync holdings from CSV</h2>
      <span class="hint">The only source of cost basis</span></div>
    <div class="card-body">
    <p class="help">
      Depository statements list what you own but never what you paid, so for anything bought
      before the mailbox history begins, no document states its cost. A broker's own export
      does. Uploading one sets that person's quantities <i>and</i> average prices to exactly
      what it says — run it whenever P&amp;L looks wrong, not just the first time.
      In Groww: <b>Reports → Holdings → download</b>. Zerodha: <b>Console → Portfolio →
      Holdings → download</b>. Dhan: <b>Portfolio → Holdings → export</b>.
    </p>
    <p class="help muted">
      Rows are matched to existing positions by ISIN, looked up from the company name via
      NSE's list and from what the person already holds, so a re-upload updates the same
      positions rather than duplicating them. Anything it can't match is reported.
    </p>
    <form class="form-row" id="form-holdings">
      <select class="form-select" name="member" required>
        <option value="">Who is this for…</option>${memberOptions}
      </select>
      <input class="form-control" name="file" type="file" accept=".csv,text/csv" required>
      <button class="btn btn-primary" type="submit"><i class="bi bi-upload"></i>Upload &amp; sync</button>
    </form>
    <p class="msg" id="msg-holdings"></p>
    </div>
  </section>

  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-arrow-repeat"></i>Sync from a statement</h2>
      <span class="hint">When a month went missing</span></div>
    <div class="card-body">
    <p class="help">
      Upload the latest holdings or demat statement PDF. Quantities are set to exactly what
      it says on its own date, then the mail cursor rewinds to that date and every contract
      note since is read and applied on top. Use this when a statement never arrived by
      mail, or when the numbers have drifted and you want them reset against the depository.
      Encrypted statements are fine — the password identifies the owner.
    </p>
    <form class="form-row" id="form-statement">
      <select class="form-select" name="member">
        <option value="">Let the password decide…</option>${memberOptions}
      </select>
      <input class="form-control" name="file" type="file" accept=".pdf,application/pdf" required>
      <button class="btn btn-primary" type="submit"><i class="bi bi-upload"></i>Upload &amp; sync</button>
    </form>
    <label class="check"><input type="checkbox" class="form-check-input" id="statement-nosync">
      Record the statement only — don't read mail afterwards</label>
    <p class="msg" id="msg-statement"></p>
    </div>
  </section>

  <section class="panel card">
    <div class="panel-head"><h2><i class="bi bi-tags"></i>Stock symbols</h2>
      <span class="hint">${SETUP.instruments.known} known ·
        ${SETUP.instruments.unmapped.length} need mapping</span></div>
    <div class="card-body">
    <p class="help">
      Statements identify stocks by ISIN, which is matched against NSE's official equity list to
      get a symbol and a price. Anything NSE doesn't list — usually a recent rename or demerger —
      needs mapping by hand, or it shows no price.
    </p>
    <p><button class="btn btn-ghost" id="btn-instruments"><i class="bi bi-arrow-repeat"></i>Refresh NSE list</button></p>
    ${SETUP.instruments.unmapped.length ? `<div class="table-responsive"><table class="table align-middle">
      <thead><tr><th class="left">ISIN</th><th class="left">Map to</th></tr></thead>
      <tbody>${unmapped}</tbody></table></div>` : ''}
    <p class="msg" id="msg-instruments"></p>
    </div>
  </section>`;
}

function say(id, text, kind = 'ok') {
  const el = $(`#${id}`);
  if (!el) return;
  el.className = `msg ${kind}`;
  el.innerHTML = text;
}

/* Wraps a handler so the button disables, errors land in the right place, and
 * the panel re-renders from whatever the server actually saved. */
function action(msgId, fn, { rerender = true } = {}) {
  return async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    const button = event.submitter || event.currentTarget;
    if (button && button.tagName === 'BUTTON') button.disabled = true;
    say(msgId, 'Working…', 'pending');
    try {
      const message = await fn(event);
      if (rerender) { await load(); }
      say(msgId, message || 'Done.', 'ok');
    } catch (err) {
      say(msgId, esc(err.message), 'err');
    } finally {
      busy = false;
      if (button && button.tagName === 'BUTTON') button.disabled = false;
    }
  };
}

/* The credential box for Claude assist, shown inside "Integrate a new broker".
 * Claude only sorts document senders from marketing noise during onboarding —
 * it never reads statements — so this is the one place a key matters. The key
 * itself never comes back from the server; only whether one is stored. */
function renderLlmAssist(llm) {
  const model = `<code class="mono">${esc(llm.model || 'claude-opus-5')}</code>`;
  const status = !llm.sdk
    ? 'Claude assist is <b>off</b> — run <code class="mono">pip install anthropic</code> on the server to enable it.'
    : llm.key_saved
      ? `Claude assist is <b>on</b>, using the key saved here · model ${model}.`
      : llm.env
        ? `Claude assist is <b>on</b>, using ANTHROPIC_API_KEY from the server's environment · model ${model}.`
        : 'No API key yet — a scan still lists every sender, you just tick the right ones yourself. '
          + 'Paste a key from <em>console.anthropic.com/settings/keys</em> to have Claude pre-select them.';

  return `
    <div class="llm-box">
    <p class="help"><b><i class="bi bi-stars"></i> Claude assist.</b> ${status}</p>
    <form class="form-row" id="form-llm" autocomplete="off">
      <input class="form-control" name="api_key" type="password"
        placeholder="${llm.key_saved ? 'key saved — paste a new one to replace it' : 'sk-ant-… API key'}">
      <input class="form-control" name="model"
        placeholder="model — blank for ${esc(llm.default_model || 'claude-opus-5')}"
        value="${esc(llm.model && llm.model !== llm.default_model ? llm.model : '')}">
      <button class="btn btn-primary" type="submit">Save</button>
      <button class="btn btn-ghost" type="button" id="btn-llm-test">Test</button>
      ${llm.key_saved ? '<button class="btn btn-ghost" type="button" id="btn-llm-clear">Forget key</button>' : ''}
    </form>
    <p class="msg" id="msg-llm"></p>
    </div>`;
}

/* Fill the review form under "Integrate a new broker" with scan results.
 * Claude's proposal (when available) pre-selects the document senders; the
 * person can edit everything before saving. */
function showBrokerProposal(name, res) {
  const box = $('#broker-proposal');
  if (!box) return;
  const candidates = res.candidates || [];
  const proposed = res.proposal || null;
  const senders = (proposed?.senders?.length
    ? proposed.senders
    : candidates.filter((c) => c.pdfs > 0).map((c) => c.sender));

  const rows = candidates.map((c) => `
    <tr>
      <td class="left"><code class="mono">${esc(c.sender)}</code></td>
      <td>${c.count}</td><td>${c.pdfs}</td>
      <td class="left"><span class="sub">${esc((c.subjects || []).join(' · '))}</span></td>
    </tr>`).join('');

  box.innerHTML = `
    ${candidates.length ? `<div class="table-responsive"><table class="table align-middle">
      <thead><tr><th class="left">Sender</th><th>Messages</th><th>With PDF</th>
        <th class="left">Subjects seen</th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : ''}
    ${proposed?.rationale ? `<p class="help">${esc(proposed.rationale)}</p>` : ''}
    <form class="form-grid" id="form-broker-save" autocomplete="off">
      <label>Broker name<input class="form-control" name="name"
        value="${esc(proposed?.label || name)}" required></label>
      <label>Document senders <span class="opt">one per line — only these are ever read</span>
        <textarea class="form-control" name="senders" rows="3"
          placeholder="statements@broker.com">${esc(senders.join('\n'))}</textarea></label>
      <label>Subject hints <span class="opt">optional, for classifying documents</span>
        <textarea class="form-control" name="subjects" rows="3"
          placeholder="contract note">${esc((proposed?.subjects || []).join('\n'))}</textarea></label>
      <div class="form-actions"><button class="btn btn-primary" type="submit">Add broker</button></div>
    </form>`;

  $('#form-broker-save').addEventListener('submit', action('msg-broker', async () => {
    const f = new FormData($('#form-broker-save'));
    const lines = (v) => String(v || '').split('\n').map((s) => s.trim()).filter(Boolean);
    await send('/api/brokers', {
      body: {
        name: (f.get('name') || '').trim(),
        senders: lines(f.get('senders')),
        subjects: lines(f.get('subjects')),
      },
    });
    return 'Broker added — it now appears in every broker list, marked unverified '
      + 'until its first document parses.';
  }));
}

function wireSetup() {
  const memberForm = $('#form-member');
  if (memberForm) {
    memberForm.addEventListener('submit', action('msg-member', async () => {
      const f = new FormData(memberForm);
      const res = await send('/api/members', {
        body: {
          name: (f.get('name') || '').trim(),
          doc_password: f.get('doc_password') || '',
          brokers: [f.get('broker') || 'groww'],
          mail_user: (f.get('mail_user') || '').trim(),
          mail_password: f.get('mail_password') || '',
        },
      });
      memberForm.reset();
      const notes = res.warnings || [];
      return notes.length
        ? `Added — but ${notes.map(esc).join('; and ')}.`
        : 'Added, with PAN and inbox. Try “Test all inboxes”.';
    }));
  }

  const scanForm = $('#form-broker-scan');
  if (scanForm) {
    scanForm.addEventListener('submit', action('msg-broker', async () => {
      const name = (new FormData(scanForm).get('name') || '').trim();
      if (!name) throw new Error('Type the broker name first.');
      const res = await send('/api/brokers/scan', { body: { name } });
      showBrokerProposal(name, res);
      const errs = (res.errors || []).length
        ? `<br><span class="muted">${esc(res.errors.join('; '))}</span>` : '';
      return `${(res.candidates || []).length} candidate sender(s) found · ${esc(res.llm || '')}.${errs}`;
    }, { rerender: false }));

    $('#btn-broker-manual').addEventListener('click', () => {
      const name = (new FormData(scanForm).get('name') || '').trim();
      showBrokerProposal(name, { candidates: [], proposal: null });
      say('msg-broker', 'Fill in the sender addresses this broker mails documents from.', 'ok');
    });
  }

  const llmForm = $('#form-llm');
  if (llmForm) {
    llmForm.addEventListener('submit', action('msg-llm', async () => {
      const f = new FormData(llmForm);
      const key = (f.get('api_key') || '').trim();
      const model = (f.get('model') || '').trim();
      if (!key && !SETUP?.llm?.key_saved && !model) {
        throw new Error('Paste an API key first (or set a model to use with the environment key).');
      }
      await send('/api/llm', { body: { api_key: key, model } });
      return 'Saved. Press Test to confirm it works — the test is free, no tokens are spent.';
    }));

    $('#btn-llm-test').addEventListener('click', action('msg-llm', async () => {
      const res = await send('/api/llm/test');
      return `✓ ${esc(res.detail)}`;
    }, { rerender: false }));

    const clearLlm = $('#btn-llm-clear');
    if (clearLlm) {
      clearLlm.addEventListener('click', action('msg-llm', async () => {
        if (!confirm('Forget the saved API key and model?\n\nClaude assist falls back to '
          + "ANTHROPIC_API_KEY from the server's environment, if set.")) {
          throw new Error('Cancelled.');
        }
        await send('/api/llm', { method: 'DELETE' });
        return 'Forgotten.';
      }));
    }
  }

  document.querySelectorAll('[data-broker-remove]').forEach((el) =>
    el.addEventListener('click', action('msg-broker', async (e) => {
      const id = e.currentTarget.dataset.brokerRemove;
      if (!confirm(`Remove the broker “${id}”?`)) throw new Error('Cancelled.');
      await send(`/api/brokers/${encodeURIComponent(id)}`, { method: 'DELETE' });
      return 'Broker removed.';
    })));

  document.querySelectorAll('[data-remove]').forEach((el) =>
    el.addEventListener('click', action('msg-member', async (e) => {
      const id = e.currentTarget.dataset.remove;
      const who = SETUP.members.find((m) => m.id === id);
      if (!confirm(`Remove ${who ? who.name : id} from the dashboard?\n\n`
        + 'Their recorded trades stay in the log, so adding them back with the same '
        + 'name restores everything.')) {
        throw new Error('Cancelled.');
      }
      await send(`/api/members/${encodeURIComponent(id)}`, { method: 'DELETE' });
      return 'Removed.';
    })));

  const testBtn = $('#btn-test');
  if (testBtn) {
    testBtn.addEventListener('click', action('msg-mailbox', async () => {
      const res = await send('/api/mailbox/test');
      return res.results.map((r) =>
        `${r.ok ? '✓' : '✗'} <b>${esc(r.user)}</b> — ${esc(r.detail)}`).join('<br>');
    }, { rerender: false }));
  }

  const mailForm = $('#form-member-mail');
  if (mailForm) {
    mailForm.addEventListener('submit', action('msg-member-mail', async () => {
      const f = new FormData(mailForm);
      const member = f.get('member');
      if (!member) throw new Error('Pick whose inbox this is.');
      await send(`/api/members/${encodeURIComponent(member)}/mailbox`, {
        body: {
          mail_user: (f.get('mail_user') || '').trim(),
          mail_password: f.get('mail_password') || '',
        },
      });
      mailForm.reset();
      return 'Saved. Use “Test all inboxes” to check it.';
    }));
  }

  const holdingsForm = $('#form-holdings');
  if (holdingsForm) {
    holdingsForm.addEventListener('submit', action('msg-holdings', async () => {
      const f = new FormData(holdingsForm);
      const member = f.get('member');
      const file = f.get('file');
      if (!member) throw new Error('Pick who this file belongs to.');
      if (!file || !file.size) throw new Error('Choose a CSV file.');
      const upload = new FormData();
      upload.append('file', file);
      const res = await send(`/api/members/${encodeURIComponent(member)}/holdings`,
        { form: upload });
      holdingsForm.reset();
      const who = SETUP.members.find((m) => m.id === member);
      const cost = res.with_cost === res.positions
        ? 'all with purchase cost'
        : `${res.with_cost} of ${res.positions} with purchase cost`;
      const money = (n) => (n == null ? '—' : `₹${Math.round(n).toLocaleString('en-IN')}`);
      const bits = [`Set ${esc(who ? who.name : member)} to ${res.positions} positions (${cost}).`,
        `Invested now ${money(res.invested)}.`];
      if (res.cost_unknown) bits.push(`${res.cost_unknown} still without cost.`);
      if (res.unmatched) bits.push(`${res.unmatched} row(s) had no ISIN — check the notes.`);
      const notes = (res.notes || []).length
        ? `<br><span class="muted">${esc(res.notes.join('; '))}</span>` : '';
      return `${bits.join(' ')}${notes}`;
    }));
  }

  const statementForm = $('#form-statement');
  if (statementForm) {
    // rerender is off: the follow-up sync is what changes the numbers, and it is
    // still running when this returns. poll() reloads once it finishes.
    statementForm.addEventListener('submit', action('msg-statement', async () => {
      const f = new FormData(statementForm);
      const file = f.get('file');
      if (!file || !file.size) throw new Error('Choose a statement PDF.');
      const skip = $('#statement-nosync')?.checked;
      const upload = new FormData();
      upload.append('file', file);
      upload.append('member', f.get('member') || '');
      upload.append('sync_after', skip ? 'false' : 'true');
      const res = await send('/api/statement', { form: upload });
      statementForm.reset();

      const who = SETUP.members.find((m) => m.id === res.member);
      const state = res.new ? 'holdings set to' : 'already matched';
      let msg = `${esc(who ? who.name : res.member)}: ${state} ${res.positions}`
        + ` positions as of ${esc(res.as_of)}.`;
      if (res.sync_started) {
        const job = $('#job');
        job.hidden = false; job.className = 'job'; job.textContent = 'Reading mail…';
        poll(job);
        msg += ` Now reading mail from ${esc(res.as_of)} onwards`
          + `${res.rewound.length ? ` (${esc(res.rewound.join(', '))})` : ''} — `
          + 'the numbers update when it finishes.';
      } else if (!skip) {
        msg += ' No inbox connected, so nothing was read after it.';
      }
      return msg;
    }, { rerender: false }));
  }

  const instrumentsBtn = $('#btn-instruments');
  if (instrumentsBtn) {
    instrumentsBtn.addEventListener('click', action('msg-instruments', async () => {
      const res = await send('/api/instruments/refresh');
      return `Loaded ${res.loaded} NSE instruments.`;
    }));
  }

  document.querySelectorAll('[data-map]').forEach((el) =>
    el.addEventListener('click', action('msg-instruments', async (e) => {
      const key = e.currentTarget.dataset.map;
      const input = document.querySelector(`[data-map-key="${key}"]`);
      const symbol = (input?.value || '').trim().toUpperCase();
      if (!symbol) throw new Error('Type the NSE symbol first.');
      await send('/api/instruments/map', { body: { key, symbol } });
      return `${esc(key)} → ${esc(symbol)}`;
    })));

  const docsForm = $('#form-docs');
  if (docsForm) {
    docsForm.addEventListener('submit', action('msg-docs', async () => {
      const f = new FormData(docsForm);
      const member = f.get('member');
      if (!member) throw new Error('Pick who this is for.');
      const brokers = f.getAll('brokers');
      const password = f.get('doc_password') || '';
      if (!brokers.length && !password.trim()) {
        throw new Error('Tick at least one broker, or enter a statement password.');
      }
      await send(`/api/members/${encodeURIComponent(member)}/docs`, {
        body: { brokers, doc_password: password },
      });
      docsForm.reset();
      return brokers.length ? `Saved: ${brokers.join(', ')}.` : 'Statement password saved.';
    }));
  }

  const sourcesForm = $('#form-sources');
  if (sourcesForm) {
    const lines = (name) => (new FormData(sourcesForm).get(name) || '')
      .split('\n').map((s) => s.trim()).filter(Boolean);

    sourcesForm.addEventListener('submit', action('msg-sources', async () => {
      const res = await send('/api/sources', {
        body: { senders: lines('senders'), subjects: lines('subjects') },
      });
      const s = res.setup.sources;
      return `Saved ${s.senders.length} sender(s) and ${s.subjects.length} subject(s).`;
    }));

    $('#btn-preview').addEventListener('click', action('msg-sources', async () => {
      const res = await send('/api/sources/preview');
      return res.results.map((r) =>
        `${r.ok ? '✓' : '✗'} <b>${esc(r.user)}</b> — ${esc(r.detail)}`).join('<br>');
    }, { rerender: false }));
  }
}

function renderAttention() {
  const items = DATA.attention || [];
  if (!items.length) return '';
  return `
  <div class="notice alert alert-warning">
    <i class="bi bi-exclamation-triangle-fill"></i>
    <div>
      <h3>Needs a look (${items.length})</h3>
      <ul>${items.slice(0, 12).map((i) => `
        <li>${i.member ? `<b>${esc(i.member)}</b> · ` : ''}${i.symbol ? `${esc(i.symbol)} · ` : ''}${esc(i.detail)}</li>`).join('')}
      </ul>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------- wiring */

function wire() {
  document.querySelectorAll('.tab').forEach((el) =>
    el.addEventListener('click', () => go(el.dataset.tab)));

  if (active === 'setup') { wireSetup(); return; }

  document.querySelectorAll('[data-goto]').forEach((el) =>
    el.addEventListener('click', () => go(el.dataset.goto, { scroll: true })));

  document.querySelectorAll('[data-row]').forEach((el) =>
    el.addEventListener('click', () => {
      const i = el.dataset.row;
      const detail = document.querySelector(`[data-detail="${i}"]`);
      const caret = document.querySelector(`[data-caret="${i}"]`);
      detail.hidden = !detail.hidden;
      caret.textContent = detail.hidden ? '▸' : '▾';
    }));
}

async function trigger(path, button, label) {
  const job = $('#job');
  button.disabled = true;
  document.body.classList.add('busy');
  job.hidden = false; job.className = 'job'; job.textContent = `${label}…`;
  try {
    const res = await fetch(path, { method: 'POST' });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `request failed (${res.status})`);
    }
    await poll(job);
  } catch (err) {
    job.className = 'job err';
    job.textContent = err.message;
  } finally {
    button.disabled = false;
    document.body.classList.remove('busy');
    setTimeout(() => { job.hidden = true; }, 8000);
  }
}

async function poll(job) {
  for (let i = 0; i < 600; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    const state = await (await fetch('/api/job')).json();
    if (state.state === 'running') { job.textContent = `${state.kind}…`; continue; }
    if (state.state === 'error') { job.className = 'job err'; job.textContent = state.error; return; }
    job.textContent = state.message || 'done';
    await load();
    return;
  }
}

$('#btn-refresh').addEventListener('click', (e) => trigger('/api/refresh', e.currentTarget, 'Fetching prices'));
$('#btn-sync').addEventListener('click', (e) => trigger('/api/sync', e.currentTarget, 'Reading mail'));
$('#btn-add-user').addEventListener('click', () => go('setup', { scroll: true }));
$('#btn-logout').addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' }).catch(() => {});
  location.replace('/login');
});

window.addEventListener('hashchange', () => {
  const id = decodeURIComponent(location.hash.slice(1)) || 'overview';
  if (id !== active) { active = id; render(); }
});

load();
setInterval(() => {
  // Never auto-reload while someone is filling in a setup form, or mid-action —
  // it would wipe what they've typed.
  if (!LIVE || busy || active === 'setup') return;
  if (document.visibilityState === 'visible') load();
}, 120000);
