'use strict';

const $ = (sel, el = document) => el.querySelector(sel);
const view = $('#view');
const dlg = $('#dialog');
const dlgBody = $('#dialog-body');
let current = 'floor';

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (n, d = 2) => (n == null ? '' : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const money = n => Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const when = s => (s ? new Date(s).toLocaleString() : '');
// Variance: positive = unfavourable (cost more than standard), shown red.
const variance = n => {
  const v = Number(n || 0);
  if (Math.abs(v) < 0.005) return '<span class="muted">0.00</span>';
  return `<span class="${v > 0 ? 'unfav' : 'fav'}">${v > 0 ? '+' : '−'}${money(Math.abs(v))}</span>`;
};
const tag = s => `<span class="tag ${esc(s)}">${esc(String(s).replace('_', ' '))}</span>`;

function operator() {
  try { return localStorage.getItem('operator') || ''; } catch { return ''; }
}
function setOperator(v) {
  try { localStorage.setItem('operator', v); } catch { /* storage unavailable */ }
}

async function api(path, opts = {}) {
  const res = await fetch('/api' + path, {
    method: opts.method || (opts.body ? 'POST' : 'GET'),
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    const err = new Error(typeof d === 'string' ? d : d?.message || JSON.stringify(d) || res.statusText);
    err.detail = d;
    err.status = res.status;
    throw err;
  }
  return data;
}
const post = (path, body = {}) => api(path, { method: 'POST', body });

function toast(msg, ms = 3500) {
  const t = $('#toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.style.display = 'none'), ms);
}

async function guarded(fn) {
  try { await fn(); } catch (e) { toast(e.message, 6000); }
}

function openDialog(html, bind) {
  dlgBody.innerHTML = html;
  bind?.(dlgBody);
  if (!dlg.open) dlg.showModal();
}
function closeDialog() { dlg.close(); }

// In-page confirmation (works on tablets and inside embedded views where confirm() is blocked).
function askConfirm(message, okLabel = 'OK') {
  const box = $('#confirm');
  $('#confirm-msg').textContent = message;
  $('#confirm-ok').textContent = okLabel;
  return new Promise(resolve => {
    const done = v => { box.close(); resolve(v); };
    $('#confirm-ok').onclick = () => done(true);
    $('#confirm-cancel').onclick = () => done(false);
    box.oncancel = e => { e.preventDefault(); done(false); };
    box.showModal();
  });
}

// ---------------------------------------------------------------- live updates
let refreshTimer;
function connectStream() {
  const live = $('#live');
  const es = new EventSource('/api/stream');
  es.onopen = () => live.classList.add('on');
  es.onerror = () => live.classList.remove('on');
  es.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => render(true, msg), 150);
  };
}

// ---------------------------------------------------------------- views
const views = {
  async floor(live, msg) {
    const [wip, events] = await Promise.all([api('/wip'), api('/events?limit=25')]);
    const flashJob = msg?.data?.job;
    view.innerHTML = `
      <div class="row" style="margin-bottom:10px">
        <h2 style="margin:0">Work in progress by stage</h2><span class="spacer"></span>
        <label style="flex-direction:row;align-items:center;gap:6px">Operator
          <input id="op-name" value="${esc(operator())}" placeholder="Your name" size="14"></label>
      </div>
      <div class="board">
        ${wip.stages.map(s => `
          <section class="col" style="--c:${esc(s.color)}">
            <div class="col-head"><b>${esc(s.name)}</b>
              <div class="small muted">${num(s.queued)} waiting · ${num(s.in_process)} in process</div></div>
            <div class="col-body">
              ${s.jobs.length ? s.jobs.map(j => `
                <div class="card ${j.number === flashJob ? 'flash' : ''}" data-job="${j.job_id}" data-op="${j.operation_id}">
                  <div class="row"><b>${esc(j.number)}</b><span class="spacer"></span>
                    <span class="small muted">${j.due_date ? 'due ' + esc(j.due_date) : ''}</span></div>
                  <div class="small">${esc(j.item_no)} · ${esc(j.description)}</div>
                  <div class="qty">
                    <div class="wait"><b>${num(j.qty_queued)}</b>waiting</div>
                    <div class="proc"><b>${num(j.qty_in_process)}</b>in process</div>
                    <div><b>${num(j.qty_done)}</b>done</div>
                  </div>
                  ${j.materials.length ? `<div class="mat">Material here: ${j.materials.map(m =>
                    `${esc(m.item_no)} ${num(m.qty)} ${esc(m.uom)}${m.lot_no ? ` <span title="lot">[${esc(m.lot_no)}${m.heat_no ? ' · heat ' + esc(m.heat_no) : ''}]</span>` : ''}`).join(', ')}</div>` : ''}
                </div>`).join('') : '<div class="muted small" style="padding:8px">Nothing here</div>'}
            </div>
          </section>`).join('')}
      </div>
      <div class="row" style="align-items:flex-start;gap:16px;margin-top:16px">
        <div class="panel" style="flex:1;min-width:280px">
          <h2>All units through: ready to close</h2>
          <div class="small muted" style="margin-bottom:6px">Finished units already went to stock and BC as they completed. Closing freezes actual cost and variances.</div>
          ${wip.awaiting_finish.length ? `<table>${wip.awaiting_finish.map(f => `
            <tr><td><a href="#" data-open="${f.job_id}">${esc(f.number)}</a></td><td>${esc(f.item_no)}</td>
            <td class="n">${num(f.qty_good)} good${f.qty_scrap ? ` · ${num(f.qty_scrap)} scrap` : ''}</td>
            <td class="n"><button class="btn" data-finish="${f.job_id}">Close job</button></td></tr>`).join('')}</table>`
            : '<div class="muted">No jobs waiting to close.</div>'}
        </div>
        <div class="panel" style="flex:2;min-width:320px">
          <h2>Recent activity</h2>
          <table>${events.map(e => `<tr><td class="small muted">${when(e.created_at)}</td><td>${esc(e.job)}</td>
            <td>${esc(e.action)}</td><td>${esc(e.stage || '')}</td><td class="n">${num(e.qty)}</td>
            <td class="small">${esc(e.note)}</td><td class="small muted">${esc(e.operator)}</td></tr>`).join('')}</table>
        </div>
      </div>`;
    $('#op-name').onchange = e => setOperator(e.target.value.trim());
    view.querySelectorAll('.card').forEach(c => c.onclick = () => moveDialog(+c.dataset.job, +c.dataset.op));
    view.querySelectorAll('[data-finish]').forEach(b => b.onclick = () => finishJob(+b.dataset.finish));
    view.querySelectorAll('[data-open]').forEach(a => a.onclick = e => { e.preventDefault(); jobDialog(+a.dataset.open); });
  },

  async plan() {
    const [demand, plan, items] = await Promise.all([api('/demand'), api('/plan'), api('/items')]);
    const makeable = items.filter(i => i.item_type !== 'raw');
    view.innerHTML = `
      <div class="panel">
        <div class="row"><h2 style="margin:0">Open demand</h2><span class="spacer"></span>
          <button class="btn" id="sync-demand">Pull demand from BC</button></div>
        <form id="add-demand" class="row" style="margin:10px 0">
          <label>Item<select name="item_id">${makeable.map(i => `<option value="${i.id}">${esc(i.no)} — ${esc(i.description)}</option>`).join('')}</select></label>
          <label>Qty<input name="qty" type="number" step="any" min="0" required style="width:90px"></label>
          <label>Due<input name="due_date" type="date"></label>
          <label>Reference<input name="reference" placeholder="optional"></label>
          <button class="btn primary" style="align-self:flex-end">Add manual demand</button>
        </form>
        <table><tr><th>Source</th><th>Reference</th><th>Item</th><th class="n">Qty</th><th>Due</th><th></th></tr>
          ${demand.map(d => `<tr><td>${tag(d.source)}</td><td>${esc(d.reference)}</td>
            <td>${esc(d.item_no)} <span class="muted">${esc(d.description)}</span></td>
            <td class="n">${num(d.qty)}</td><td>${esc(d.due_date || '')}</td>
            <td class="n"><button class="btn" data-job="${d.id}">Create job</button>
              ${d.source === 'manual' ? `<button class="btn" data-cancel="${d.id}">Cancel</button>` : ''}</td></tr>`).join('')
            || '<tr><td colspan="6" class="muted">No open demand</td></tr>'}
        </table>
      </div>
      <div class="panel">
        <div class="row"><h2 style="margin:0">Material requirements plan</h2><span class="spacer"></span>
          <button class="btn primary" id="make-all" ${plan.summary.make_lines ? '' : 'disabled'}>Create jobs for all "make" lines</button></div>
        <div class="kpis" style="margin:10px 0">
          <div class="kpi"><span class="muted small">To make</span><b>${plan.summary.make_lines}</b></div>
          <div class="kpi"><span class="muted small">Raw materials short</span><b>${plan.summary.buy_lines}</b></div>
          <div class="kpi"><span class="muted small">Shortfall value</span><b>${money(plan.summary.shortfall_value)}</b></div>
        </div>
        <table><tr><th>Level</th><th>Item</th><th>Action</th><th class="n">Required</th><th class="n">On hand</th>
          <th class="n">On order (jobs)</th><th class="n">Net need</th><th>UoM</th><th>Needed by</th><th class="n">Shortfall value</th><th>Driven by</th></tr>
          ${plan.lines.map(l => `<tr><td>${l.level}</td><td>${esc(l.item_no)}<div class="small muted">${esc(l.description)}</div></td>
            <td>${tag(l.action)}</td><td class="n">${num(l.gross, 3)}</td><td class="n">${num(l.on_hand, 3)}</td>
            <td class="n">${num(l.scheduled_receipts, 3)}</td><td class="n"><b>${l.net ? num(l.net, 3) : ''}</b></td>
            <td>${esc(l.uom)}</td><td>${esc(l.earliest_due || '')}</td>
            <td class="n">${l.shortfall_value ? money(l.shortfall_value) : ''}</td>
            <td class="small muted">${l.sources.map(esc).join('<br>')}</td></tr>`).join('')
            || '<tr><td colspan="11" class="muted">Nothing required</td></tr>'}
        </table>
      </div>`;
    $('#add-demand').onsubmit = e => {
      e.preventDefault();
      const f = Object.fromEntries(new FormData(e.target));
      guarded(async () => {
        await post('/demand', { item_id: +f.item_id, qty: +f.qty, due_date: f.due_date || null, reference: f.reference });
        render();
      });
    };
    $('#sync-demand').onclick = () => guarded(async () => { const r = await post('/bc/sync-demand'); toast(`Demand: ${r.created} new, ${r.updated} updated, ${r.closed} closed`); render(); });
    $('#make-all').onclick = () => guarded(async () => { const j = await post('/plan/create-jobs'); toast(`Created ${j.length} planned job(s)`); render(); });
    view.querySelectorAll('[data-job]').forEach(b => b.onclick = () => guarded(async () => {
      const j = await post(`/demand/${b.dataset.job}/create-job`); toast(`Created ${j.number}`); render();
    }));
    view.querySelectorAll('[data-cancel]').forEach(b => b.onclick = () => guarded(async () => {
      await post(`/demand/${b.dataset.cancel}/cancel`); render();
    }));
  },

  async jobs() {
    const filter = views.jobs.filter ?? 'planned,released,in_progress';
    const [jobs, wipValue] = await Promise.all([api('/jobs' + (filter ? '?status=' + filter : '')), api('/wip/value')]);
    view.innerHTML = `
      <div class="panel">
        <div class="row"><h2 style="margin:0">Production jobs</h2>
          <select id="job-filter">
            ${[['planned,released,in_progress', 'Open'], ['completed,posted', 'Completed'], ['', 'All']].map(([v, l]) =>
              `<option value="${v}" ${v === filter ? 'selected' : ''}>${l}</option>`).join('')}
          </select><span class="spacer"></span>
          <span class="muted" title="Cost put into open jobs minus finished output already in stock at standard">Value in WIP: <b>${money(wipValue.total)}</b></span>
          <button class="btn primary" id="new-job">New job</button></div>
        <table style="margin-top:10px"><tr><th>Job</th><th>Item</th><th>Status</th><th class="n">Qty</th><th class="n">Good</th><th class="n">Scrap</th>
          <th>Progress</th><th>Due</th><th class="n">Actual cost</th><th class="n">Actual / unit</th><th class="n">Std / unit</th><th class="n">Variance</th></tr>
          ${jobs.map(j => `<tr data-open="${j.id}" style="cursor:pointer">
            <td><b>${esc(j.number)}</b></td><td>${esc(j.item_no)} <span class="muted small">${esc(j.description)}</span></td>
            <td>${tag(j.status)}</td><td class="n">${num(j.qty)}</td><td class="n">${num(j.qty_good)}</td><td class="n">${num(j.qty_scrap)}</td>
            <td class="small">${j.operations.map(o => `<span title="${esc(o.stage_name)}" style="color:${esc(o.color)}">${esc(o.stage_code)} ${num(o.qty_done)}</span>`).join(' → ')}</td>
            <td>${esc(j.due_date || '')}</td><td class="n">${money(j.costs.total_cost)}</td>
            <td class="n">${j.costs.unit_cost ? money(j.costs.unit_cost) : ''}</td>
            <td class="n">${money(j.costs.output_unit_cost)}</td>
            <td class="n">${j.qty_good ? variance(j.costs.variance.total) : ''}</td></tr>`).join('')
            || '<tr><td colspan="12" class="muted">No jobs</td></tr>'}
        </table>
      </div>`;
    $('#job-filter').onchange = e => { views.jobs.filter = e.target.value; render(); };
    $('#new-job').onclick = newJobDialog;
    view.querySelectorAll('[data-open]').forEach(r => r.onclick = () => jobDialog(+r.dataset.open));
  },

  async recipes() {
    const recipes = await api('/recipes');
    view.innerHTML = `
      <div class="panel">
        <div class="row"><h2 style="margin:0">Recipes (bill of materials + routing)</h2><span class="spacer"></span>
          <button class="btn primary" id="new-recipe">New recipe</button></div>
        ${recipes.map(r => `
          <div class="panel" style="margin:12px 0 0">
            <div class="row"><b>${esc(r.item_no)}</b><span class="muted">${esc(r.item_description)}</span>
              <span class="tag">v${esc(r.version)}</span>${r.active ? '<span class="tag ok">active</span>' : '<span class="tag">inactive</span>'}
              <span class="muted small">makes ${num(r.output_qty)} per batch${r.std_batch_qty ? ` · standard run ${num(r.std_batch_qty)}` : ''}</span><span class="spacer"></span>
              <span>Standard cost <b>${money(r.standard_unit_cost)}</b> / unit</span>
              <button class="btn" data-edit="${r.id}">Edit</button></div>
            <div class="steps">${r.operations.map(o => `<div class="step"><b>${o.sequence} · ${esc(o.stage_name)}</b>
              <div class="small muted">setup ${num(o.setup_minutes)} min · ${num(o.run_minutes_per_unit)} min/unit</div>
              <div class="small">${r.components.filter(c => (c.operation_seq ?? r.operations[0].sequence) === o.sequence)
                .map(c => `${esc(c.item_no)} × ${num(c.qty_per, 4)} ${esc(c.uom)}${c.scrap_pct ? ` (+${num(c.scrap_pct)}%)` : ''}`).join('<br>')}</div>
            </div>`).join('')}</div>
            ${r.notes ? `<div class="small muted">${esc(r.notes)}</div>` : ''}
          </div>`).join('') || '<div class="muted">No recipes yet</div>'}
      </div>`;
    $('#new-recipe').onclick = () => recipeDialog(null);
    view.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => recipeDialog(+b.dataset.edit));
  },

  async lots() {
    const q = views.lots.q || '';
    const [lots, items] = await Promise.all([api('/lots?q=' + encodeURIComponent(q)), api('/items')]);
    const raw = items.filter(i => i.item_type === 'raw').sort((a, b) => b.lot_tracked - a.lot_tracked);
    view.innerHTML = `
      <div class="panel">
        <h2>Receive a lot</h2>
        <div class="small muted" style="margin-bottom:8px">Record each delivery of lot-tracked material with its heat number and mill cert.
          Stock quantities still come from BC; this records which lots make up that stock.</div>
        <form id="rcv" class="row">
          <label>Item<select name="item_id">${raw.map(i => `<option value="${i.id}">${esc(i.no)}${i.lot_tracked ? ' ★' : ''} — ${esc(i.description)}</option>`).join('')}</select></label>
          <label>Lot no.<input name="lot_no" required size="10"></label>
          <label>Heat no.<input name="heat_no" size="10"></label>
          <label>Qty<input name="qty" type="number" step="any" min="0" required style="width:80px"></label>
          <label>Supplier<input name="supplier" size="12"></label>
          <label>Cert ref<input name="cert_ref" size="10"></label>
          <button class="btn primary" style="align-self:flex-end">Receive</button>
        </form>
      </div>
      <div class="panel">
        <div class="row"><h2 style="margin:0">Lots</h2><span class="spacer"></span>
          <input id="lot-q" placeholder="Search lot, heat, item, cert, supplier" value="${esc(q)}" size="34"></div>
        <table style="margin-top:8px"><tr><th>Lot</th><th>Heat</th><th>Item</th><th>Source</th><th>Supplier / cert</th>
          <th class="n">Received</th><th class="n">On hand</th><th>Date</th><th></th></tr>
          ${lots.map(l => `<tr><td><b>${esc(l.lot_no)}</b></td><td>${esc(l.heat_no)}</td>
            <td>${esc(l.item_no)} <span class="muted small">${esc(l.description)}</span></td>
            <td>${l.source === 'output' ? 'made (job)' : 'received'}</td>
            <td class="small">${esc(l.supplier)} ${l.cert_ref ? '· ' + esc(l.cert_ref) : ''}</td>
            <td class="n">${num(l.qty_received, 3)}</td><td class="n">${num(l.qty_on_hand, 3)} ${esc(l.uom)}</td>
            <td class="small muted">${when(l.created_at)}</td>
            <td class="n"><button class="btn" data-trace="${l.id}">Trace</button></td></tr>`).join('')
            || '<tr><td colspan="9" class="muted">No lots</td></tr>'}
        </table>
      </div>`;
    $('#rcv').onsubmit = e => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target));
      guarded(async () => { await post('/lots', { ...f, item_id: +f.item_id, qty: +f.qty }); toast(`Lot ${f.lot_no} received`); render(); }); };
    let t;
    $('#lot-q').oninput = e => { clearTimeout(t); t = setTimeout(() => { views.lots.q = e.target.value; render().then(() => {
      const i = $('#lot-q'); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }); }, 300); };
    view.querySelectorAll('[data-trace]').forEach(b => b.onclick = () => traceDialog(+b.dataset.trace));
  },

  async items() {
    const [items, stages] = await Promise.all([api('/items'), api('/stages')]);
    view.innerHTML = `
      <div class="panel">
        <div class="row"><h2 style="margin:0">Stages</h2><span class="muted small">Rate = labour + overhead per hour, used to cost time at each stage</span></div>
        <table style="margin-top:8px"><tr><th>Seq</th><th>Code</th><th>Name</th><th class="n">Rate / hour</th><th>Colour</th></tr>
          ${stages.map(s => `<tr><td>${s.sequence}</td><td>${esc(s.code)}</td><td>${esc(s.name)}</td>
            <td class="n"><input type="number" step="any" value="${s.rate_per_hour}" data-rate="${s.id}" style="width:90px"></td>
            <td><input type="color" value="${esc(s.color)}" data-color="${s.id}"></td></tr>`).join('')}
        </table>
        <form id="add-stage" class="row" style="margin-top:8px">
          <input name="code" placeholder="Code" required size="6"><input name="name" placeholder="Name" required>
          <input name="sequence" type="number" placeholder="Seq" style="width:70px">
          <input name="rate_per_hour" type="number" step="any" placeholder="Rate/hr" style="width:90px">
          <button class="btn">Add stage</button></form>
      </div>
      <div class="panel">
        <div class="row"><h2 style="margin:0">Items</h2><span class="spacer"></span>
          <button class="btn" id="sync-items">Pull items, costs & stock from BC</button></div>
        <table style="margin-top:8px"><tr><th>No.</th><th>Description</th><th>Type</th><th>UoM</th><th class="n">Std cost</th><th class="n">On hand</th><th>Lot tracked</th><th>Synced from BC</th></tr>
          ${items.map(i => `<tr><td>${esc(i.no)}</td><td>${esc(i.description)}</td>
            <td><select data-type="${i.id}">${['raw', 'sub', 'finished'].map(t => `<option ${t === i.item_type ? 'selected' : ''}>${t}</option>`).join('')}</select></td>
            <td>${esc(i.uom)}</td><td class="n">${money(i.unit_cost)}</td><td class="n">${num(i.qty_on_hand, 3)}</td>
            <td>${i.item_type === 'raw' ? `<input type="checkbox" data-lot="${i.id}" ${i.lot_tracked ? 'checked' : ''}>` : '<span class="small muted">output lot per job</span>'}</td>
            <td class="small muted">${when(i.bc_synced_at)}</td></tr>`).join('')}
        </table>
        <form id="add-item" class="row" style="margin-top:8px">
          <input name="no" placeholder="Item no." required size="12"><input name="description" placeholder="Description">
          <select name="item_type"><option>raw</option><option>sub</option><option>finished</option></select>
          <input name="uom" placeholder="UoM" value="EA" size="5">
          <input name="unit_cost" type="number" step="any" placeholder="Unit cost" style="width:100px">
          <button class="btn">Add item (local only)</button></form>
      </div>`;
    view.querySelectorAll('[data-rate]').forEach(i => i.onchange = () => guarded(() => api(`/stages/${i.dataset.rate}`, { method: 'PATCH', body: { rate_per_hour: +i.value } })));
    view.querySelectorAll('[data-color]').forEach(i => i.onchange = () => guarded(() => api(`/stages/${i.dataset.color}`, { method: 'PATCH', body: { color: i.value } })));
    view.querySelectorAll('[data-lot]').forEach(c => c.onchange = () => guarded(() => api(`/items/${c.dataset.lot}`, { method: 'PATCH', body: { lot_tracked: c.checked } })));
    view.querySelectorAll('[data-type]').forEach(s => s.onchange = () => guarded(() => api(`/items/${s.dataset.type}`, { method: 'PATCH', body: { item_type: s.value } })));
    $('#sync-items').onclick = () => guarded(async () => { const r = await post('/bc/sync-items'); toast(`Items: ${r.created} new, ${r.updated} updated`); render(); });
    $('#add-stage').onsubmit = e => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target));
      guarded(async () => { await post('/stages', { ...f, sequence: +f.sequence || 99, rate_per_hour: +f.rate_per_hour || 0 }); render(); }); };
    $('#add-item').onsubmit = e => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target));
      guarded(async () => { await post('/items', { ...f, unit_cost: +f.unit_cost || 0 }); render(); }); };
  },

  async bc() {
    const [status, postings] = await Promise.all([api('/bc/status'), api('/bc/postings')]);
    view.innerHTML = `
      <div class="panel">
        <div class="row"><h2 style="margin:0">Business Central</h2>
          <span class="tag">${esc(status.mode)} mode</span></div>
        <p class="muted small">Pull brings in items, unit costs, inventory and open sales-order demand. Material consumed on the floor and finished
          goods output are queued below and sent to BC as item journal lines (consumption = negative adjustment, output = positive adjustment at the job's unit cost).</p>
        <div class="row">
          <button class="btn" id="b-items">Pull items & stock</button>
          <button class="btn" id="b-demand">Pull demand</button>
          <button class="btn primary" id="b-post">Send ${(status.postings.pending || 0) + (status.postings.failed || 0)} pending posting(s) to BC</button>
        </div>
        <div class="kpis" style="margin-top:10px">${['pending', 'failed', 'posted'].map(k =>
          `<div class="kpi"><span class="muted small">${k}</span><b>${status.postings[k] || 0}</b></div>`).join('')}</div>
      </div>
      <div class="panel"><h2>Postings</h2>
        <table><tr><th>#</th><th>Kind</th><th>Document</th><th>Item</th><th class="n">Qty</th><th class="n">Unit cost</th><th>Status</th><th>Created</th><th>Error</th></tr>
          ${postings.map(p => `<tr><td>${p.id}</td><td>${esc(p.kind)}</td><td>${esc(p.document_no)}</td><td>${esc(p.item_no)}</td>
            <td class="n">${num(p.qty, 4)}</td><td class="n">${money(p.unit_cost)}</td><td>${tag(p.status)}</td>
            <td class="small muted">${when(p.created_at)}</td><td class="small" style="color:var(--bad)">${esc(p.error)}</td></tr>`).join('')
            || '<tr><td colspan="9" class="muted">Nothing yet</td></tr>'}
        </table></div>`;
    $('#b-items').onclick = () => guarded(async () => { const r = await post('/bc/sync-items'); toast(`Items: ${r.created} new, ${r.updated} updated`); render(); });
    $('#b-demand').onclick = () => guarded(async () => { const r = await post('/bc/sync-demand'); toast(`Demand: ${r.created} new, ${r.updated} updated, ${r.closed} closed`); render(); });
    $('#b-post').onclick = () => guarded(async () => { const r = await post('/bc/process-postings'); toast(`Posted ${r.posted}, failed ${r.failed}`); render(); });
  },
};

