/*
 * In-browser stand-in for the Python API, used only by the phone demo
 * (demo/build.py). It ports the server logic in app/ (planning, production,
 * costing, lots/trace, mock BC) so the unchanged web UI can run without a
 * server. The Python code in app/ is the real implementation; keep this in
 * step when behaviour there changes. Data persists in localStorage.
 */
'use strict';
(function () {
  const EPS = 1e-9;
  const KEY = 'mfgcost-demo-v1';
  const r4 = x => Math.round(x * 1e4) / 1e4;
  const now = () => new Date().toISOString();
  const today = (days = 0) => { const d = new Date(); d.setDate(d.getDate() + days); return d.toISOString().slice(0, 10); };

  class ApiError extends Error {
    constructor(status, detail) { super(typeof detail === 'string' ? detail : detail.message); this.status = status; this.detail = detail; }
  }
  const fail = (status, detail) => { throw new ApiError(status, detail); };

  // ------------------------------------------------------------------ state
  let S;
  const nextId = t => (S.seq[t] = (S.seq[t] || 0) + 1);
  const add = (t, row) => { row.id = nextId(t); S[t].push(row); return row; };
  const byId = (t, id) => S[t].find(r => r.id === id);
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch { /* storage unavailable */ } };

  const MOCK_BC = {
    items: [
      { no: 'RM-STL-3MM', description: 'Steel sheet 3mm 2400x1200', uom: 'SHEET', unit_cost: 185.00, inventory: 40 },
      { no: 'RM-TUBE-50', description: 'RHS tube 50x50x3 6.5m', uom: 'LEN', unit_cost: 72.50, inventory: 60 },
      { no: 'RM-WIRE-MIG', description: 'MIG wire 0.9mm', uom: 'KG', unit_cost: 9.80, inventory: 30 },
      { no: 'RM-PWD-BLK', description: 'Powder coat satin black', uom: 'KG', unit_cost: 21.00, inventory: 25 },
      { no: 'RM-BOLT-M10', description: 'Bolt M10x30 zinc', uom: 'EA', unit_cost: 0.45, inventory: 2000 },
      { no: 'SA-BRKT-01', description: 'Pressed bracket (sub-assembly)', uom: 'EA', unit_cost: 15.50, inventory: 0 },
      { no: 'FG-FRAME-01', description: 'Welded frame, powder coated black', uom: 'EA', unit_cost: 245.00, inventory: 0 },
    ],
    demand: [{ item_no: 'FG-FRAME-01', qty: 24, due_in_days: 10, reference: 'SO-1001/10000' }],
    journal: [],
  };

  function seed() {
    S = { seq: {}, items: [], stages: [], recipes: [], demands: [], jobs: [], lots: [], issues: [], events: [], postings: [],
          bc: JSON.parse(JSON.stringify(MOCK_BC)) };
    [['CUT', 'Cutting', 10, 85, '#0ea5e9'], ['PRESS', 'Pressing', 20, 110, '#8b5cf6'], ['WELD', 'Welding', 30, 95, '#f97316'],
     ['PCOAT', 'Powder coating', 40, 120, '#10b981'], ['ASSY', 'Assembly', 50, 75, '#eab308']]
      .forEach(([code, name, sequence, rate_per_hour, color]) => add('stages', { code, name, sequence, rate_per_hour, color }));
    syncItems();
    const st = Object.fromEntries(S.stages.map(s => [s.code, s.id]));
    const it = Object.fromEntries(S.items.map(i => [i.no, i.id]));
    for (const no of ['RM-STL-3MM', 'RM-PWD-BLK']) byId('items', it[no]).lot_tracked = true;
    [['RM-STL-3MM', 'L24-0911', 'H-778120', 20, 'BlueScope', 'MC-55120'],
     ['RM-STL-3MM', 'L24-0930', 'H-779455', 20, 'BlueScope', 'MC-55871'],
     ['RM-PWD-BLK', 'PB-6611', '', 15, 'Dulux', 'COA-6611'],
     ['RM-PWD-BLK', 'PB-6702', '', 10, 'Dulux', 'COA-6702']].forEach(([no, lot_no, heat_no, qty, supplier, cert_ref]) =>
      newLot({ item_id: it[no], lot_no, heat_no, supplier, cert_ref, qty_received: qty, qty_on_hand: qty }));
    const op = (stage, sequence, setup_minutes, run_minutes_per_unit) => ({ id: nextId('recipe_operations'), stage_id: st[stage], sequence, setup_minutes, run_minutes_per_unit });
    const comp = (no, qty_per, scrap_pct, operation_seq) => ({ id: nextId('recipe_components'), item_id: it[no], qty_per, scrap_pct, operation_seq });
    add('recipes', { item_id: it['SA-BRKT-01'], version: '1', output_qty: 20, std_batch_qty: null, active: true, notes: '20 brackets nested per sheet',
      operations: [op('CUT', 10, 15, 0.5), op('PRESS', 20, 30, 0.75)], components: [comp('RM-STL-3MM', 1, 5, 10)] });
    add('recipes', { item_id: it['FG-FRAME-01'], version: '1', output_qty: 1, std_batch_qty: 24, active: true, notes: '',
      operations: [op('CUT', 10, 10, 4), op('WELD', 20, 15, 18), op('PCOAT', 30, 20, 6), op('ASSY', 40, 0, 5)],
      components: [comp('RM-TUBE-50', 1.5, 3, 10), comp('SA-BRKT-01', 4, 0, 20), comp('RM-WIRE-MIG', 0.2, 0, 20),
                   comp('RM-PWD-BLK', 0.6, 10, 30), comp('RM-BOLT-M10', 8, 0, 40)] });
    syncDemand();
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
  const activeRecipes = () => Object.fromEntries(S.items.map(i => [i.id, activeRecipe(i.id)]).filter(([, r]) => r));
  const componentQty = (recipe, c, outQty) => outQty / recipe.output_qty * c.qty_per * (1 + c.scrap_pct / 100);
  const jobIssues = job => S.issues.filter(i => i.job_id === job.id);
  const jobRecipe = job => byId('recipes', job.recipe_id);
  const newLot = l => add('lots', { heat_no: '', supplier: '', cert_ref: '', source: 'receipt', job_id: null, notes: '', created_at: now(), ...l });

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
    for (const job of S.jobs.filter(j => ['planned', 'released', 'in_progress'].includes(j.status))) {
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
      shortfall_value: Math.round(buy.reduce((a, l) => a + l.shortfall_value, 0) * 100) / 100 } };
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
      minutes: extra.minutes || 0, operator: extra.operator || '', note: extra.note || '', created_at: now() });
  }

  function createJob(itemId, qty, dueDate = null, demandId = null) {
    if (!(qty > 0)) fail(409, 'Quantity must be positive');
    const r = activeRecipe(itemId);
    if (!r) fail(409, 'No active recipe for this item');
    if (!r.operations.length) fail(409, 'Recipe has no operations (stages)');
    const n = (S.seq.jobs || 0) + 1;
    const job = add('jobs', { number: 'MO' + String(n).padStart(6, '0'), item_id: itemId, recipe_id: r.id, demand_id: demandId, qty,
      status: 'planned', due_date: dueDate, qty_good: 0, qty_scrap: 0, material_cost: 0, conversion_cost: 0, unit_cost: 0,
      std_unit_cost: r4(standardUnitCost(r, activeRecipes())), cost_snapshot: null, created_at: now(), completed_at: null,
      operations: [...r.operations].sort((a, b) => a.sequence - b.sequence).map(o => ({ id: nextId('job_operations'), stage_id: o.stage_id,
        sequence: o.sequence, setup_minutes: o.setup_minutes, run_minutes_per_unit: o.run_minutes_per_unit, qty_queued: 0,
        qty_in_process: 0, qty_done: 0, qty_scrap: 0, actual_minutes: 0, started_at: null, completed_at: null })) });
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
    if (!op.started_at) { op.started_at = now(); op.actual_minutes += op.setup_minutes; }
    job.status = 'in_progress';
    for (const [c, q] of needs) issueMaterial(job, item(c.item_id), q, op, operator, (lotChoices || {})[c.item_id]);
    log(job, op, 'start', qty, { operator });
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
      add('issues', { job_id: job.id, job_operation_id: op ? op.id : null, item_id: it.id, lot_id: lot ? lot.id : null, qty: q, unit_cost: it.unit_cost, created_at: now() });
      add('postings', { kind: 'consumption', job_id: job.id, document_no: job.number, item_no: it.no, lot_no: lot ? lot.lot_no : '',
        qty: q, unit_cost: it.unit_cost, status: 'pending', attempts: 0, error: '', created_at: now(), posted_at: null });
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
      unit_cost: cost, status: 'pending', attempts: 0, error: '', created_at: now(), posted_at: null });
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

  // ------------------------------------------------------------------ mock BC
  function syncItems() {
    const pending = {};
    for (const p of S.postings.filter(p => p.status !== 'posted')) pending[p.item_no] = (pending[p.item_no] || 0) + (p.kind === 'consumption' ? -p.qty : p.qty);
    let created = 0, updated = 0;
    for (const bi of S.bc.items) {
      let it = S.items.find(i => i.no === bi.no);
      if (!it) { it = add('items', { no: bi.no, item_type: { 'FG-': 'finished', 'SA-': 'sub' }[bi.no.slice(0, 3)] || 'raw', lot_tracked: false, unit_cost: 0 }); created++; } else updated++;
      it.description = bi.description; it.uom = bi.uom;
      if (bi.unit_cost || !it.unit_cost) it.unit_cost = bi.unit_cost;
      it.qty_on_hand = bi.inventory + (pending[bi.no] || 0);
      it.bc_synced_at = now();
    }
    return { created, updated };
  }
  function syncDemand() {
    let created = 0, updated = 0, skipped = 0, closed = 0; const seen = new Set();
    for (const line of S.bc.demand) {
      const it = S.items.find(i => i.no === line.item_no);
      if (!it) { skipped++; continue; }
      seen.add(line.reference);
      const d = S.demands.find(x => x.source === 'bc' && x.reference === line.reference);
      const due = today(line.due_in_days ?? 14);
      if (d) { if (d.status === 'open') { Object.assign(d, { qty: line.qty, due_date: due, item_id: it.id }); updated++; } }
      else { add('demands', { item_id: it.id, qty: line.qty, due_date: due, source: 'bc', reference: line.reference, status: 'open', created_at: now() }); created++; }
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
      S.bc.journal.push({ entry_no: S.bc.journal.length + 1, kind: p.kind, document_no: p.document_no, item_no: p.item_no, qty: p.qty, value: r4(sign * p.qty * bi.unit_cost) });
      p.status = 'posted'; p.error = ''; p.posted_at = now(); posted++;
    }
    return { posted, failed };
  }

  // ------------------------------------------------------------------ serializers
  const ser = {
    item: i => ({ id: i.id, no: i.no, description: i.description, item_type: i.item_type, uom: i.uom, unit_cost: i.unit_cost,
      qty_on_hand: i.qty_on_hand, lot_tracked: !!i.lot_tracked, bc_synced_at: i.bc_synced_at || null }),
    stage: s => ({ ...s }),
    recipe: r => {
      const it = item(r.item_id);
      return { id: r.id, item_id: r.item_id, item_no: it.no, item_description: it.description, version: r.version, output_qty: r.output_qty,
        std_batch_qty: r.std_batch_qty, active: r.active, notes: r.notes,
        operations: [...r.operations].sort((a, b) => a.sequence - b.sequence).map(o => ({ ...o, stage_code: stage(o.stage_id).code, stage_name: stage(o.stage_id).name })),
        components: r.components.map(c => { const ci = item(c.item_id); return { ...c, item_no: ci.no, description: ci.description, uom: ci.uom, unit_cost: ci.unit_cost }; }),
        standard_unit_cost: r4(standardUnitCost(r, activeRecipes())) };
    },
    demand: d => ({ ...d, item_no: item(d.item_id).no, description: item(d.item_id).description }),
    job: (j, detail) => {
      const it = item(j.item_id);
      const out = { id: j.id, number: j.number, item_id: j.item_id, item_no: it.no, recipe_id: j.recipe_id, description: it.description,
        qty: j.qty, status: j.status, due_date: j.due_date, demand_id: j.demand_id, qty_good: j.qty_good, qty_scrap: j.qty_scrap,
        created_at: j.created_at, completed_at: j.completed_at,
        costs: j.status === 'completed' && j.cost_snapshot ? j.cost_snapshot : jobCosts(j),
        operations: j.operations.map(o => { const s = stage(o.stage_id); return { ...o, stage_code: s.code, stage_name: s.name, color: s.color, actual_minutes: Math.round(o.actual_minutes * 100) / 100 }; }) };
      if (detail) {
        out.issues = jobIssues(j).map(i => { const ii = item(i.item_id), l = i.lot_id ? byId('lots', i.lot_id) : null;
          return { item_no: ii.no, description: ii.description, qty: i.qty, lot_id: i.lot_id, lot_no: l ? l.lot_no : '', heat_no: l ? l.heat_no : '',
            uom: ii.uom, unit_cost: i.unit_cost, value: r4(i.qty * i.unit_cost), job_operation_id: i.job_operation_id, created_at: i.created_at }; });
      }
      return out;
    },
    lot: l => { const it = item(l.item_id); return { ...l, item_no: it.no, description: it.description, uom: it.uom }; },
  };

  // ------------------------------------------------------------------ routes
  function getJob(id) { return byId('jobs', id) || fail(404, 'Job not found'); }
  const num = v => (v === '' || v == null ? null : Number(v));

  function route(method, path, q, body) {
    let m;
    if (method === 'GET' && path === '/items') return [...S.items].sort((a, b) => a.no.localeCompare(b.no)).map(ser.item);
    if (method === 'POST' && path === '/items') {
      if (!body.no) fail(422, 'Item number is required');
      if (S.items.some(i => i.no === body.no)) fail(409, 'Item number already exists');
      return ser.item(add('items', { no: body.no, description: body.description || '', item_type: body.item_type || 'raw', uom: body.uom || 'EA',
        unit_cost: +body.unit_cost || 0, qty_on_hand: +body.qty_on_hand || 0, lot_tracked: !!body.lot_tracked }));
    }
    if (method === 'PATCH' && (m = path.match(/^\/items\/(\d+)$/))) {
      const it = item(+m[1]) || fail(404, 'Not found');
      for (const k of ['description', 'item_type', 'uom', 'unit_cost', 'lot_tracked']) if (k in body) it[k] = body[k];
      return ser.item(it);
    }
    if (method === 'GET' && path === '/stages') return [...S.stages].sort((a, b) => a.sequence - b.sequence).map(ser.stage);
    if (method === 'POST' && path === '/stages') return ser.stage(add('stages', { code: body.code, name: body.name, sequence: +body.sequence || 10, rate_per_hour: +body.rate_per_hour || 0, color: body.color || '#64748b' }));
    if (method === 'PATCH' && (m = path.match(/^\/stages\/(\d+)$/))) {
      const s = stage(+m[1]) || fail(404, 'Not found');
      for (const k of ['name', 'sequence', 'rate_per_hour', 'color']) if (k in body) s[k] = body[k];
      return ser.stage(s);
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
      const snapshot = JSON.stringify(S);
      const r = m ? byId('recipes', +m[1]) || fail(404, 'Not found') : add('recipes', {});
      Object.assign(r, { item_id: body.item_id, version: body.version || '1', output_qty: body.output_qty, std_batch_qty: body.std_batch_qty ?? null,
        active: body.active !== false, notes: body.notes || '',
        operations: body.operations.map(o => ({ id: nextId('recipe_operations'), ...o })),
        components: body.components.map(c => ({ id: nextId('recipe_components'), scrap_pct: 0, operation_seq: null, ...c })) });
      if (r.active) S.recipes.filter(o => o.item_id === r.item_id && o.id !== r.id).forEach(o => { o.active = false; });
      try { lowLevelCodes(activeRecipes()); } catch (e) { S = JSON.parse(snapshot); throw e; }
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
      return ser.demand(add('demands', { item_id: body.item_id, qty: body.qty, due_date: body.due_date || null, source: 'manual', reference: body.reference || '', status: 'open', created_at: now() }));
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
      out.events = S.events.filter(e => e.job_id === j.id).sort((a, b) => b.id - a.id)
        .map(e => ({ action: e.action, qty: e.qty, minutes: e.minutes, operator: e.operator, note: e.note, stage: e.stage_id ? stage(e.stage_id).name : null, created_at: e.created_at }));
      return out;
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
            qty_queued: op.qty_queued, qty_in_process: op.qty_in_process, qty_done: op.qty_done, qty_scrap: op.qty_scrap, materials: [...mats.values()] });
        }
      }
      return { stages: [...board.values()], awaiting_finish: jobs.filter(j => j.status === 'in_progress' && unitsRemaining(j) <= EPS)
        .map(j => ({ job_id: j.id, number: j.number, item_no: item(j.item_id).no, qty_good: j.qty_good, qty_scrap: j.qty_scrap })) };
    }
    if (method === 'GET' && path === '/wip/value') {
      const rows = S.jobs.filter(j => ['released', 'in_progress'].includes(j.status)).map(j => { const c = jobCosts(j);
        return { number: j.number, item_no: item(j.item_id).no, total_cost: c.total_cost, output_value: c.bc_value, wip: r4(c.total_cost - c.bc_value) }; });
      return { jobs: rows, total: Math.round(rows.reduce((a, r) => a + r.wip, 0) * 100) / 100 };
    }
    if (method === 'GET' && path === '/events') {
      return [...S.events].sort((a, b) => b.id - a.id).slice(0, +(q.get('limit') || 50)).map(e => { const j = byId('jobs', e.job_id);
        return { job: j.number, item_no: item(j.item_id).no, action: e.action, qty: e.qty, stage: e.stage_id ? stage(e.stage_id).name : null, operator: e.operator, note: e.note, created_at: e.created_at }; });
    }
    if (method === 'GET' && path === '/lots') {
      const term = (q.get('q') || '').toLowerCase(), itemId = num(q.get('item_id')), avail = q.get('available') === 'true';
      return S.lots.filter(l => {
        const it = item(l.item_id);
        if (term && ![l.lot_no, l.heat_no, it.no, l.cert_ref, l.supplier].some(v => (v || '').toLowerCase().includes(term))) return false;
        if (itemId && l.item_id !== itemId) return false;
        return !avail || l.qty_on_hand > EPS;
      }).sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id).map(ser.lot);
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
    const snapshot = JSON.stringify(S);
    try {
      const data = route(method, u.pathname.slice(4), u.searchParams, body);
      if (method !== 'GET') save();
      return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
    } catch (e) {
      S = JSON.parse(snapshot); // roll back, like a failed DB transaction
      if (!(e instanceof ApiError)) console.error(e);
      const status = e instanceof ApiError ? e.status : 500;
      return new Response(JSON.stringify({ detail: e instanceof ApiError ? e.detail : String(e.message || e) }), { status, headers: { 'Content-Type': 'application/json' } });
    }
  };
  // No server to stream from: the UI refreshes itself after each action.
  window.EventSource = class { constructor() { setTimeout(() => this.onopen && this.onopen(), 0); } close() {} };
  window.mfgDemoReset = () => { try { localStorage.removeItem(KEY); } catch { /* ignore */ } seed(); };
})();
