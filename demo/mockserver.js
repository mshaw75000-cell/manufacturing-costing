/*
 * In-browser stand-in for the Python API, used only by the phone demo
 * (demo/build.py). It ports the server logic in app/ (planning, production,
 * costing, lots/trace, purchasing, issues/notes, reports, mock BC) so the
 * unchanged web UI can run without a server. The Python code in app/ is the
 * real implementation; keep this in step when behaviour there changes.
 *
 * Starting data is the trial dataset exported by demo/export_state.py
 * (window.MFG_SEED). Dates are shifted so the data is always current.
 * Changes persist in localStorage.
 */
'use strict';
(function () {
  const EPS = 1e-9;
  const KEY = 'mfgcost-demo-v3';
  const DAY = 86400000;
  const r4 = x => Math.round(x * 1e4) / 1e4;
  const r2 = x => Math.round(x * 100) / 100;
  const nowD = () => new Date();
  const now = () => new Date().toISOString();
  const isoDate = d => d.toISOString().slice(0, 10);
  const today = (days = 0) => isoDate(new Date(Date.now() + days * DAY));

  class ApiError extends Error {
    constructor(status, detail) { super(typeof detail === 'string' ? detail : detail.message); this.status = status; this.detail = detail; }
  }
  const fail = (status, detail) => { throw new ApiError(status, detail); };

  // ------------------------------------------------------------------ state
  let S;
  const nextId = t => (S.seq[t] = (S.seq[t] || 0) + 1);
  const add = (t, row) => { row.id = nextId(t); if (!('created_at' in row) && t !== 'items' && t !== 'stages') row.created_at = now(); S[t].push(row); return row; };
  const byId = (t, id) => S[t].find(r => r.id === id);
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch { /* storage full or unavailable */ } };

  // Shift every date in the seed forward so "now" in the data is today.
  function shiftDates(obj, ms, days) {
    if (Array.isArray(obj)) { obj.forEach(o => shiftDates(o, ms, days)); return; }
    if (!obj || typeof obj !== 'object') return;
    for (const [k, v] of Object.entries(obj)) {
      if (typeof v === 'string' && /(_at|_date|needed_by)$/.test(k)) {
        if (/^\d{4}-\d\d-\d\dT/.test(v)) obj[k] = new Date(Date.parse(v) + ms).toISOString();
        else if (/^\d{4}-\d\d-\d\d$/.test(v)) obj[k] = isoDate(new Date(Date.parse(v + 'T00:00:00Z') + days * DAY));
      } else if (v && typeof v === 'object') shiftDates(v, ms, days);
    }
  }

  function seed() {
    S = JSON.parse(JSON.stringify(window.MFG_SEED));
    const built = Date.parse(S.built_at);
    const ms = Date.now() - built;
    const days = Math.round((Date.parse(today() + 'T00:00:00Z') - Date.parse(S.built_at.slice(0, 10) + 'T00:00:00Z')) / DAY);
    shiftDates(S, ms, days);
    save();
  }

  function load() {
    try { const raw = localStorage.getItem(KEY); if (raw) { S = JSON.parse(raw); return; } } catch { /* fall through */ }
    seed();
  }

  // ------------------------------------------------------------------ helpers
  const item = id => byId('items', id);
  const stage = id => byId('stages', id);
  const activeRecipe = itemId => S.recipes.filter(r => r.item_id === itemId && r.active).sort((a, b) => b.id - a.id)[0];
  const activeRecipes = () => { const m = {}; for (const r of S.recipes) if (r.active && (!m[r.item_id] || r.id > m[r.item_id].id)) m[r.item_id] = r; return m; };
  const componentQty = (recipe, c, outQty) => outQty / recipe.output_qty * c.qty_per * (1 + c.scrap_pct / 100);
  let issuesByJob = null; // cache, rebuilt per request
  const jobIssues = job => {
    if (!issuesByJob) { issuesByJob = new Map(); for (const i of S.issues) { if (!issuesByJob.has(i.job_id)) issuesByJob.set(i.job_id, []); issuesByJob.get(i.job_id).push(i); } }
    return issuesByJob.get(job.id) || [];
  };
  const jobRecipe = job => byId('recipes', job.recipe_id);
  const newLot = l => add('lots', { heat_no: '', supplier: '', cert_ref: '', source: 'receipt', job_id: null, notes: '', ...l });
  const OPEN_JOB = ['planned', 'released', 'in_progress'];
  const OPEN_PR = ['draft', 'sent'];
  const byTimeDesc = (a, b) => (b.created_at || '').localeCompare(a.created_at || '') || b.id - a.id;

  // ------------------------------------------------------------------ planning
  function jobRemainingComponents(job) {
    const issued = {};
    for (const i of jobIssues(job)) issued[i.item_id] = (issued[i.item_id] || 0) + i.qty;
    const rem = {}; const r = jobRecipe(job);
    for (const c of r.components) {
      const need = componentQty(r, c, job.qty) - (issued[c.item_id] || 0);
      if (need > EPS) rem[c.item_id] = (rem[c.item_id] || 0) + need;
    }
    return rem;
  }
  const jobRemainingOutput = job => Math.max(0, job.qty - job.qty_good - job.qty_scrap);

  function lowLevelCodes(recipes) {
    const codes = {};
    const walk = (itemId, level, path) => {
      if (path.includes(itemId)) fail(422, `Recipe cycle involving item ${item(itemId).no}`);
      codes[itemId] = Math.max(codes[itemId] ?? 0, level);
      const r = recipes[itemId];
      if (r) for (const c of r.components) walk(c.item_id, level + 1, [...path, itemId]);
    };
    for (const id of Object.keys(recipes)) walk(+id, 0, []);
    return codes;
  }

  function buildPlan() {
    const recipes = activeRecipes();
    const gross = {}, receipts = {}, due = {}, sources = {};
    const addG = (id, q) => { gross[id] = (gross[id] || 0) + q; };
    const src = (id, s) => { (sources[id] = sources[id] || []).push(s); };
    const noteDue = (id, d) => { if (d && (!due[id] || d < due[id])) due[id] = d; };
    for (const d of S.demands.filter(d => d.status === 'open')) {
      addG(d.item_id, d.qty); noteDue(d.item_id, d.due_date);
      src(d.item_id, `${d.source.toUpperCase()} ${d.reference || '#' + d.id}: ${+d.qty.toFixed(4)}`);
    }
    for (const pr of S.purchase_requests.filter(p => OPEN_PR.includes(p.status))) {
      for (const ln of pr.lines) {
        receipts[ln.item_id] = (receipts[ln.item_id] || 0) + ln.qty;
        src(ln.item_id, `On order ${pr.bc_po_no || pr.number}: ${+ln.qty.toFixed(4)}`);
      }
    }
    for (const job of S.jobs.filter(j => OPEN_JOB.includes(j.status))) {
      receipts[job.item_id] = (receipts[job.item_id] || 0) + jobRemainingOutput(job);
      for (const [id, q] of Object.entries(jobRemainingComponents(job))) {
        addG(+id, q); noteDue(+id, job.due_date); src(+id, `Job ${job.number}: ${+q.toFixed(4)}`);
      }
    }
    const codes = lowLevelCodes(recipes);
    for (const id of Object.keys(gross)) codes[id] = codes[id] ?? 0;
    const maxLevel = Math.max(0, ...Object.values(codes));
    const lines = [];
    for (let level = 0; level <= maxLevel; level++) {
      for (const id of Object.keys(codes).filter(i => codes[i] === level).map(Number)) {
        const it = item(id);
        if ((gross[id] || 0) <= EPS && !(id in receipts)) continue;
        const line = { item_id: id, item_no: it.no, description: it.description, item_type: it.item_type, uom: it.uom, level,
          gross: gross[id] || 0, on_hand: it.qty_on_hand, scheduled_receipts: receipts[id] || 0, net: 0, action: 'ok',
          unit_cost: it.unit_cost, shortfall_value: 0, earliest_due: due[id] || null, sources: sources[id] || [] };
        line.net = Math.max(0, line.gross - Math.max(0, line.on_hand) - line.scheduled_receipts);
        const r = recipes[id];
        if (line.net > EPS) {
          if (r) {
            line.action = 'make';
            const makeQty = Math.ceil(line.net / r.output_qty - EPS) * r.output_qty;
            line.net = makeQty;
            for (const c of r.components) {
              const q = componentQty(r, c, makeQty);
              addG(c.item_id, q); noteDue(c.item_id, line.earliest_due); src(c.item_id, `Planned ${it.no}: ${+q.toFixed(4)}`);
            }
          } else {
            line.action = 'buy';
            line.shortfall_value = line.net * it.unit_cost;
          }
        }
        lines.push(line);
      }
    }
    lines.sort((a, b) => a.level - b.level || a.item_no.localeCompare(b.item_no));
    const buy = lines.filter(l => l.action === 'buy');
    return { lines, summary: { make_lines: lines.filter(l => l.action === 'make').length, buy_lines: buy.length,
      shortfall_value: r2(buy.reduce((a, l) => a + l.shortfall_value, 0)) } };
  }

  // ------------------------------------------------------------------ costing
  function componentStdCost(it, active, seen) {
    if (it.unit_cost > 0) return it.unit_cost;
    const sub = active[it.id];
    return sub && !seen.includes(sub.id) ? standardUnitCost(sub, active, seen) : 0;
  }
  function standardUnitCost(r, active, seen = []) {
    if (!r.output_qty) return 0;
    const s2 = [...seen, r.id];
    const material = r.components.reduce((a, c) => a + c.qty_per / r.output_qty * (1 + c.scrap_pct / 100) * componentStdCost(item(c.item_id), active, s2), 0);
    const batch = r.std_batch_qty || r.output_qty;
    const conversion = r.operations.reduce((a, o) => a + (o.setup_minutes / batch + o.run_minutes_per_unit) / 60 * stage(o.stage_id).rate_per_hour, 0);
    return material + conversion;
  }
  const outputUnitCost = job => (item(job.item_id).unit_cost > 0 ? item(job.item_id).unit_cost : job.std_unit_cost);

  function jobCosts(job) {
    const good = job.qty_good, r = jobRecipe(job);
    const issued = {};
    for (const i of jobIssues(job)) {
      const row = issued[i.item_id] = issued[i.item_id] || { item_no: item(i.item_id).no, qty: 0, value: 0 };
      row.qty += i.qty; row.value += i.qty * i.unit_cost;
    }
    const materials = [];
    for (const c of r.components) {
      const act = issued[c.item_id] || { qty: 0, value: 0 }; delete issued[c.item_id];
      const it = item(c.item_id), stdQty = componentQty(r, c, good);
      materials.push({ item_no: it.no, uom: it.uom, std_qty: stdQty, actual_qty: act.qty, std_cost: it.unit_cost,
        actual_value: act.value, std_value: stdQty * it.unit_cost, variance: act.value - stdQty * it.unit_cost });
    }
    for (const act of Object.values(issued)) {
      materials.push({ item_no: act.item_no, uom: '', std_qty: 0, actual_qty: act.qty, std_cost: 0, actual_value: act.value, std_value: 0, variance: act.value });
    }
    const stages = job.operations.map(op => {
      const rate = stage(op.stage_id).rate_per_hour;
      const stdMin = good > 0 ? op.setup_minutes + op.run_minutes_per_unit * good : 0;
      return { stage: stage(op.stage_id).name, rate, std_minutes: stdMin, actual_minutes: op.actual_minutes,
        std_value: stdMin / 60 * rate, actual_value: op.actual_minutes / 60 * rate, variance: (op.actual_minutes - stdMin) / 60 * rate };
    });
    const sum = (rows, k) => rows.reduce((a, x) => a + x[k], 0);
    const actMat = sum(materials, 'actual_value'), actConv = sum(stages, 'actual_value');
    const stdMat = sum(materials, 'std_value'), stdConv = sum(stages, 'std_value');
    const total = actMat + actConv, outCost = outputUnitCost(job), bcValue = good * outCost;
    const round = rows => rows.map(x => Object.fromEntries(Object.entries(x).map(([k, v]) => [k, typeof v === 'number' ? r4(v) : v])));
    return {
      material_cost: r4(actMat), conversion_cost: r4(actConv), total_cost: r4(total), unit_cost: good > 0 ? r4(total / good) : 0,
      std_unit_cost: r4(job.std_unit_cost), output_unit_cost: r4(outCost), bc_value: r4(bcValue),
      variance: { material_usage: r4(actMat - stdMat), conversion: r4(actConv - stdConv), standard_gap: r4(stdMat + stdConv - bcValue), total: r4(total - bcValue) },
      materials: round(materials), stages: round(stages),
    };
  }

  // ------------------------------------------------------------------ production
  function log(job, op, action, qty, extra = {}) {
    add('events', { job_id: job.id, job_operation_id: op ? op.id : null, stage_id: op ? op.stage_id : null, action, qty,
      minutes: extra.minutes || 0, operator: extra.operator || '', note: extra.note || '' });
  }

  function createJob(itemId, qty, dueDate = null, demandId = null) {
    if (!(qty > 0)) fail(409, 'Quantity must be positive');
    const r = activeRecipe(itemId);
    if (!r) fail(409, 'No active recipe for this item');
    if (!r.operations.length) fail(409, 'Recipe has no operations (stages)');
    const n = (S.seq.jobs || 0) + 1;
    const job = add('jobs', { number: 'MO' + String(n).padStart(6, '0'), item_id: itemId, recipe_id: r.id, demand_id: demandId, qty,
      status: 'planned', due_date: dueDate, qty_good: 0, qty_scrap: 0, material_cost: 0, conversion_cost: 0, unit_cost: 0,
      std_unit_cost: r4(standardUnitCost(r, activeRecipes())), cost_snapshot: null, completed_at: null,
      operations: [...r.operations].sort((a, b) => a.sequence - b.sequence).map(o => ({ id: nextId('job_operations'), job_id: null, stage_id: o.stage_id,
        sequence: o.sequence, setup_minutes: o.setup_minutes, run_minutes_per_unit: o.run_minutes_per_unit, qty_queued: 0,
        qty_in_process: 0, qty_done: 0, qty_scrap: 0, actual_minutes: 0, started_at: null, completed_at: null })) });
    job.operations.forEach(o => { o.job_id = job.id; });
    log(job, null, 'create', qty);
    return job;
  }

  function release(job, operator) {
    if (job.status !== 'planned') fail(409, `Job is ${job.status}, only planned jobs can be released`);
    job.status = 'released';
    job.operations[0].qty_queued = job.qty;
    log(job, job.operations[0], 'release', job.qty, { operator });
  }

  const getOp = (job, opId) => job.operations.find(o => o.id === opId) || fail(409, 'Operation not found on this job');
  const compsForOp = (job, op) => {
    const first = job.operations[0].sequence;
    return jobRecipe(job).components.filter(c => (c.operation_seq ?? first) === op.sequence);
  };

  function start(job, opId, qty, operator, force, lotChoices) {
    if (!['released', 'in_progress'].includes(job.status)) fail(409, `Job is ${job.status}`);
    const op = getOp(job, opId);
    if (!(qty > 0) || qty > op.qty_queued + EPS) fail(409, `Can start between 0 and ${op.qty_queued} at this stage`);
    const needs = compsForOp(job, op).map(c => [c, componentQty(jobRecipe(job), c, qty)]);
    const shortages = needs.filter(([c, q]) => item(c.item_id).qty_on_hand + EPS < q)
      .map(([c, q]) => ({ item_no: item(c.item_id).no, need: q, on_hand: item(c.item_id).qty_on_hand }));
    if (shortages.length && !force) {
      fail(409, { message: 'Insufficient stock: ' + shortages.map(s => `${s.item_no} (need ${+s.need.toFixed(4)}, have ${+s.on_hand.toFixed(4)})`).join(', '), shortages });
    }
    op.qty_queued -= qty; op.qty_in_process += qty;
    let setup = 0;
    if (!op.started_at) { op.started_at = now(); setup = op.setup_minutes; op.actual_minutes += setup; }
    job.status = 'in_progress';
    for (const [c, q] of needs) issueMaterial(job, item(c.item_id), q, op, operator, (lotChoices || {})[c.item_id]);
    log(job, op, 'start', qty, { operator, minutes: setup });
  }

  function allocateLots(it, qty, preferred) {
    let lots = S.lots.filter(l => l.item_id === it.id && l.qty_on_hand > EPS).sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id - b.id);
    if (preferred) {
      const pref = lots.filter(l => l.id === preferred);
      if (!pref.length) fail(409, `That lot has no stock of ${it.no}`);
      lots = [...pref, ...lots.filter(l => l.id !== preferred)];
    }
    const parts = []; let rem = qty;
    for (const l of lots) { if (rem <= EPS) break; const take = Math.min(l.qty_on_hand, rem); parts.push([l, take]); rem -= take; }
    if (rem > EPS) parts.push([null, rem]);
    return parts;
  }

  function issueMaterial(job, it, qty, op, operator, preferred) {
    const parts = allocateLots(it, qty, preferred);
    it.qty_on_hand -= qty;
    for (const [lot, q] of parts) {
      if (lot) lot.qty_on_hand -= q;
      add('issues', { job_id: job.id, job_operation_id: op ? op.id : null, item_id: it.id, lot_id: lot ? lot.id : null, qty: q, unit_cost: it.unit_cost });
      issuesByJob = null;
      add('postings', { kind: 'consumption', job_id: job.id, document_no: job.number, item_no: it.no, lot_no: lot ? lot.lot_no : '',
        qty: q, unit_cost: it.unit_cost, status: 'pending', attempts: 0, error: '', posted_at: null });
      const where = lot ? `lot ${lot.lot_no}${lot.heat_no ? ', heat ' + lot.heat_no : ''}` : (it.lot_tracked ? 'NO LOT' : '');
      log(job, op, 'issue', q, { operator, note: `${it.no} x ${+q.toFixed(4)} ${it.uom}${where ? ` (${where})` : ''}` });
    }
  }

  function complete(job, opId, qty, minutes, operator) {
    const op = getOp(job, opId);
    if (!(qty > 0) || qty > op.qty_in_process + EPS) fail(409, `Can complete between 0 and ${op.qty_in_process} at this stage`);
    if (minutes == null) minutes = op.run_minutes_per_unit * qty;
    op.qty_in_process -= qty; op.qty_done += qty; op.actual_minutes += minutes;
    const idx = job.operations.indexOf(op);
    if (idx + 1 < job.operations.length) job.operations[idx + 1].qty_queued += qty;
    else receiveOutput(job, qty);
    closeOpIfDone(op);
    log(job, op, 'complete', qty, { minutes, operator });
  }

  function receiveOutput(job, qty) {
    const it = item(job.item_id);
    job.qty_good += qty; it.qty_on_hand += qty;
    let lot = S.lots.find(l => l.item_id === job.item_id && l.lot_no === job.number);
    if (!lot) lot = newLot({ item_id: job.item_id, lot_no: job.number, source: 'output', job_id: job.id, qty_received: 0, qty_on_hand: 0 });
    lot.qty_received += qty; lot.qty_on_hand += qty;
    const cost = outputUnitCost(job);
    add('postings', { kind: 'output', job_id: job.id, document_no: job.number, item_no: it.no, lot_no: job.number, qty,
      unit_cost: cost, status: 'pending', attempts: 0, error: '', posted_at: null });
    log(job, null, 'output', qty, { note: `${it.no} x ${qty} to stock @ std ${cost.toFixed(4)}` });
  }

  function scrap(job, opId, qty, fromState, operator, note) {
    const op = getOp(job, opId);
    const attr = { queued: 'qty_queued', in_process: 'qty_in_process' }[fromState] || fail(409, "from_state must be 'queued' or 'in_process'");
    if (!(qty > 0) || qty > op[attr] + EPS) fail(409, `Can scrap between 0 and ${op[attr]}`);
    op[attr] -= qty; op.qty_scrap += qty; job.qty_scrap += qty;
    closeOpIfDone(op);
    log(job, op, 'scrap', qty, { operator, note });
  }

  const closeOpIfDone = op => { if (op.qty_queued <= EPS && op.qty_in_process <= EPS && !op.completed_at) op.completed_at = now(); };
  const unitsRemaining = job => job.operations.reduce((a, o) => a + o.qty_queued + o.qty_in_process, 0);

  function finish(job, operator) {
    if (!['released', 'in_progress'].includes(job.status)) fail(409, `Job is ${job.status}`);
    if (job.operations.some(o => o.qty_in_process > EPS)) fail(409, 'Units are still in process; complete or scrap them first');
    const short = unitsRemaining(job);
    const c = jobCosts(job);
    Object.assign(job, { material_cost: c.material_cost, conversion_cost: c.conversion_cost, unit_cost: c.unit_cost,
      cost_snapshot: c, status: 'completed', completed_at: now() });
    job.operations.forEach(o => { o.qty_queued = 0; });
    if (job.demand_id) { const d = byId('demands', job.demand_id); if (d && d.status === 'open') d.status = 'completed'; }
    let note = `Actual unit cost ${c.unit_cost.toFixed(4)}, variance ${c.variance.total.toFixed(2)}`;
    if (short > EPS) note += `; closed short, ${short} units not started`;
    log(job, null, 'finish', job.qty_good, { operator, note });
  }

  // ------------------------------------------------------------------ trace
  function traceNode(lot, qty, it, job) {
    if (!lot) return { lot_id: null, lot_no: '', heat_no: '', item_no: it.no, description: it.description, uom: it.uom, qty: r4(qty), source: 'unlotted', job: null, children: [] };
    const li = item(lot.item_id);
    return { lot_id: lot.id, lot_no: lot.lot_no, heat_no: lot.heat_no, item_no: li.no, description: li.description, uom: li.uom,
      qty: r4(qty), source: lot.source, supplier: lot.supplier, cert_ref: lot.cert_ref, job: job ? job.number : null, children: [] };
  }
  function backward(lot, qty, depth = 0) {
    const job = lot.job_id ? byId('jobs', lot.job_id) : null;
    const node = traceNode(lot, qty ?? lot.qty_received, null, job);
    if (job && depth < 12) {
      const consumed = new Map();
      for (const i of jobIssues(job)) { const k = `${i.item_id}|${i.lot_id}`; consumed.set(k, (consumed.get(k) || 0) + i.qty); }
      [...consumed.entries()].sort((a, b) => item(+a[0].split('|')[0]).no.localeCompare(item(+b[0].split('|')[0]).no)).forEach(([k, q]) => {
        const [itemId, lotId] = k.split('|');
        node.children.push(lotId !== 'null' ? backward(byId('lots', +lotId), q, depth + 1) : traceNode(null, q, item(+itemId)));
      });
    }
    return node;
  }
  function forward(lot, qty, depth = 0) {
    const job = lot.job_id ? byId('jobs', lot.job_id) : null;
    const node = traceNode(lot, qty ?? lot.qty_received, null, job);
    if (depth >= 12) return node;
    const used = new Map();
    for (const i of S.issues.filter(i => i.lot_id === lot.id)) used.set(i.job_id, (used.get(i.job_id) || 0) + i.qty);
    for (const [jobId, q] of used) {
      const consumer = byId('jobs', jobId), ci = item(consumer.item_id);
      const out = S.lots.find(l => l.item_id === consumer.item_id && l.lot_no === consumer.number);
      const child = out ? forward(out, undefined, depth + 1)
        : { lot_id: null, lot_no: '', heat_no: '', item_no: ci.no, description: ci.description, uom: ci.uom, qty: 0, source: 'wip', job: consumer.number, children: [] };
      child.consumed_qty = r4(q);
      node.children.push(child);
    }
    return node;
  }

  // ------------------------------------------------------------------ purchasing
  const WHOLE = new Set(['EA', 'SHEET', 'LEN', 'PCS', 'CTN', 'BOX']);
  const orderQty = (q, uom) => (WHOLE.has(uom.toUpperCase()) ? Math.ceil(q - EPS) : Math.ceil(q * 10 - EPS) / 10);

  function availability() {
    const plan = Object.fromEntries(buildPlan().lines.map(l => [l.item_id, l]));
    const allocated = {}, onOrder = {};
    for (const j of S.jobs.filter(j => OPEN_JOB.includes(j.status))) for (const [id, q] of Object.entries(jobRemainingComponents(j))) allocated[id] = (allocated[id] || 0) + q;
    for (const pr of S.purchase_requests.filter(p => OPEN_PR.includes(p.status))) for (const l of pr.lines) onOrder[l.item_id] = (onOrder[l.item_id] || 0) + l.qty;
    return S.items.filter(i => i.item_type === 'raw').sort((a, b) => a.no.localeCompare(b.no)).map(it => {
      const line = plan[it.id], gross = line ? line.gross : 0, alloc = allocated[it.id] || 0, oo = onOrder[it.id] || 0;
      const shortage = line && line.action === 'buy' ? line.net : 0, available = it.qty_on_hand - alloc;
      return { item_id: it.id, item_no: it.no, description: it.description, uom: it.uom, unit_cost: it.unit_cost, vendor_no: it.vendor_no,
        vendor_name: it.vendor_name, lead_time_days: it.lead_time_days, on_hand: it.qty_on_hand, allocated: r4(alloc), available: r4(available),
        on_order: oo, planned_need: r4(Math.max(0, gross - alloc)), shortage: r4(shortage), shortage_value: r2(shortage * it.unit_cost),
        needed_by: line ? line.earliest_due : null,
        status: shortage > EPS ? 'short' : (gross > 0 && available + oo < gross * 0.15 + EPS ? 'tight' : 'ok') };
    });
  }

  function prFromShortages(operator) {
    const byVendor = new Map();
    for (const l of buildPlan().lines.filter(l => l.action === 'buy' && l.net > EPS)) {
      const it = item(l.item_id);
      if (!byVendor.has(it.vendor_no)) byVendor.set(it.vendor_no, []);
      byVendor.get(it.vendor_no).push([it, l]);
    }
    return [...byVendor.entries()].sort().map(([vendor, rows]) => {
      const pr = add('purchase_requests', { number: 'PR' + String((S.seq.purchase_requests || 0) + 1).padStart(5, '0'), vendor_no: vendor,
        vendor_name: rows[0][0].vendor_name || 'Vendor not set', status: 'draft', bc_po_no: '', notes: '', created_by: operator || '',
        sent_at: null, received_at: null, lines: [] });
      for (const [it, l] of rows) {
        const needed = l.earliest_due ? isoDate(new Date(Date.parse(l.earliest_due + 'T00:00:00Z') - DAY)) : today(it.lead_time_days);
        pr.lines.push({ id: nextId('purchase_request_lines'), request_id: pr.id, item_id: it.id, qty: orderQty(l.net, it.uom), unit_cost: it.unit_cost, needed_by: needed });
      }
      pr.needed_by = pr.lines.map(l => l.needed_by).sort()[0] || null;
      return pr;
    });
  }

  function prSend(pr) {
    if (pr.status !== 'draft') fail(409, `Request is ${pr.status}; only drafts can be sent`);
    if (!pr.lines.length) fail(409, 'Request has no lines');
    if (!pr.vendor_no) fail(409, 'Set a vendor for these items before sending');
    const po = `PO-${10001 + S.bc.purchase_orders.length}`;
    S.bc.purchase_orders.push({ no: po, vendor_no: pr.vendor_no, reference: pr.number, status: 'Released',
      lines: pr.lines.map(l => ({ item_no: item(l.item_id).no, qty: l.qty, unit_cost: l.unit_cost, needed_by: l.needed_by })) });
    Object.assign(pr, { bc_po_no: po, status: 'sent', sent_at: now() });
  }

  function prReceive(pr, heats) {
    if (pr.status !== 'sent') fail(409, 'Only requests sent to BC can be received');
    const po = S.bc.purchase_orders.find(p => p.no === pr.bc_po_no);
    if (po) {
      po.status = 'Received';
      for (const l of po.lines) { const bi = S.bc.items.find(i => i.no === l.item_no); if (bi) bi.inventory += l.qty; }
    }
    pr.lines.forEach((l, k) => {
      const it = item(l.item_id);
      it.qty_on_hand += l.qty;
      if (it.lot_tracked) newLot({ item_id: it.id, lot_no: `${pr.bc_po_no}-${k + 1}`, heat_no: (heats || {})[l.id] || '', supplier: pr.vendor_name, qty_received: l.qty, qty_on_hand: l.qty });
    });
    Object.assign(pr, { status: 'received', received_at: now() });
  }

  // ------------------------------------------------------------------ reports
  const weekStart = iso => { const d = new Date(iso); const ms = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()); return isoDate(new Date(ms - ((new Date(ms).getUTCDay() + 6) % 7) * DAY)); };
  const ratio = (a, b) => (b > EPS ? a / b : null);
  function metrics(m, capMin) {
    const a = ratio(m.worked, m.worked + m.downtime), p = ratio(m.earned, m.worked), q = ratio(m.earned_good, m.earned);
    const oee = a != null && p != null && q != null ? a * Math.min(p, 1) * q : null;
    const r = x => (x == null ? null : r4(x));
    return { availability: r(a), performance: r(p), quality: r(q), oee: r(oee), output_vs_capacity: r(ratio(m.earned_good, capMin)),
      worked_hours: r2(m.worked / 60), downtime_hours: r2(m.downtime / 60), earned_hours: r2(m.earned_good / 60), capacity_hours: r2(capMin / 60),
      good_units: m.good, scrap_units: m.scrap };
  }
  function trendFit(pts) {
    pts = pts.filter(([, y]) => y != null);
    if (pts.length < 2) return null;
    const n = pts.length, mx = pts.reduce((a, [x]) => a + x, 0) / n, my = pts.reduce((a, [, y]) => a + y, 0) / n;
    const sxx = pts.reduce((a, [x]) => a + (x - mx) ** 2, 0);
    const slope = sxx ? pts.reduce((a, [x, y]) => a + (x - mx) * (y - my), 0) / sxx : 0;
    return { slope: Math.round(slope * 1e5) / 1e5, intercept: Math.round((my - slope * mx) * 1e5) / 1e5 };
  }

  function performanceReport(weeks = 10) {
    const todayIso = today(), thisWeek = weekStart(todayIso + 'T12:00:00Z');
    const keys = []; for (let i = weeks - 1; i >= 0; i--) keys.push(isoDate(new Date(Date.parse(thisWeek + 'T00:00:00Z') - i * 7 * DAY)));
    const since = keys[0] + 'T00:00:00';
    const stages = [...S.stages].sort((a, b) => a.sequence - b.sequence);
    const ops = new Map(); for (const j of S.jobs) for (const o of j.operations) ops.set(o.id, o);
    const blank = () => ({ worked: 0, downtime: 0, earned: 0, earned_good: 0, good: 0, scrap: 0 });
    const cell = new Map(); const get = (sid, wk) => { const k = sid + '|' + wk; if (!cell.has(k)) cell.set(k, blank()); return cell.get(k); };
    for (const e of S.events) {
      if (e.created_at < since || !['start', 'complete', 'scrap'].includes(e.action)) continue;
      const op = ops.get(e.job_operation_id); if (!op) continue;
      const m = get(op.stage_id, weekStart(e.created_at));
      if (e.action === 'start') { m.worked += e.minutes; if (e.minutes) { m.earned += op.setup_minutes; m.earned_good += op.setup_minutes; } }
      else if (e.action === 'complete') { m.worked += e.minutes; m.earned += op.run_minutes_per_unit * e.qty; m.earned_good += op.run_minutes_per_unit * e.qty; m.good += e.qty; }
      else { m.earned += op.run_minutes_per_unit * e.qty; m.scrap += e.qty; }
    }
    const issues = S.issue_logs.filter(i => i.created_at >= since);
    for (const i of issues) if (i.stage_id && i.minutes_lost) get(i.stage_id, weekStart(i.created_at)).downtime += i.minutes_lost;
    const capWeek = Object.fromEntries(stages.map(s => [s.id, s.capacity_hours_per_day * 60 * 5]));
    const capAll = Object.values(capWeek).reduce((a, b) => a + b, 0);
    const daysIn = keys.map(wk => (wk === thisWeek ? Math.min(5, Math.round((Date.parse(todayIso) - Date.parse(wk)) / DAY) + 1) : 5));
    const periodWeeks = daysIn.reduce((a, b) => a + b, 0) / 5;
    const weekly = keys.map((wk, idx) => {
      const tot = blank(); for (const s of stages) for (const [k, v] of Object.entries(get(s.id, wk))) tot[k] += v;
      return { week: wk, index: idx, partial: daysIn[idx] < 5, ...metrics(tot, capAll * daysIn[idx] / 5) };
    });
    const full = weekly.filter(w => !w.partial);
    const byStage = stages.map(s => {
      const tot = blank(); for (const wk of keys) for (const [k, v] of Object.entries(get(s.id, wk))) tot[k] += v;
      return { stage_id: s.id, stage: s.name, code: s.code, color: s.color, rate_per_hour: s.rate_per_hour, ...metrics(tot, capWeek[s.id] * periodWeeks) };
    });
    const overallTot = blank(); for (const s of stages) for (const wk of keys) for (const [k, v] of Object.entries(get(s.id, wk))) overallTot[k] += v;
    const pareto = new Map();
    for (const i of issues) {
      if (!i.minutes_lost || !i.stage_id) continue;
      const st = stage(i.stage_id), k = i.category + '|' + st.name;
      const p = pareto.get(k) || { category: i.category, stage: st.name, minutes: 0, cost: 0, count: 0 };
      p.minutes += i.minutes_lost; p.cost += i.minutes_lost / 60 * st.rate_per_hour; p.count++; pareto.set(k, p);
    }
    const downtime = [...pareto.values()].map(p => ({ category: p.category, stage: p.stage, hours: r2(p.minutes / 60), cost: r2(p.cost), count: p.count })).sort((a, b) => b.cost - a.cost);
    const costs = costReport(since, isoDate(new Date(Date.parse(thisWeek + 'T00:00:00Z') - 21 * DAY)) + 'T00:00:00');
    return { weeks: weekly, trend: trendFit(full.map(w => [w.index, w.output_vs_capacity])), oee_trend: trendFit(full.map(w => [w.index, w.oee])),
      overall: metrics(overallTot, capAll * periodWeeks), by_stage: byStage, downtime, costs,
      issues: { open: S.issue_logs.filter(i => i.status === 'open').length, logged: issues.length },
      opportunities: opportunities(byStage, downtime, costs, periodWeeks || 1) };
  }

  function costReport(since, recentFrom) {
    const prod = new Map(), materials = {}, stageVar = {};
    for (const j of S.jobs.filter(j => j.status === 'completed' && j.completed_at >= since)) {
      const c = j.cost_snapshot; if (!c || j.qty_good <= 0) continue;
      const it = item(j.item_id);
      const p = prod.get(j.item_id) || { item_no: it.no, description: it.description, jobs: 0, units: 0, actual: 0, bc_value: 0, std: 0, recipe_std: 0, material: 0, conversion: 0, scrap: 0, last4: [0, 0], prev: [0, 0] };
      p.jobs++; p.units += j.qty_good; p.scrap += j.qty_scrap; p.actual += c.total_cost; p.bc_value += c.bc_value;
      p.std = c.output_unit_cost; p.recipe_std = c.std_unit_cost; p.material += c.variance.material_usage; p.conversion += c.variance.conversion;
      const b = j.completed_at >= recentFrom ? p.last4 : p.prev; b[0] += c.total_cost; b[1] += j.qty_good;
      for (const m of c.materials) materials[m.item_no] = (materials[m.item_no] || 0) + m.variance;
      for (const s of c.stages) stageVar[s.stage] = (stageVar[s.stage] || 0) + s.variance;
      prod.set(j.item_id, p);
    }
    const rows = [...prod.values()].map(p => { const avg = p.actual / p.units;
      return { item_no: p.item_no, description: p.description, jobs: p.jobs, units: p.units, scrap: p.scrap, avg_unit_cost: r2(avg),
        bc_standard: r2(p.std), recipe_standard: r2(p.recipe_std), variance_pct: p.std ? r4((avg - p.std) / p.std) : null,
        variance: r2(p.actual - p.bc_value), material_variance: r2(p.material), conversion_variance: r2(p.conversion),
        recent_avg: p.last4[1] ? r2(p.last4[0] / p.last4[1]) : null, earlier_avg: p.prev[1] ? r2(p.prev[0] / p.prev[1]) : null }; })
      .sort((a, b) => a.item_no.localeCompare(b.item_no));
    const sorted = o => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, r2(v)]).sort((a, b) => b[1] - a[1]));
    return { products: rows, total_variance: r2(rows.reduce((a, r) => a + r.variance, 0)),
      material_variance_by_item: sorted(materials), conversion_variance_by_stage: sorted(stageVar) };
  }

  function opportunities(byStage, downtime, costs, weeks) {
    const ops = [], annual = 52 / weeks;
    const add = (area, title, detail, impact) => { if (impact > 1) ops.push({ area, title, detail, impact: r2(impact), annualised: Math.round(impact * annual) }); };
    const pct = x => Math.round(x * 100) + '%';
    for (const [st, v] of Object.entries(costs.conversion_variance_by_stage)) {
      const s = byStage.find(x => x.stage === st), perf = s ? s.performance : null;
      if (v > 0) add('Labour', `${st} runs slower than standard`, perf ? `Performance ${pct(perf)} of standard time. Review method, fixtures and the standard minutes in the recipe.` : 'Review method and the standard minutes in the recipe.', v);
    }
    for (const d of downtime.slice(0, 4)) add('Downtime', `${d.category[0].toUpperCase() + d.category.slice(1)} at ${d.stage}`, `${d.hours.toFixed(1)} h lost across ${d.count} logged issues. Target the root cause on the Issues log.`, d.cost);
    for (const [no, v] of Object.entries(costs.material_variance_by_item).slice(0, 3)) if (v > 0) add('Material', `${no} used above standard`, 'Scrap and over-use against the recipe. Check nesting, cut lists and scrap reasons.', v);
    for (const p of costs.products) {
      if (p.bc_standard && Math.abs(p.recipe_standard - p.bc_standard) / p.bc_standard > 0.02) {
        add('Standards', `Update BC standard cost for ${p.item_no}`, `Recipe works out at ${p.recipe_standard.toFixed(2)} vs BC ${p.bc_standard.toFixed(2)}. Inventory and margins are misstated until BC is updated.`, Math.abs((p.recipe_standard - p.bc_standard) * p.units));
      }
    }
    const oees = byStage.filter(s => s.oee != null);
    if (oees.length) {
      const avg = oees.reduce((a, s) => a + s.oee, 0) / oees.length, worst = oees.reduce((a, s) => (s.oee < a.oee ? s : a));
      if (worst.oee < avg - 0.05) add('OEE', `Lift ${worst.stage} OEE to the shop average`, `${worst.stage} OEE ${pct(worst.oee)} vs ${pct(avg)} average (availability ${pct(worst.availability)}, performance ${pct(Math.min(worst.performance, 1))}, quality ${pct(worst.quality)}).`, (avg - worst.oee) * worst.worked_hours * worst.rate_per_hour);
    }
    return ops.sort((a, b) => b.impact - a.impact);
  }

  // ------------------------------------------------------------------ costing assumptions
  const ELEMENTS = ['labour', 'electricity', 'gas', 'consumables', 'maintenance', 'depreciation', 'overhead'];
  const DEFAULT_SETTINGS = { electricity_per_kwh: 0.28, working_days_per_week: 5, working_weeks_per_year: 48 };
  const SETTING_LABELS = { electricity_per_kwh: 'Electricity price ($ per kWh)', working_days_per_week: 'Working days per week', working_weeks_per_year: 'Working weeks per year' };
  const ASSUMPTION_FIELDS = ['crew_size', 'labour_rate', 'machine_kw', 'power_load_pct', 'gas_per_hour', 'consumables_per_hour',
    'maintenance_per_hour', 'machine_cost', 'machine_life_years', 'overhead_per_hour', 'capacity_hours_per_day', 'assumption_notes'];
  const settings = () => ({ ...DEFAULT_SETTINGS, ...(S.settings || {}) });
  const annualHours = (s, st) => s.capacity_hours_per_day * st.working_days_per_week * st.working_weeks_per_year;
  function rateBreakdown(s, st) {
    const v = k => s[k] || 0, hours = annualHours(s, st);
    return { labour: v('crew_size') * v('labour_rate'), electricity: v('machine_kw') * v('power_load_pct') / 100 * st.electricity_per_kwh,
      gas: v('gas_per_hour'), consumables: v('consumables_per_hour'), maintenance: v('maintenance_per_hour'),
      depreciation: v('machine_cost') && v('machine_life_years') && hours ? v('machine_cost') / v('machine_life_years') / hours : 0,
      overhead: v('overhead_per_hour') };
  }
  const stageElements = (s, st) => (s.rate_from_assumptions ? rateBreakdown(s, st) : { ...Object.fromEntries(ELEMENTS.map(e => [e, 0])), overhead: s.rate_per_hour });
  const sumVals = o => Object.values(o).reduce((a, b) => a + b, 0);
  function recomputeRates() { const st = settings(); for (const s of S.stages) if (s.rate_from_assumptions) s.rate_per_hour = r4(sumVals(rateBreakdown(s, st))); }
  function standardBreakdown(r, active, st) {
    const out = { material: 0, ...Object.fromEntries(ELEMENTS.map(e => [e, 0])) };
    if (!r.output_qty) return out;
    for (const c of r.components) out.material += c.qty_per / r.output_qty * (1 + c.scrap_pct / 100) * componentStdCost(item(c.item_id), active, [r.id]);
    const batch = r.std_batch_qty || r.output_qty;
    for (const o of r.operations) {
      const hours = (o.setup_minutes / batch + o.run_minutes_per_unit) / 60;
      for (const [k, v] of Object.entries(stageElements(stage(o.stage_id), st))) out[k] += hours * v;
    }
    return Object.fromEntries(Object.entries(out).map(([k, v]) => [k, r4(v)]));
  }
  function assumptionSummary() {
    const st = settings();
    return { settings: st, labels: SETTING_LABELS, elements: ELEMENTS,
      stages: [...S.stages].sort((a, b) => a.sequence - b.sequence).map(s => {
        const br = stageElements(s, st), hours = annualHours(s, st);
        return { id: s.id, code: s.code, name: s.name, color: s.color, sequence: s.sequence, rate_from_assumptions: !!s.rate_from_assumptions,
          rate_per_hour: s.rate_per_hour, ...Object.fromEntries(ASSUMPTION_FIELDS.map(k => [k, s[k] ?? (k === 'assumption_notes' ? '' : 0)])),
          breakdown: Object.fromEntries(Object.entries(br).map(([k, v]) => [k, r4(v)])), annual_hours: Math.round(hours * 10) / 10,
          annual_cost_at_capacity: Math.round(s.rate_per_hour * hours), electricity_kwh_per_hour: r4((s.machine_kw || 0) * (s.power_load_pct || 0) / 100) };
      }) };
  }

  // ------------------------------------------------------------------ mock BC
  function syncItems() {
    const pending = {};
    for (const p of S.postings.filter(p => p.status !== 'posted')) pending[p.item_no] = (pending[p.item_no] || 0) + (p.kind === 'consumption' ? -p.qty : p.qty);
    let created = 0, updated = 0;
    for (const bi of S.bc.items) {
      let it = S.items.find(i => i.no === bi.no);
      if (!it) { it = add('items', { no: bi.no, item_type: { 'FG-': 'finished', 'SA-': 'sub' }[bi.no.slice(0, 3)] || 'raw', lot_tracked: false, unit_cost: 0, vendor_no: '', vendor_name: '', lead_time_days: 7 }); created++; } else updated++;
      it.description = bi.description; it.uom = bi.uom;
      if (bi.unit_cost || !it.unit_cost) it.unit_cost = bi.unit_cost;
      if (bi.vendor_no) Object.assign(it, { vendor_no: bi.vendor_no, vendor_name: bi.vendor_name, lead_time_days: bi.lead_time_days });
      it.qty_on_hand = bi.inventory + (pending[bi.no] || 0);
      it.bc_synced_at = now();
    }
    return { created, updated };
  }
  function syncDemand() {
    // new customer orders arrive with each pull (two at a time)
    for (let k = 0; k < 2 && S.bc.incoming.length; k++) {
      const o = S.bc.incoming.shift();
      if ('due_in_days' in o) { o.due_date = today(o.due_in_days); delete o.due_in_days; }
      S.bc.demand.push(o);
    }
    let created = 0, updated = 0, skipped = 0, closed = 0; const seen = new Set();
    for (const line of S.bc.demand) {
      const it = S.items.find(i => i.no === line.item_no);
      if (!it) { skipped++; continue; }
      seen.add(line.reference);
      const d = S.demands.find(x => x.source === 'bc' && x.reference === line.reference);
      const due = line.due_date || today(line.due_in_days ?? 14);
      if (d) { if (d.status === 'open') { Object.assign(d, { qty: line.qty, due_date: due, item_id: it.id, customer: line.customer || d.customer }); updated++; } }
      else { add('demands', { item_id: it.id, qty: line.qty, due_date: due, source: 'bc', reference: line.reference, customer: line.customer || '', status: 'open' }); created++; }
    }
    for (const d of S.demands.filter(d => d.source === 'bc' && d.status === 'open' && !seen.has(d.reference))) { d.status = 'cancelled'; closed++; }
    return { created, updated, closed, skipped_unknown_items: skipped };
  }
  function processOutbox() {
    let posted = 0, failed = 0;
    for (const p of S.postings.filter(p => p.status === 'pending' || p.status === 'failed')) {
      p.attempts++;
      const bi = S.bc.items.find(i => i.no === p.item_no);
      if (!bi) { p.status = 'failed'; p.error = `Item ${p.item_no} not found in BC`; failed++; continue; }
      const sign = p.kind === 'consumption' ? -1 : 1;
      bi.inventory += sign * p.qty;
      p.status = 'posted'; p.error = ''; p.posted_at = now(); posted++;
    }
    return { posted, failed };
  }

  // ------------------------------------------------------------------ serializers
  const ser = {
    item: i => ({ id: i.id, no: i.no, description: i.description, item_type: i.item_type, uom: i.uom, unit_cost: i.unit_cost,
      qty_on_hand: i.qty_on_hand, lot_tracked: !!i.lot_tracked, vendor_no: i.vendor_no || '', vendor_name: i.vendor_name || '',
      lead_time_days: i.lead_time_days ?? 7, bc_synced_at: i.bc_synced_at || null }),
    stage: s => ({ ...s }),
    recipe: r => {
      const it = item(r.item_id);
      return { id: r.id, item_id: r.item_id, item_no: it.no, item_description: it.description, version: r.version, output_qty: r.output_qty,
        std_batch_qty: r.std_batch_qty, active: r.active, notes: r.notes,
        operations: [...r.operations].sort((a, b) => a.sequence - b.sequence).map(o => ({ ...o, stage_code: stage(o.stage_id).code, stage_name: stage(o.stage_id).name })),
        components: r.components.map(c => { const ci = item(c.item_id); return { ...c, item_no: ci.no, description: ci.description, uom: ci.uom, unit_cost: ci.unit_cost }; }),
        standard_unit_cost: r4(standardUnitCost(r, activeRecipes())), standard_breakdown: standardBreakdown(r, activeRecipes(), settings()) };
    },
    demand: d => ({ ...d, item_no: item(d.item_id).no, description: item(d.item_id).description }),
    job: (j, detail) => {
      const it = item(j.item_id);
      const out = { id: j.id, number: j.number, item_id: j.item_id, item_no: it.no, recipe_id: j.recipe_id, description: it.description,
        qty: j.qty, status: j.status, due_date: j.due_date, demand_id: j.demand_id, qty_good: j.qty_good, qty_scrap: j.qty_scrap,
        created_at: j.created_at, completed_at: j.completed_at,
        costs: j.status === 'completed' && j.cost_snapshot ? j.cost_snapshot : jobCosts(j),
        operations: j.operations.map(o => { const s = stage(o.stage_id); return { ...o, stage_code: s.code, stage_name: s.name, color: s.color, actual_minutes: r2(o.actual_minutes) }; }) };
      if (detail) {
        out.issues = jobIssues(j).map(i => { const ii = item(i.item_id), l = i.lot_id ? byId('lots', i.lot_id) : null;
          return { item_no: ii.no, description: ii.description, qty: i.qty, lot_id: i.lot_id, lot_no: l ? l.lot_no : '', heat_no: l ? l.heat_no : '',
            uom: ii.uom, unit_cost: i.unit_cost, value: r4(i.qty * i.unit_cost), job_operation_id: i.job_operation_id, created_at: i.created_at }; });
      }
      return out;
    },
    lot: l => { const it = item(l.item_id); return { ...l, item_no: it.no, description: it.description, uom: it.uom }; },
    issueLog: i => { const j = i.job_id ? byId('jobs', i.job_id) : null;
      return { ...i, job: j ? j.number : null, item_no: j ? item(j.item_id).no : null, stage: i.stage_id ? stage(i.stage_id).name : null }; },
    note: n => ({ ...n, stage: n.stage_id ? stage(n.stage_id).name : null }),
    pr: pr => { const lines = pr.lines.map(l => { const it = item(l.item_id);
        return { id: l.id, item_id: l.item_id, item_no: it.no, description: it.description, uom: it.uom, qty: l.qty, unit_cost: l.unit_cost,
          value: r2(l.qty * l.unit_cost), needed_by: l.needed_by, lot_tracked: !!it.lot_tracked }; });
      return { ...pr, lines, value: r2(lines.reduce((a, l) => a + l.value, 0)) }; },
  };

  // ------------------------------------------------------------------ routes
  function getJob(id) { return byId('jobs', id) || fail(404, 'Job not found'); }
  const num = v => (v === '' || v == null ? null : Number(v));
  const getPr = id => byId('purchase_requests', id) || fail(404, 'Purchase request not found');

  function route(method, path, q, body) {
    let m;
    if (method === 'GET' && path === '/items') return [...S.items].sort((a, b) => a.no.localeCompare(b.no)).map(ser.item);
    if (method === 'POST' && path === '/items') {
      if (!body.no) fail(422, 'Item number is required');
      if (S.items.some(i => i.no === body.no)) fail(409, 'Item number already exists');
      return ser.item(add('items', { no: body.no, description: body.description || '', item_type: body.item_type || 'raw', uom: body.uom || 'EA',
        unit_cost: +body.unit_cost || 0, qty_on_hand: +body.qty_on_hand || 0, lot_tracked: !!body.lot_tracked, vendor_no: '', vendor_name: '', lead_time_days: 7 }));
    }
    if (method === 'PATCH' && (m = path.match(/^\/items\/(\d+)$/))) {
      const it = item(+m[1]) || fail(404, 'Not found');
      for (const k of ['description', 'item_type', 'uom', 'unit_cost', 'lot_tracked', 'vendor_no', 'vendor_name', 'lead_time_days']) if (k in body) it[k] = body[k];
      return ser.item(it);
    }
    if (method === 'GET' && path === '/stages') return [...S.stages].sort((a, b) => a.sequence - b.sequence).map(ser.stage);
    if (method === 'POST' && path === '/stages') return ser.stage(add('stages', { code: body.code, name: body.name, sequence: +body.sequence || 10, rate_per_hour: +body.rate_per_hour || 0, capacity_hours_per_day: +body.capacity_hours_per_day || 7.6, color: body.color || '#64748b' }));
    if (method === 'PATCH' && (m = path.match(/^\/stages\/(\d+)$/))) {
      const s = stage(+m[1]) || fail(404, 'Not found');
      for (const k of ['name', 'sequence', 'rate_per_hour', 'capacity_hours_per_day', 'color']) if (k in body) s[k] = body[k];
      if ('rate_per_hour' in body) s.rate_from_assumptions = false; else if (s.rate_from_assumptions) recomputeRates();
      return ser.stage(s);
    }
    if (method === 'GET' && path === '/assumptions') return assumptionSummary();
    if (method === 'PATCH' && path === '/assumptions/settings') {
      for (const [k, v] of Object.entries(body)) { if (!(k in DEFAULT_SETTINGS)) fail(422, `Unknown setting ${k}`); S.settings = { ...settings(), [k]: +v }; }
      recomputeRates();
      return assumptionSummary();
    }
    if (method === 'PATCH' && (m = path.match(/^\/stages\/(\d+)\/assumptions$/))) {
      const s = stage(+m[1]) || fail(404, 'Not found');
      for (const [k, v] of Object.entries(body)) {
        if (!ASSUMPTION_FIELDS.includes(k)) fail(422, `Unknown assumption ${k}`);
        if (k !== 'assumption_notes' && (typeof v !== 'number' || v < 0)) fail(422, `${k} must be a number of 0 or more`);
        s[k] = v;
      }
      if (Object.keys(body).some(k => !['capacity_hours_per_day', 'assumption_notes'].includes(k))) s.rate_from_assumptions = true;
      if (s.rate_from_assumptions) s.rate_per_hour = r4(sumVals(rateBreakdown(s, settings())));
      return assumptionSummary();
    }
    if (method === 'GET' && path === '/recipes') return S.recipes.map(ser.recipe);
    if (method === 'GET' && (m = path.match(/^\/recipes\/(\d+)$/))) return ser.recipe(byId('recipes', +m[1]) || fail(404, 'Not found'));
    if ((method === 'POST' && path === '/recipes') || (method === 'PUT' && (m = path.match(/^\/recipes\/(\d+)$/)))) {
      if (!body.operations?.length) fail(422, 'A recipe needs at least one operation (stage)');
      if (!(body.output_qty > 0)) fail(422, 'Output qty must be more than 0');
      const seqs = new Set(body.operations.map(o => o.sequence));
      for (const c of body.components) {
        if (!(c.qty_per > 0)) fail(422, 'Every component needs a quantity above 0');
        if (c.operation_seq != null && !seqs.has(c.operation_seq)) fail(422, `Component consumed at operation ${c.operation_seq}, which doesn't exist`);
        if (c.item_id === body.item_id) fail(422, "A recipe can't consume its own output");
      }
      const r = m ? byId('recipes', +m[1]) || fail(404, 'Not found') : add('recipes', {});
      Object.assign(r, { item_id: body.item_id, version: body.version || '1', output_qty: body.output_qty, std_batch_qty: body.std_batch_qty ?? null,
        active: body.active !== false, notes: body.notes || '',
        operations: body.operations.map(o => ({ id: nextId('recipe_operations'), recipe_id: r.id, ...o })),
        components: body.components.map(c => ({ id: nextId('recipe_components'), recipe_id: r.id, scrap_pct: 0, operation_seq: null, ...c })) });
      if (r.active) S.recipes.filter(o => o.item_id === r.item_id && o.id !== r.id).forEach(o => { o.active = false; });
      lowLevelCodes(activeRecipes());
      const it = item(r.item_id); if (it.item_type === 'raw') it.item_type = 'sub';
      return ser.recipe(r);
    }
    if (method === 'GET' && path === '/demand') {
      const status = q.get('status') ?? 'open';
      return S.demands.filter(d => !status || d.status === status)
        .sort((a, b) => (a.due_date == null) - (b.due_date == null) || (a.due_date || '').localeCompare(b.due_date || '') || a.id - b.id).map(ser.demand);
    }
    if (method === 'POST' && path === '/demand') {
      if (!item(body.item_id)) fail(404, 'Item not found');
      if (!(body.qty > 0)) fail(422, 'Quantity must be more than 0');
      return ser.demand(add('demands', { item_id: body.item_id, qty: body.qty, due_date: body.due_date || null, source: 'manual', reference: body.reference || '', customer: '', status: 'open' }));
    }
    if (method === 'POST' && (m = path.match(/^\/demand\/(\d+)\/cancel$/))) { const d = byId('demands', +m[1]) || fail(404, 'Demand not found'); d.status = 'cancelled'; return ser.demand(d); }
    if (method === 'POST' && (m = path.match(/^\/demand\/(\d+)\/create-job$/))) {
      const d = byId('demands', +m[1]) || fail(404, 'Demand not found');
      return ser.job(createJob(d.item_id, d.qty, d.due_date, d.id));
    }
    if (method === 'GET' && path === '/plan') return buildPlan();
    if (method === 'POST' && path === '/plan/create-jobs') return buildPlan().lines.filter(l => l.action === 'make').map(l => ser.job(createJob(l.item_id, l.net, l.earliest_due)));

    if (method === 'GET' && path === '/jobs') {
      const st = q.get('status');
      return S.jobs.filter(j => !st || st.split(',').includes(j.status)).sort((a, b) => b.id - a.id).map(j => ser.job(j));
    }
    if (method === 'POST' && path === '/jobs') return ser.job(createJob(body.item_id, body.qty, body.due_date || null), true);
    if (method === 'GET' && (m = path.match(/^\/jobs\/(\d+)$/))) {
      const j = getJob(+m[1]); const out = ser.job(j, true);
      out.events = S.events.filter(e => e.job_id === j.id).sort(byTimeDesc)
        .map(e => ({ action: e.action, qty: e.qty, minutes: e.minutes, operator: e.operator, note: e.note, stage: e.stage_id ? stage(e.stage_id).name : null, created_at: e.created_at }));
      out.notes = S.notes.filter(n => n.job_id === j.id).sort(byTimeDesc).map(ser.note);
      out.issue_logs = S.issue_logs.filter(i => i.job_id === j.id).sort(byTimeDesc).map(ser.issueLog);
      return out;
    }
    if ((m = path.match(/^\/jobs\/(\d+)\/notes$/))) {
      const j = getJob(+m[1]);
      if (method === 'GET') return S.notes.filter(n => n.job_id === j.id).sort(byTimeDesc).map(ser.note);
      if (!body.note || !body.note.trim()) fail(422, 'Write a short note first');
      const op = body.operation_id ? getOp(j, body.operation_id) : null;
      return ser.note(add('notes', { job_id: j.id, job_operation_id: op ? op.id : null, stage_id: op ? op.stage_id : null,
        note: body.note.slice(0, 300), detail: body.detail || '', operator: body.operator || '' }));
    }
    if (method === 'POST' && (m = path.match(/^\/jobs\/(\d+)\/(release|start|complete|scrap|issue|finish|cancel)$/))) {
      const j = getJob(+m[1]), act = m[2], who = body.operator ?? q.get('operator') ?? '';
      if (act === 'release') release(j, who);
      if (act === 'start') start(j, body.operation_id, +body.qty, who, !!body.force,
        Object.fromEntries(Object.entries(body.lot_choices || {}).map(([k, v]) => [+k, v])));
      if (act === 'complete') complete(j, body.operation_id, +body.qty, num(body.minutes), who);
      if (act === 'scrap') scrap(j, body.operation_id, +body.qty, body.from_state || 'in_process', who, body.note || '');
      if (act === 'issue') {
        if (!['released', 'in_progress'].includes(j.status)) fail(409, `Job is ${j.status}`);
        issueMaterial(j, item(body.item_id) || fail(404, 'Item not found'), +body.qty, body.operation_id ? getOp(j, body.operation_id) : null, who, body.lot_id);
      }
      if (act === 'finish') finish(j, who);
      if (act === 'cancel') { if (j.status !== 'planned') fail(409, 'Only planned jobs can be cancelled'); j.status = 'cancelled'; log(j, null, 'cancel', j.qty); }
      return ser.job(j, true);
    }
    if (method === 'GET' && path === '/wip') {
      const board = new Map([...S.stages].sort((a, b) => a.sequence - b.sequence).map(s => [s.id, { ...s, queued: 0, in_process: 0, jobs: [] }]));
      const jobs = S.jobs.filter(j => ['released', 'in_progress'].includes(j.status));
      const openIssues = new Map(); for (const i of S.issue_logs) if (i.status === 'open' && i.job_operation_id) openIssues.set(i.job_operation_id, (openIssues.get(i.job_operation_id) || 0) + 1);
      const noteCount = new Map(); for (const n of S.notes) noteCount.set(n.job_id, (noteCount.get(n.job_id) || 0) + 1);
      for (const j of jobs) {
        for (const op of j.operations) {
          if (op.qty_queued <= EPS && op.qty_in_process <= EPS) continue;
          const col = board.get(op.stage_id); col.queued += op.qty_queued; col.in_process += op.qty_in_process;
          const mats = new Map();
          for (const i of jobIssues(j).filter(i => i.job_operation_id === op.id)) {
            const k = `${i.item_id}|${i.lot_id}`, ii = item(i.item_id), l = i.lot_id ? byId('lots', i.lot_id) : null;
            const row = mats.get(k) || { item_no: ii.no, description: ii.description, qty: 0, uom: ii.uom, lot_no: l ? l.lot_no : '', heat_no: l ? l.heat_no : '' };
            row.qty += i.qty; mats.set(k, row);
          }
          col.jobs.push({ job_id: j.id, number: j.number, item_no: item(j.item_id).no, description: item(j.item_id).description, job_qty: j.qty,
            due_date: j.due_date, operation_id: op.id, sequence: op.sequence, is_last: op === j.operations[j.operations.length - 1],
            qty_queued: op.qty_queued, qty_in_process: op.qty_in_process, qty_done: op.qty_done, qty_scrap: op.qty_scrap, materials: [...mats.values()],
            open_issues: openIssues.get(op.id) || 0, notes: noteCount.get(j.id) || 0 });
        }
      }
      return { stages: [...board.values()], awaiting_finish: jobs.filter(j => j.status === 'in_progress' && unitsRemaining(j) <= EPS)
        .map(j => ({ job_id: j.id, number: j.number, item_no: item(j.item_id).no, qty_good: j.qty_good, qty_scrap: j.qty_scrap })) };
    }
    if (method === 'GET' && path === '/wip/value') {
      const rows = S.jobs.filter(j => ['released', 'in_progress'].includes(j.status)).map(j => { const c = jobCosts(j);
        return { number: j.number, item_no: item(j.item_id).no, total_cost: c.total_cost, output_value: c.bc_value, wip: r4(c.total_cost - c.bc_value) }; });
      return { jobs: rows, total: r2(rows.reduce((a, r) => a + r.wip, 0)) };
    }
    if (method === 'GET' && path === '/events') {
      return [...S.events].sort(byTimeDesc).slice(0, +(q.get('limit') || 50)).map(e => { const j = byId('jobs', e.job_id);
        return { job: j.number, item_no: item(j.item_id).no, action: e.action, qty: e.qty, stage: e.stage_id ? stage(e.stage_id).name : null, operator: e.operator, note: e.note, created_at: e.created_at }; });
    }
    // issue log
    if (method === 'GET' && path === '/issues') {
      const st = q.get('status'), sid = num(q.get('stage_id')), jid = num(q.get('job_id')), cat = q.get('category');
      return S.issue_logs.filter(i => (!st || i.status === st) && (!sid || i.stage_id === sid) && (!jid || i.job_id === jid) && (!cat || i.category === cat))
        .sort(byTimeDesc).slice(0, +(q.get('limit') || 300)).map(ser.issueLog);
    }
    if (method === 'POST' && path === '/issues') {
      if (!body.title || !body.title.trim()) fail(422, 'Say what happened first');
      let stageId = body.stage_id || null;
      if (body.job_id) { const j = getJob(body.job_id); if (body.operation_id) stageId = getOp(j, body.operation_id).stage_id; }
      return ser.issueLog(add('issue_logs', { number: 'ISS' + String((S.seq.issue_logs || 0) + 1).padStart(5, '0'), job_id: body.job_id || null,
        job_operation_id: body.operation_id || null, stage_id: stageId, category: body.category || 'other', severity: body.severity || 'medium',
        title: body.title.slice(0, 200), detail: body.detail || '', minutes_lost: +body.minutes_lost || 0, qty_affected: +body.qty_affected || 0,
        operator: body.operator || '', status: 'open', resolution: '', resolved_at: null }));
    }
    if (method === 'POST' && (m = path.match(/^\/issues\/(\d+)\/resolve$/))) {
      const i = byId('issue_logs', +m[1]) || fail(404, 'Issue not found');
      Object.assign(i, { status: 'resolved', resolution: body.resolution || '', resolved_at: now() });
      return ser.issueLog(i);
    }
    // purchasing
    if (method === 'GET' && path === '/materials/availability') return availability();
    if (method === 'GET' && path === '/purchase-requests') {
      const st = q.get('status');
      return S.purchase_requests.filter(p => !st || st.split(',').includes(p.status)).sort((a, b) => b.id - a.id).map(ser.pr);
    }
    if (method === 'POST' && path === '/purchase-requests/from-shortages') return prFromShortages(q.get('operator')).map(ser.pr);
    if (method === 'POST' && (m = path.match(/^\/purchase-requests\/(\d+)\/(line|send|receive|cancel)$/))) {
      const pr = getPr(+m[1]);
      if (m[2] === 'line') {
        if (pr.status !== 'draft') fail(409, 'Only draft requests can be changed');
        const l = pr.lines.find(x => x.id === body.line_id) || fail(409, 'Line not found');
        if (+body.qty <= 0) pr.lines = pr.lines.filter(x => x !== l); else l.qty = +body.qty;
      }
      if (m[2] === 'send') prSend(pr);
      if (m[2] === 'receive') prReceive(pr, body.heat_numbers);
      if (m[2] === 'cancel') { if (pr.status !== 'draft') fail(409, 'Only drafts can be cancelled here; cancel a sent PO in BC'); pr.status = 'cancelled'; }
      return ser.pr(pr);
    }
    if (method === 'GET' && path === '/reports/performance') return performanceReport(Math.max(2, Math.min(+(q.get('weeks') || 10), 52)));
    // lots
    if (method === 'GET' && path === '/lots') {
      const term = (q.get('q') || '').toLowerCase(), itemId = num(q.get('item_id')), avail = q.get('available') === 'true';
      return S.lots.filter(l => {
        const it = item(l.item_id);
        if (term && ![l.lot_no, l.heat_no, it.no, l.cert_ref, l.supplier].some(v => (v || '').toLowerCase().includes(term))) return false;
        if (itemId && l.item_id !== itemId) return false;
        return !avail || l.qty_on_hand > EPS;
      }).sort(byTimeDesc).slice(0, +(q.get('limit') || 200)).map(ser.lot);
    }
    if (method === 'POST' && path === '/lots') {
      if (!item(body.item_id)) fail(404, 'Item not found');
      if (!body.lot_no) fail(422, 'Lot number is required');
      if (!(body.qty > 0)) fail(422, 'Quantity must be more than 0');
      if (S.lots.some(l => l.item_id === body.item_id && l.lot_no === body.lot_no)) fail(409, 'That lot number already exists for this item');
      return ser.lot(newLot({ item_id: body.item_id, lot_no: body.lot_no, heat_no: body.heat_no || '', supplier: body.supplier || '',
        cert_ref: body.cert_ref || '', notes: body.notes || '', qty_received: body.qty, qty_on_hand: body.qty }));
    }
    if (method === 'GET' && (m = path.match(/^\/lots\/(\d+)\/trace$/))) {
      const l = byId('lots', +m[1]) || fail(404, 'Lot not found');
      return { lot: ser.lot(l), backward: backward(l), forward: forward(l) };
    }
    // BC
    if (method === 'GET' && path === '/bc/status') {
      const postings = {}; for (const p of S.postings) postings[p.status] = (postings[p.status] || 0) + 1;
      return { mode: 'mock', postings };
    }
    if (method === 'POST' && path === '/bc/sync-items') return syncItems();
    if (method === 'POST' && path === '/bc/sync-demand') return syncDemand();
    if (method === 'POST' && path === '/bc/process-postings') return processOutbox();
    if (method === 'GET' && path === '/bc/postings') {
      return [...S.postings].sort((a, b) => b.id - a.id).slice(0, 200).map(p => ({ id: p.id, kind: p.kind, document_no: p.document_no, item_no: p.item_no,
        lot_no: p.lot_no, qty: p.qty, unit_cost: p.unit_cost, status: p.status, attempts: p.attempts, error: p.error, created_at: p.created_at, posted_at: p.posted_at }));
    }
    fail(404, 'Not found');
  }

  // ------------------------------------------------------------------ wiring
  load();
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    if (!url.startsWith('/api/')) return realFetch(input, init);
    const u = new URL(url, 'http://demo.local');
    const method = (init.method || 'GET').toUpperCase();
    const body = init.body ? JSON.parse(init.body) : {};
    const snapshot = method === 'GET' ? null : JSON.stringify(S);
    issuesByJob = null;
    try {
      const data = route(method, u.pathname.slice(4), u.searchParams, body);
      if (method !== 'GET') save();
      return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
    } catch (e) {
      if (snapshot) S = JSON.parse(snapshot); // roll back, like a failed DB transaction
      issuesByJob = null;
      if (!(e instanceof ApiError)) console.error(e);
      const status = e instanceof ApiError ? e.status : 500;
      return new Response(JSON.stringify({ detail: e instanceof ApiError ? e.detail : String(e.message || e) }), { status, headers: { 'Content-Type': 'application/json' } });
    }
  };
  // No server to stream from: the UI refreshes itself after each action.
  window.EventSource = class { constructor() { setTimeout(() => this.onopen && this.onopen(), 0); } close() {} };
  window.mfgDemoReset = () => { try { localStorage.removeItem(KEY); } catch { /* ignore */ } seed(); };
})();