// ---------------------------------------------------------------- dialogs
async function moveDialog(jobId, opId) {
  const job = await api(`/jobs/${jobId}`);
  const op = job.operations.find(o => o.id === opId);
  const who = operator();
  // Components consumed when units start here, with the lots available for each.
  const recipe = await api(`/recipes/${job.recipe_id}`);
  const firstSeq = job.operations[0].sequence;
  const comps = recipe.components.filter(c => (c.operation_seq ?? firstSeq) === op.sequence);
  const lotsFor = await Promise.all(comps.map(c => api(`/lots?item_id=${c.item_id}&available=true`)));
  const pickers = comps.map((c, k) => lotsFor[k].length ? `<label>${esc(c.item_no)} lot
      <select data-lotfor="${c.item_id}"><option value="">Oldest first (auto)</option>
      ${lotsFor[k].slice().reverse().map(l => `<option value="${l.id}">${esc(l.lot_no)}${l.heat_no ? ' · heat ' + esc(l.heat_no) : ''} (${num(l.qty_on_hand)} ${esc(l.uom)})</option>`).join('')}
      </select></label>` : '').join('');
  openDialog(`
    <div class="row"><h2 style="margin:0">${esc(job.number)} · ${esc(op.stage_name)}</h2><span class="spacer"></span>
      <a href="#" id="full">Full job</a></div>
    <div class="muted">${esc(job.item_no)} — ${esc(job.description)} · job qty ${num(job.qty)}</div>
    <div class="kpis">
      <div class="kpi"><span class="small muted">Waiting</span><b>${num(op.qty_queued)}</b></div>
      <div class="kpi"><span class="small muted">In process</span><b>${num(op.qty_in_process)}</b></div>
      <div class="kpi"><span class="small muted">Done here</span><b>${num(op.qty_done)}</b></div>
      <div class="kpi"><span class="small muted">Scrapped here</span><b>${num(op.qty_scrap)}</b></div>
    </div>
    ${who ? '' : '<div class="small" style="color:var(--warn)">Tip: set your name in the Operator box so actions are recorded against you.</div>'}
    <div class="row">
      <label>Start (move into ${esc(op.stage_name.toLowerCase())})<input id="q-start" type="number" step="any" value="${op.qty_queued}" style="width:110px"></label>
      <button type="button" class="btn primary big" id="do-start" ${op.qty_queued ? '' : 'disabled'}>Start</button>
    </div>
    ${pickers ? `<div class="row">${pickers}</div>` : ''}
    <div class="row">
      <label>Complete<input id="q-done" type="number" step="any" value="${op.qty_in_process}" style="width:110px"></label>
      <label>Minutes worked (blank = standard)<input id="q-min" type="number" step="any" style="width:110px"></label>
      <button type="button" class="btn primary big" id="do-done" ${op.qty_in_process ? '' : 'disabled'}>Complete</button>
    </div>
    <div class="row">
      <label>Scrap<input id="q-scrap" type="number" step="any" style="width:110px"></label>
      <label>From<select id="q-from"><option value="in_process">in process</option><option value="queued">waiting</option></select></label>
      <label>Reason<input id="q-note"></label>
      <button type="button" class="btn" id="do-scrap">Scrap</button>
    </div>
    <div class="row"><span class="spacer"></span><button class="btn">Close</button></div>`, root => {
    $('#full', root).onclick = e => { e.preventDefault(); jobDialog(jobId); };
    const act = (path, body) => guarded(async () => {
      try {
        await post(`/jobs/${jobId}/${path}`, { operation_id: opId, operator: operator(), ...body });
      } catch (e) {
        if (e.detail?.shortages && await askConfirm(`${e.message}. Start anyway? Stock will go negative.`, 'Start anyway')) {
          await post(`/jobs/${jobId}/${path}`, { operation_id: opId, operator: operator(), ...body, force: true });
        } else throw e;
      }
      closeDialog();
      render();
    });
    $('#do-start', root).onclick = () => {
      const lot_choices = {};
      root.querySelectorAll('[data-lotfor]').forEach(s => { if (s.value) lot_choices[s.dataset.lotfor] = +s.value; });
      act('start', { qty: +$('#q-start', root).value, lot_choices });
    };
    $('#do-done', root).onclick = () => {
      const m = $('#q-min', root).value;
      act('complete', { qty: +$('#q-done', root).value, minutes: m === '' ? null : +m });
    };
    $('#do-scrap', root).onclick = () => act('scrap', { qty: +$('#q-scrap', root).value, from_state: $('#q-from', root).value, note: $('#q-note', root).value });
  });
}

async function jobDialog(jobId) {
  const j = await api(`/jobs/${jobId}`);
  const c = j.costs;
  openDialog(`
    <div class="row"><h2 style="margin:0">${esc(j.number)}</h2>${tag(j.status)}<span class="spacer"></span>
      ${j.status === 'planned' ? '<button type="button" class="btn primary" id="j-release">Release to floor</button><button type="button" class="btn" id="j-cancel">Cancel job</button>' : ''}
      ${['released', 'in_progress'].includes(j.status) ? '<button type="button" class="btn primary" id="j-finish">Close job</button>' : ''}</div>
    <div>${esc(j.item_no)} — ${esc(j.description)} · qty ${num(j.qty)} · good ${num(j.qty_good)} · scrap ${num(j.qty_scrap)}${j.due_date ? ' · due ' + esc(j.due_date) : ''}</div>
    <div class="kpis">
      <div class="kpi"><span class="small muted">Actual material</span><b>${money(c.material_cost)}</b></div>
      <div class="kpi"><span class="small muted">Actual labour & overhead</span><b>${money(c.conversion_cost)}</b></div>
      <div class="kpi"><span class="small muted">Actual / unit</span><b>${c.unit_cost ? money(c.unit_cost) : '—'}</b></div>
      <div class="kpi"><span class="small muted">BC standard / unit</span><b>${money(c.output_unit_cost)}</b></div>
      <div class="kpi"><span class="small muted">Into stock at standard</span><b>${money(c.bc_value)}</b></div>
      <div class="kpi"><span class="small muted">Total variance</span><b>${variance(c.variance.total)}</b></div>
    </div>
    ${j.qty_good ? `<div class="small muted">Variance = material usage ${variance(c.variance.material_usage)} + labour & overhead ${variance(c.variance.conversion)}
      + recipe vs BC standard ${variance(c.variance.standard_gap)} <span title="Positive = unfavourable (cost more than standard)">ⓘ</span>
      ${Math.abs(c.std_unit_cost - c.output_unit_cost) > 0.005 ? `<br>Recipe standard is ${money(c.std_unit_cost)}/unit vs BC standard ${money(c.output_unit_cost)}; consider updating the BC standard.` : ''}</div>` : ''}
    <div class="steps">${j.operations.map(o => `<div class="step" style="--c:${esc(o.color)}">
      <b>${esc(o.stage_name)}</b>
      <div class="small">waiting ${num(o.qty_queued)} · in process ${num(o.qty_in_process)}</div>
      <div class="small">done ${num(o.qty_done)} · scrap ${num(o.qty_scrap)}</div>
      <div class="small muted">${num(o.actual_minutes)} min</div></div>`).join('')}</div>
    <h2>Material: actual vs standard for ${num(j.qty_good)} good</h2>
    <table><tr><th>Item</th><th class="n">Std qty</th><th class="n">Actual qty</th><th class="n">Std cost</th><th class="n">Variance</th></tr>
      ${c.materials.map(m => `<tr><td>${esc(m.item_no)}</td><td class="n">${num(m.std_qty, 4)}</td><td class="n">${num(m.actual_qty, 4)} ${esc(m.uom)}</td>
        <td class="n">${money(m.std_cost)}</td><td class="n">${variance(m.variance)}</td></tr>`).join('')}</table>
    <h2>Stages: actual vs standard time</h2>
    <table><tr><th>Stage</th><th class="n">Rate / hr</th><th class="n">Std min</th><th class="n">Actual min</th><th class="n">Variance</th></tr>
      ${c.stages.map(s => `<tr><td>${esc(s.stage)}</td><td class="n">${money(s.rate)}</td><td class="n">${num(s.std_minutes)}</td>
        <td class="n">${num(s.actual_minutes)}</td><td class="n">${variance(s.variance)}</td></tr>`).join('')}</table>
    <h2>Material consumed (lots)</h2>
    <table><tr><th>Item</th><th>Lot</th><th>Heat</th><th class="n">Qty</th><th class="n">Std cost</th><th class="n">Value</th><th>When</th></tr>
      ${j.issues.map(i => `<tr><td>${esc(i.item_no)} <span class="muted small">${esc(i.description)}</span></td>
        <td>${i.lot_id ? `<a href="#" data-lot="${i.lot_id}">${esc(i.lot_no)}</a>` : '<span class="muted">—</span>'}</td><td>${esc(i.heat_no)}</td>
        <td class="n">${num(i.qty, 4)} ${esc(i.uom)}</td><td class="n">${money(i.unit_cost)}</td><td class="n">${money(i.value)}</td>
        <td class="small muted">${when(i.created_at)}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">None yet</td></tr>'}</table>
    <h2>History</h2>
    <table>${j.events.map(e => `<tr><td class="small muted">${when(e.created_at)}</td><td>${esc(e.action)}</td><td>${esc(e.stage || '')}</td>
      <td class="n">${num(e.qty)}</td><td class="small">${esc(e.note)}</td><td class="small muted">${esc(e.operator)}</td></tr>`).join('')}</table>
    <div class="row"><span class="spacer"></span><button class="btn">Close</button></div>`, root => {
    const act = path => guarded(async () => { await post(`/jobs/${jobId}/${path}?operator=${encodeURIComponent(operator())}`); jobDialog(jobId); render(); });
    $('#j-release', root) && ($('#j-release', root).onclick = () => act('release'));
    $('#j-cancel', root) && ($('#j-cancel', root).onclick = () => act('cancel'));
    $('#j-finish', root) && ($('#j-finish', root).onclick = () => finishJob(jobId));
    root.querySelectorAll('[data-lot]').forEach(a => a.onclick = e => { e.preventDefault(); traceDialog(+a.dataset.lot); });
  });
}

async function finishJob(jobId) {
  if (!await askConfirm('Close this job? Actual cost and variances are frozen. Any units still waiting at a stage are closed short.', 'Close job')) return;
  guarded(async () => {
    const j = await post(`/jobs/${jobId}/finish?operator=${encodeURIComponent(operator())}`);
    toast(`${j.number} closed: ${num(j.qty_good)} good, actual ${money(j.costs.unit_cost)}/unit, variance ${money(j.costs.variance.total)}`);
    if (dlg.open) closeDialog();
    render();
  });
}

function traceTree(node, dir) {
  const label = n => n.source === 'unlotted' ? `<span class="muted">no lot</span>`
    : n.source === 'wip' ? `<span class="muted">in WIP on ${esc(n.job)}</span>`
    : `<a href="#" data-lot="${n.lot_id}"><b>${esc(n.lot_no)}</b></a>`;
  const li = (n, root) => `<li>${label(n)} · ${esc(n.item_no)} <span class="muted small">${esc(n.description)}</span>
    ${n.heat_no ? `<span class="tag">heat ${esc(n.heat_no)}</span>` : ''}
    <span class="small">${root ? (n.source === 'output' ? 'made ' : 'received ') + num(n.qty, 4) + ' ' + esc(n.uom) : dir === 'back' ? num(n.qty, 4) + ' ' + esc(n.uom) + ' used' : n.consumed_qty != null ? num(n.consumed_qty, 4) + ' used → made ' + num(n.qty, 4) + ' ' + esc(n.uom) : num(n.qty, 4) + ' ' + esc(n.uom)}</span>
    ${n.supplier || n.cert_ref ? `<span class="small muted">${esc(n.supplier)} ${esc(n.cert_ref)}</span>` : ''}
    ${n.children.length ? `<ul>${n.children.map(c => li(c, false)).join('')}</ul>` : ''}</li>`;
  return `<ul class="tree">${li(node, true)}</ul>`;
}

async function traceDialog(lotId) {
  const t = await api(`/lots/${lotId}/trace`);
  const l = t.lot;
  openDialog(`
    <div class="row"><h2 style="margin:0">Lot ${esc(l.lot_no)}</h2><span class="tag">${l.source === 'output' ? 'made' : 'received'}</span>
      ${l.heat_no ? `<span class="tag">heat ${esc(l.heat_no)}</span>` : ''}</div>
    <div>${esc(l.item_no)} — ${esc(l.description)} · received ${num(l.qty_received, 3)} · on hand ${num(l.qty_on_hand, 3)} ${esc(l.uom)}
      ${l.supplier ? ' · ' + esc(l.supplier) : ''}${l.cert_ref ? ' · cert ' + esc(l.cert_ref) : ''}</div>
    <h2>Made from (backward)</h2>
    ${t.backward.children.length ? traceTree(t.backward, 'back') : '<div class="muted">Received material, not made here.</div>'}
    <h2>Used in (forward)</h2>
    ${t.forward.children.length ? traceTree(t.forward, 'fwd') : '<div class="muted">Not used in any job yet.</div>'}
    <div class="row"><span class="spacer"></span><button class="btn">Close</button></div>`, root => {
    root.querySelectorAll('[data-lot]').forEach(a => a.onclick = e => { e.preventDefault(); traceDialog(+a.dataset.lot); });
  });
}

async function newJobDialog() {
  const items = (await api('/items')).filter(i => i.item_type !== 'raw');
  openDialog(`<h2>New production job</h2>
    <label>Item<select id="nj-item">${items.map(i => `<option value="${i.id}">${esc(i.no)} — ${esc(i.description)}</option>`).join('')}</select></label>
    <label>Quantity<input id="nj-qty" type="number" step="any" min="0"></label>
    <label>Due date<input id="nj-due" type="date"></label>
    <div class="row"><span class="spacer"></span><button class="btn">Cancel</button>
      <button type="button" class="btn primary" id="nj-go">Create</button></div>`, root => {
    $('#nj-go', root).onclick = () => guarded(async () => {
      const j = await post('/jobs', { item_id: +$('#nj-item', root).value, qty: +$('#nj-qty', root).value, due_date: $('#nj-due', root).value || null });
      toast(`Created ${j.number}`);
      closeDialog();
      render();
    });
  });
}

async function recipeDialog(recipeId) {
  const [items, stages, r] = await Promise.all([api('/items'), api('/stages'), recipeId ? api(`/recipes/${recipeId}`) : null]);
  const recipe = r || { item_id: items.find(i => i.item_type !== 'raw')?.id, version: '1', output_qty: 1, active: true, notes: '',
    operations: [{ stage_id: stages[0]?.id, sequence: 10, setup_minutes: 0, run_minutes_per_unit: 0 }], components: [] };
  const itemOpts = sel => items.map(i => `<option value="${i.id}" ${i.id === sel ? 'selected' : ''}>${esc(i.no)} — ${esc(i.description)}</option>`).join('');
  const stageOpts = sel => stages.map(s => `<option value="${s.id}" ${s.id === sel ? 'selected' : ''}>${esc(s.name)}</option>`).join('');
  const opRow = o => `<tr class="op"><td><input class="seq" type="number" value="${o.sequence}" style="width:60px"></td>
    <td><select class="stage">${stageOpts(o.stage_id)}</select></td>
    <td><input class="setup" type="number" step="any" value="${o.setup_minutes}" style="width:80px"></td>
    <td><input class="run" type="number" step="any" value="${o.run_minutes_per_unit}" style="width:80px"></td>
    <td><button type="button" class="btn del">✕</button></td></tr>`;
  const compRow = c => `<tr class="comp"><td><select class="item">${itemOpts(c.item_id)}</select></td>
    <td><input class="qty" type="number" step="any" value="${c.qty_per ?? ''}" style="width:90px"></td>
    <td><input class="scrap" type="number" step="any" value="${c.scrap_pct ?? 0}" style="width:70px"></td>
    <td><input class="at" type="number" value="${c.operation_seq ?? ''}" placeholder="first" style="width:70px"></td>
    <td><button type="button" class="btn del">✕</button></td></tr>`;
  openDialog(`<h2>${recipeId ? 'Edit' : 'New'} recipe</h2>
    <div class="row">
      <label style="flex:1">Makes item<select id="r-item">${itemOpts(recipe.item_id)}</select></label>
      <label>Version<input id="r-ver" value="${esc(recipe.version)}" size="4"></label>
      <label>Output qty per batch<input id="r-out" type="number" step="any" value="${recipe.output_qty}" style="width:90px"></label>
      <label title="Typical run size: setup time is spread over this many units in the standard cost">Std run size<input id="r-batch" type="number" step="any" value="${recipe.std_batch_qty ?? ''}" placeholder="= output" style="width:90px"></label>
      <label>Active<input id="r-active" type="checkbox" ${recipe.active ? 'checked' : ''}></label>
    </div>
    <b>Stages (routing)</b>
    <table id="r-ops"><tr><th>Seq</th><th>Stage</th><th>Setup min</th><th>Run min / unit</th><th></th></tr>${recipe.operations.map(opRow).join('')}</table>
    <div><button type="button" class="btn" id="r-add-op">+ Stage</button></div>
    <b>Components (per output qty)</b>
    <table id="r-comps"><tr><th>Item</th><th>Qty</th><th>Scrap %</th><th>Used at stage seq</th><th></th></tr>${recipe.components.map(compRow).join('')}</table>
    <div><button type="button" class="btn" id="r-add-comp">+ Component</button></div>
    <label>Notes<textarea id="r-notes" rows="2">${esc(recipe.notes)}</textarea></label>
    <div class="row"><span class="spacer"></span><button class="btn">Cancel</button><button type="button" class="btn primary" id="r-save">Save</button></div>`, root => {
    const bindDel = () => root.querySelectorAll('.del').forEach(b => b.onclick = () => b.closest('tr').remove());
    bindDel();
    $('#r-add-op', root).onclick = () => {
      const seqs = [...root.querySelectorAll('.op .seq')].map(i => +i.value);
      $('#r-ops tbody', root).insertAdjacentHTML('beforeend', opRow({ stage_id: stages[0]?.id, sequence: (Math.max(0, ...seqs) + 10), setup_minutes: 0, run_minutes_per_unit: 0 }));
      bindDel();
    };
    $('#r-add-comp', root).onclick = () => {
      $('#r-comps tbody', root).insertAdjacentHTML('beforeend', compRow({ item_id: items.find(i => i.item_type === 'raw')?.id, scrap_pct: 0 }));
      bindDel();
    };
    $('#r-save', root).onclick = () => guarded(async () => {
      const body = {
        item_id: +$('#r-item', root).value, version: $('#r-ver', root).value, output_qty: +$('#r-out', root).value,
        std_batch_qty: $('#r-batch', root).value === '' ? null : +$('#r-batch', root).value,
        active: $('#r-active', root).checked, notes: $('#r-notes', root).value,
        operations: [...root.querySelectorAll('.op')].map(tr => ({
          sequence: +$('.seq', tr).value, stage_id: +$('.stage', tr).value,
          setup_minutes: +$('.setup', tr).value || 0, run_minutes_per_unit: +$('.run', tr).value || 0 })),
        components: [...root.querySelectorAll('.comp')].map(tr => ({
          item_id: +$('.item', tr).value, qty_per: +$('.qty', tr).value, scrap_pct: +$('.scrap', tr).value || 0,
          operation_seq: $('.at', tr).value === '' ? null : +$('.at', tr).value })),
      };
      await api(recipeId ? `/recipes/${recipeId}` : '/recipes', { method: recipeId ? 'PUT' : 'POST', body });
      toast('Recipe saved');
      closeDialog();
      render();
    });
  });
}

// ---------------------------------------------------------------- shell
async function render(live = false, msg = null) {
  if (live && dlg.open) return; // don't yank a dialog out from under someone
  try {
    await views[current](live, msg);
  } catch (e) {
    view.innerHTML = `<div class="panel" style="color:var(--bad)">${esc(e.message)}</div>`;
  }
}

$('#tabs').onclick = e => {
  const b = e.target.closest('button');
  if (!b) return;
  current = b.dataset.view;
  document.querySelectorAll('#tabs button').forEach(x => x.classList.toggle('active', x === b));
  render();
};

render();
connectStream();
