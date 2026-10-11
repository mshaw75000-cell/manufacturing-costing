"""Trial / demo dataset: a small fabrication shop with ten weeks of history.

    python -m app.seed --trial

Builds, through the real production services with a simulated clock:
  * 14 raw materials, 3 sub-assemblies, 5 finished products with recipes
  * 6 stages with hourly rates and daily capacity
  * ~10 weeks of history: 50 completed jobs with stage times, scrap,
    downtime and quality issues, operator notes, purchase orders and receipts
  * 20 open jobs at different stages, open BC sales orders (some not yet
    planned), a sent purchase order, and raw materials running short
  * mock BC state to match, plus a queue of new sales orders that arrive
    each time demand is pulled from BC

Everything is deterministic (fixed random seed) relative to "now".
"""
import math
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import clock
from .bc.mock import MockBusinessCentral
from .models import (BcPosting, Demand, IssueLog, Item, Job, JobNote, Lot, PurchaseRequest, PurchaseRequestLine,
                     Recipe, RecipeComponent, RecipeOperation, Stage)
from .services import production as P
from .services.costing import active_recipes, standard_unit_cost
from .services.planning import component_qty

# code, name, sequence, rate/h, capacity h/day, color
STAGES = [
    ("CUT", "Cutting", 10, 85.0, 15.2, "#0ea5e9"),
    ("PRESS", "Pressing", 20, 110.0, 7.6, "#8b5cf6"),
    ("WELD", "Welding", 30, 95.0, 22.8, "#f97316"),
    ("PCOAT", "Powder coating", 40, 120.0, 7.6, "#10b981"),
    ("ASSY", "Assembly", 50, 75.0, 11.4, "#eab308"),
    ("PACK", "Inspect & pack", 60, 65.0, 7.6, "#64748b"),
]

V = {"V-1001": ("BlueScope Distribution", 5), "V-1002": ("Orrcon Steel", 7), "V-1003": ("Dulux Powders", 10),
     "V-1004": ("BOC Gas & Gear", 3), "V-1005": ("Bolt & Nut Supplies", 4), "V-1006": ("Castors Direct", 14),
     "V-1007": ("Visy Packaging", 5)}

# no, description, uom, std cost, vendor, lot tracked, receipt multiple (x what's needed)
RAW = [
    ("RM-STL-3MM", "Steel sheet 3mm 2400x1200", "SHEET", 185.00, "V-1001", True, 2.5),
    ("RM-STL-2MM", "Steel sheet 2mm 2400x1200", "SHEET", 128.00, "V-1001", True, 2.5),
    ("RM-PLT-6MM", "Steel plate 6mm 2400x1200", "SHEET", 362.00, "V-1001", True, 1.0),
    ("RM-TUBE-50", "RHS tube 50x50x3 6.5m", "LEN", 72.50, "V-1002", True, 2.0),
    ("RM-TUBE-25", "SHS tube 25x25x2 6.5m", "LEN", 31.40, "V-1002", True, 2.5),
    ("RM-FLAT-50", "Flat bar 50x6 6m", "LEN", 28.90, "V-1002", True, 2.5),
    ("RM-WIRE-MIG", "MIG wire 0.9mm", "KG", 9.80, "V-1004", False, 3.0),
    ("RM-PWD-BLK", "Powder coat satin black", "KG", 21.00, "V-1003", True, 2.0),
    ("RM-PWD-WHT", "Powder coat gloss white", "KG", 22.50, "V-1003", True, 1.0),
    ("RM-PWD-GRY", "Powder coat textured grey", "KG", 24.00, "V-1003", True, 2.0),
    ("RM-BOLT-M10", "Bolt M10x30 zinc", "EA", 0.45, "V-1005", False, 3.0),
    ("RM-NUT-M10", "Nyloc nut M10 zinc", "EA", 0.12, "V-1005", False, 3.0),
    ("RM-CASTOR-100", "Castor 100mm swivel braked", "EA", 14.80, "V-1006", False, 1.0),
    ("RM-CTN-L", "Carton large 1200x800x600", "EA", 6.20, "V-1007", False, 2.5),
]
SUB = [("SA-BRKT-01", "Pressed bracket 3mm"), ("SA-GUSSET-01", "Gusset plate 6mm"),
       ("SA-PANEL-01", "Side panel 2mm folded")]
FG = [("FG-FRAME-01", "Welded frame, satin black"), ("FG-GATE-01", "Security gate, satin black"),
      ("FG-TROLLEY-01", "Workshop trolley, textured grey"), ("FG-SHELF-01", "Shelving bay, gloss white"),
      ("FG-BENCH-01", "Workbench 1800, textured grey")]
# BC standard as a share of the recipe standard: some are out of date on purpose
BC_STD_FACTOR = {"FG-FRAME-01": 0.97, "FG-GATE-01": 1.0, "FG-TROLLEY-01": 1.04, "FG-SHELF-01": 0.96,
                 "FG-BENCH-01": 1.0, "SA-BRKT-01": 1.0, "SA-GUSSET-01": 1.0, "SA-PANEL-01": 1.0}

# item: (output qty, std batch, [(stage, seq, setup, run)], [(component, qty, scrap %, at seq)], notes)
RECIPES = {
    "SA-BRKT-01": (20, 200, [("CUT", 10, 15, 0.5), ("PRESS", 20, 30, 0.75)],
                   [("RM-STL-3MM", 1, 5, 10)], "20 brackets nested per sheet"),
    "SA-GUSSET-01": (40, 160, [("CUT", 10, 20, 0.6), ("PRESS", 20, 25, 0.8)],
                     [("RM-PLT-6MM", 1, 4, 10)], "40 gussets nested per plate"),
    "SA-PANEL-01": (4, 120, [("CUT", 10, 15, 2.5), ("PRESS", 20, 30, 3.0)],
                    [("RM-STL-2MM", 1, 3, 10)], "4 panels per sheet, 3 folds each"),
    "FG-FRAME-01": (1, 60, [("CUT", 10, 10, 4), ("WELD", 20, 15, 18), ("PCOAT", 30, 20, 6), ("ASSY", 40, 0, 5),
                            ("PACK", 50, 5, 3)],
                    [("RM-TUBE-50", 1.5, 3, 10), ("SA-BRKT-01", 4, 0, 20), ("RM-WIRE-MIG", 0.2, 0, 20),
                     ("RM-PWD-BLK", 0.6, 10, 30), ("RM-BOLT-M10", 8, 0, 40), ("RM-CTN-L", 1, 0, 50)], ""),
    "FG-GATE-01": (1, 20, [("CUT", 10, 15, 8), ("WELD", 20, 20, 35), ("PCOAT", 30, 20, 9), ("ASSY", 40, 0, 10),
                           ("PACK", 50, 5, 4)],
                   [("RM-TUBE-50", 2.2, 3, 10), ("RM-FLAT-50", 1.5, 3, 10), ("SA-GUSSET-01", 6, 0, 20),
                    ("RM-WIRE-MIG", 0.35, 0, 20), ("RM-PWD-BLK", 0.9, 10, 30), ("RM-BOLT-M10", 6, 0, 40),
                    ("RM-NUT-M10", 6, 0, 40)], "Hinges supplied by customer"),
    "FG-TROLLEY-01": (1, 40, [("CUT", 10, 10, 5), ("WELD", 20, 15, 22), ("PCOAT", 30, 20, 7), ("ASSY", 40, 10, 12),
                              ("PACK", 50, 5, 4)],
                      [("RM-TUBE-25", 2.4, 3, 10), ("SA-PANEL-01", 2, 0, 20), ("SA-BRKT-01", 2, 0, 20),
                       ("RM-WIRE-MIG", 0.25, 0, 20), ("RM-PWD-GRY", 0.8, 10, 30), ("RM-CASTOR-100", 4, 0, 40),
                       ("RM-BOLT-M10", 16, 0, 40), ("RM-NUT-M10", 16, 0, 40), ("RM-CTN-L", 1, 0, 50)], ""),
    "FG-SHELF-01": (1, 80, [("CUT", 10, 10, 3), ("WELD", 20, 15, 10), ("PCOAT", 30, 20, 6), ("ASSY", 40, 5, 8),
                            ("PACK", 50, 5, 3)],
                    [("RM-TUBE-25", 1.2, 3, 10), ("SA-PANEL-01", 4, 0, 20), ("SA-BRKT-01", 8, 0, 20),
                     ("RM-WIRE-MIG", 0.1, 0, 20), ("RM-PWD-WHT", 0.7, 10, 30), ("RM-BOLT-M10", 24, 0, 40),
                     ("RM-NUT-M10", 24, 0, 40), ("RM-CTN-L", 1, 0, 50)], "Flat-packed"),
    "FG-BENCH-01": (1, 24, [("CUT", 10, 15, 9), ("WELD", 20, 20, 40), ("PCOAT", 30, 20, 10), ("ASSY", 40, 10, 15),
                            ("PACK", 50, 5, 5)],
                    [("RM-TUBE-50", 3.0, 3, 10), ("SA-GUSSET-01", 8, 0, 20), ("RM-WIRE-MIG", 0.4, 0, 20),
                     ("RM-PWD-GRY", 1.1, 10, 30), ("RM-BOLT-M10", 12, 0, 40), ("RM-NUT-M10", 12, 0, 40)], ""),
}
JOB_SIZE = {"FG-FRAME-01": (40, 72), "FG-GATE-01": (12, 24), "FG-TROLLEY-01": (24, 48), "FG-SHELF-01": (50, 96),
            "FG-BENCH-01": (12, 28)}
SA_BATCH = {"SA-BRKT-01": 1600, "SA-GUSSET-01": 480, "SA-PANEL-01": 480}
# raw materials left short at the end, so the plan has something to buy
SHORT = {"RM-CASTOR-100", "RM-PWD-WHT", "RM-PLT-6MM", "RM-FLAT-50"}
MIX = ["FG-FRAME-01"] * 3 + ["FG-SHELF-01"] * 3 + ["FG-TROLLEY-01"] * 2 + ["FG-GATE-01"] * 2 + ["FG-BENCH-01"] * 2

CUSTOMERS = ["Coastal Fitouts", "Bunnings Trade QLD", "Metro Storage Solutions", "Hendra Engineering",
             "Allied Shopfit", "Northside Schools", "Pinnacle Warehousing", "Redline Automotive", "CQ Mining Services"]
OPERATORS = {"CUT": ["Mick O'Brien", "Tom Nguyen"], "PRESS": ["Lena Kowalski"], "WELD": ["Sam Patel", "Jack Reid", "Ana Silva"],
             "PCOAT": ["Priya Nair", "Ben Carter"], "ASSY": ["Grace Holt", "Tom Nguyen"], "PACK": ["Grace Holt", "Ravi Shah"]}

# actual time vs standard per stage at the start and end of the history (improvement over time)
PERF = {"CUT": (1.14, 1.06), "PRESS": (1.28, 1.14), "WELD": (1.30, 1.10), "PCOAT": (1.12, 1.05),
        "ASSY": (1.20, 1.08), "PACK": (1.06, 1.00)}
# downtime: events per standard hour of work, (min, max) minutes, [(category, title, detail, resolution)]
DOWNTIME = {
    "CUT": (0.10, (20, 75), [("breakdown", "Bandsaw blade snapped", "Blade failed mid-cut on 50x50 RHS.", "Blade replaced; tension checked."),
                             ("waiting", "Waiting on forklift to unload steel", "Rack empty until delivery unloaded.", "Delivery unloaded.")]),
    "PRESS": (0.22, (20, 80), [("changeover", "Press brake tool change overran", "Die set for previous job not returned to rack.", "Tooling board set up beside press."),
                               ("breakdown", "Back gauge fault", "Back gauge not homing; reset twice.", "Encoder cable re-terminated.")]),
    "WELD": (0.12, (15, 60), [("breakdown", "Wire feed jamming on welder 2", "Liner worn, wire birdnesting.", "Liner replaced."),
                              ("material", "Shielding gas cylinder empty", "Cylinder ran out mid-shift; no spare on bay.", "Second cylinder kept on bay."),
                              ("waiting", "Waiting for jig to be freed", "Jig in use on another job.", "Second jig ordered.")]),
    "PCOAT": (0.22, (30, 120), [("breakdown", "Curing oven over-temperature alarm", "Oven tripped at 215 C; line stopped to cool.", "Thermocouple replaced."),
                                ("changeover", "Colour change took longer than planned", "Booth clean-down between black and white.", "Colour sequence planned light to dark."),
                                ("breakdown", "Conveyor chain stopped", "Drive sprocket slipping.", "Sprocket and chain tensioned.")]),
    "ASSY": (0.10, (20, 60), [("material", "Fasteners short at assembly", "Bolt bin empty; picked from stores.", "Kanban bins added."),
                              ("waiting", "Waiting for parts from powder coat", "Parts still curing.", "Allow cure time in schedule.")]),
    "PACK": (0.06, (10, 30), [("material", "No large cartons at packing", "Cartons not booked out.", "Carton stock added to reorder list.")]),
}
# quality: probability per operation, max units scrapped, [(title, detail)]
QUALITY = {
    "CUT": (0.08, 2, [("Tube cut short", "Stop set 5 mm short; parts outside tolerance.")]),
    "WELD": (0.35, 3, [("Weld porosity on corner joints", "Porosity found on visual inspection."),
                       ("Frame distorted after welding", "Out of square by 6 mm; could not be pulled back.")]),
    "PCOAT": (0.40, 4, [("Orange peel finish", "Film build too high on top rail."),
                        ("Contamination in powder finish", "Specks in finish; booth filters due.")]),
    "ASSY": (0.05, 1, [("Thread stripped on mounting point", "Insert spun during assembly.")]),
}
NOTES = [
    ("First-off checked and signed", "Dimensions within tolerance against drawing rev C."),
    ("Used offcuts from rack B", "Offcuts long enough for short members; saved about one length."),
    ("Powder batch changed mid-run", "Switched to new box of powder at about half way; colour match OK."),
    ("Jig 3 clamps worn", "Clamp pads replaced with spares; maintenance asked to order more."),
    ("Customer asked for photos before dispatch", "Photos of labels and packing to be emailed to the rep."),
    ("Extra QC on corner welds", "100% visual inspection after last week's porosity."),
    ("Masking added to threaded holes", "Prevents powder build-up on M10 threads."),
    ("Run split across two shifts", "Afternoon shift to finish; parts tagged at the station."),
]


class _Sim:
    def __init__(self, db: Session, now: datetime, seed: int):
        self.db, self.now, self.rng = db, now, random.Random(seed)
        self.items: dict[str, Item] = {}
        self.stages: dict[str, Stage] = {}
        self.receive_mult: dict[str, float] = {}
        self.history_start = now - timedelta(weeks=10)
        self.pr_seq = 0
        self.so_seq = 1040

    # -------------------------------------------------------------- helpers
    def at(self, t: datetime):
        clock.set_now(min(t, self.now))

    def operator(self, stage_code):
        return self.rng.choice(OPERATORS[stage_code])

    def next_so(self):
        self.so_seq += 1
        return f"SO-{self.so_seq}/10000"

    def receive(self, item: Item, qty: float, t: datetime, status="received"):
        """A purchase order for raw material, received at time t (or still on order)."""
        db = self.db
        qty = float(math.ceil(qty)) if item.uom in ("EA", "SHEET", "LEN") else math.ceil(qty)
        self.pr_seq += 1
        vendor = V[item.vendor_no]
        placed = t - timedelta(days=vendor[1])
        self.at(placed)
        pr = PurchaseRequest(number=f"PR{self.pr_seq:05d}", vendor_no=item.vendor_no, vendor_name=vendor[0],
                             status=status, bc_po_no=f"PO-{10000 + self.pr_seq}", needed_by=t.date(),
                             created_by="Planner", created_at=placed, sent_at=placed + timedelta(hours=1),
                             received_at=t if status == "received" else None)
        pr.lines.append(PurchaseRequestLine(item_id=item.id, item=item, qty=qty, unit_cost=item.unit_cost, needed_by=t.date()))
        db.add(pr)
        if status == "received":
            self.at(t)
            item.qty_on_hand += qty
            if item.lot_tracked:
                db.add(Lot(item_id=item.id, lot_no=f"{pr.bc_po_no}-1",
                           heat_no=f"H-{self.rng.randint(770000, 799999)}" if item.no.startswith(("RM-STL", "RM-PLT", "RM-TUBE", "RM-FLAT"))
                           else f"B-{self.rng.randint(6000, 9999)}",
                           supplier=vendor[0], cert_ref=f"MC-{self.rng.randint(50000, 69999)}" if item.no.startswith(("RM-STL", "RM-PLT", "RM-TUBE", "RM-FLAT")) else f"COA-{self.rng.randint(6000, 9999)}",
                           qty_received=qty, qty_on_hand=qty))
        db.flush()
        return pr

    def ensure_stock(self, job: Job, op, qty: float, t: datetime):
        for comp in P._components_for_op(job, op):
            need = component_qty(job.recipe, comp, qty)
            it = comp.item
            if it.qty_on_hand + 1e-9 >= need:
                continue
            if it.item_type == "raw":
                self.receive(it, (need - it.qty_on_hand) * self.receive_mult[it.no] + need * 0.1, t - timedelta(hours=6))
            else:
                self.make_sub(it, need - it.qty_on_hand, t - timedelta(days=2))

    # -------------------------------------------------------------- job running
    def run_op(self, job: Job, op, qty: float, t: datetime, complete_all=True, done_qty=None) -> datetime:
        """Start qty at op at time t, maybe scrap some, complete (all or done_qty). Returns end time."""
        code = op.stage.code
        self.ensure_stock(job, op, qty, t)
        who = self.operator(code)
        self.at(t)
        P.start(self.db, job, op.id, qty, who, force=True)
        progress = (t - self.history_start) / (self.now - self.history_start)
        lo, hi = PERF[code]
        factor = (lo + (hi - lo) * max(0.0, min(1.0, progress))) * self.rng.uniform(0.94, 1.06)

        elapsed = op.setup_minutes
        # downtime: events in proportion to the hours of work
        rate, (mn, mx), causes = DOWNTIME[code]
        std_hours = (op.setup_minutes + op.run_minutes_per_unit * qty) / 60
        for _ in range(self._poisson(rate * std_hours)):
            cat, title, detail, resolution = self.rng.choice(causes)
            mins = self.rng.randint(mn, mx)
            when = t + timedelta(minutes=elapsed + self.rng.uniform(10, 60))
            self.issue(job, op, cat, title, detail, mins, 0, who, when, resolution, self.rng.choice(["medium", "high", "low"]))
            elapsed += mins
        # scrap
        scrapped = 0
        if code in QUALITY and self.rng.random() < QUALITY[code][0]:
            scrapped = min(self.rng.randint(1, QUALITY[code][1]), max(0, int(qty) - 1))
            if scrapped:
                title, detail = self.rng.choice(QUALITY[code][2])
                when = t + timedelta(minutes=elapsed + 30)
                self.at(when)
                P.scrap(self.db, job, op.id, scrapped, "in_process", who, title)
                self.issue(job, op, "quality", title, detail, 0, scrapped, who, when,
                           "Scrapped and remade within the job" if self.rng.random() < 0.5 else "Root cause reviewed at toolbox meeting",
                           "medium")
        # notes
        if self.rng.random() < 0.22:
            note, detail = self.rng.choice(NOTES)
            self.at(t + timedelta(minutes=elapsed + 5))
            self.db.add(JobNote(job_id=job.id, job_operation_id=op.id, stage_id=op.stage_id, note=note,
                                detail=detail if self.rng.random() < 0.7 else "", operator=who))
        to_complete = (qty - scrapped) if complete_all else min(done_qty or 0, qty - scrapped)
        run_min = op.run_minutes_per_unit * to_complete * factor
        end = t + timedelta(minutes=elapsed + run_min)
        if to_complete > 0:
            self.at(end)
            P.complete(self.db, job, op.id, to_complete, round(run_min, 1), who)
        return end

    def issue(self, job, op, cat, title, detail, minutes, qty, who, when, resolution, severity, resolved=True):
        self.at(when)
        n = (self.db.scalar(select(func.max(IssueLog.id))) or 0) + 1
        resolved_at = when + timedelta(hours=self.rng.randint(1, 30))
        resolved = resolved and resolved_at < self.now
        self.db.add(IssueLog(number=f"ISS{n:05d}", job_id=job.id if job else None, job_operation_id=op.id if op else None,
                             stage_id=op.stage_id if op else None, category=cat, severity=severity, title=title,
                             detail=detail, minutes_lost=minutes, qty_affected=qty, operator=who,
                             status="resolved" if resolved else "open", resolution=resolution if resolved else "",
                             resolved_at=resolved_at if resolved else None))
        self.db.flush()

    def _poisson(self, lam: float) -> int:
        n, p, threshold = 0, 1.0, math.exp(-lam)
        while True:
            p *= self.rng.random()
            if p <= threshold:
                return n
            n += 1

    def gap(self):
        return timedelta(hours=self.rng.uniform(1, 14))

    def make_job(self, item: Item, qty: float, t: datetime, demand: Demand | None = None) -> Job:
        self.at(t)
        job = P.create_job(self.db, item.id, qty, (demand.due_date if demand else (t + timedelta(days=10)).date()),
                           demand_id=demand.id if demand else None)
        self.db.flush()
        return job

    def make_sub(self, item: Item, need: float, t: datetime):
        """Make a batch of a sub-assembly, finishing before t."""
        qty = max(SA_BATCH[item.no], math.ceil(need / 20) * 20)
        start = t - timedelta(days=2)
        job = self.make_job(item, qty, start)
        self.complete_job(job, start)

    def complete_job(self, job: Job, t: datetime) -> datetime:
        self.at(t)
        P.release_job(self.db, job, "Planner")
        t += timedelta(minutes=30)
        for op in job.operations:
            q = op.qty_queued
            if q <= 0:
                break
            t = self.run_op(job, op, q, t) + self.gap()
        self.at(t)
        P.finish(self.db, job, "Supervisor")
        if job.demand_id:  # BC ships the order to the customer
            job.item.qty_on_hand -= min(job.qty_good, self.db.get(Demand, job.demand_id).qty)
        self.db.flush()
        return t

    # -------------------------------------------------------------- build
    def master_data(self):
        db = self.db
        self.at(self.history_start - timedelta(days=14))
        for code, name, seq, rate, cap, color in STAGES:
            s = Stage(code=code, name=name, sequence=seq, rate_per_hour=rate, capacity_hours_per_day=cap, color=color)
            db.add(s)
            self.stages[code] = s
        for no, desc, uom, cost, vendor, lot, mult in RAW:
            it = Item(no=no, description=desc, item_type="raw", uom=uom, unit_cost=cost, lot_tracked=lot,
                      vendor_no=vendor, vendor_name=V[vendor][0], lead_time_days=V[vendor][1], qty_on_hand=0)
            db.add(it)
            self.items[no] = it
            self.receive_mult[no] = 1.0 if no in SHORT else max(mult, 3.0)
        for no, desc in SUB:
            self.items[no] = Item(no=no, description=desc, item_type="sub", uom="EA")
            db.add(self.items[no])
        for no, desc in FG:
            self.items[no] = Item(no=no, description=desc, item_type="finished", uom="EA")
            db.add(self.items[no])
        db.flush()
        for no, (out, batch, ops, comps, notes) in RECIPES.items():
            db.add(Recipe(item_id=self.items[no].id, output_qty=out, std_batch_qty=batch, notes=notes,
                          operations=[RecipeOperation(stage_id=self.stages[c].id, sequence=s, setup_minutes=su,
                                                      run_minutes_per_unit=r) for c, s, su, r in ops],
                          components=[RecipeComponent(item_id=self.items[c].id, qty_per=q, scrap_pct=sc, operation_seq=at)
                                      for c, q, sc, at in comps]))
        db.flush()
        # BC standard costs: sub-assemblies first, then products (which roll up subs at their BC standard)
        for group in (SUB, FG):
            active = active_recipes(db)
            for no, _ in group:
                it = self.items[no]
                it.unit_cost = round(standard_unit_cost(active[it.id], active) * BC_STD_FACTOR[no], 2)
            db.flush()
        # opening stock
        for no, *_ in RAW:
            it = self.items[no]
            self.receive(it, {"EA": 400, "KG": 60}.get(it.uom, 30) if it.unit_cost > 1 else 3000,
                         self.history_start - timedelta(days=3))

    def history(self, target_jobs=50):
        """Weekly FG jobs (more each week as the business grows) until target completed jobs."""
        db = self.db
        per_week = [3, 3, 4, 4, 4, 4, 5, 5, 5, 5]
        for week, n in enumerate(per_week):
            growth = 0.8 + 0.4 * week / (len(per_week) - 1)  # the business is growing
            for k in range(n):
                done = db.scalar(select(func.count()).select_from(Job).where(Job.status == "completed"))
                if done >= target_jobs:
                    return
                prod = self.rng.choice(MIX)
                lo, hi = JOB_SIZE[prod]
                qty = round(self.rng.randint(lo, hi) * growth)
                t = self.history_start + timedelta(weeks=week, days=k * 5 / n, hours=self.rng.uniform(6, 9))
                t = self.working(t)
                self.at(t - timedelta(days=12))
                so = Demand(item_id=self.items[prod].id, qty=qty, due_date=(t + timedelta(days=10)).date(), source="bc",
                            reference=self.next_so(), customer=self.rng.choice(CUSTOMERS))
                db.add(so)
                db.flush()
                job = self.make_job(self.items[prod], qty, t - timedelta(days=1), so)
                end = self.complete_job(job, t)
                if end > self.now - timedelta(days=8):
                    return  # keep the last week for the open jobs

    def working(self, t: datetime) -> datetime:
        while t.weekday() >= 5:
            t += timedelta(days=1)
        return t

    def open_jobs(self):
        """20 open jobs: 3 planned, 2 released, the rest part-way through their stages."""
        db = self.db
        plans = [  # product, stage index reached, fraction done at that stage, linked to a sales order
            ("FG-FRAME-01", None, 0, True), ("FG-SHELF-01", None, 0, True), ("FG-BENCH-01", None, 0, False),
            ("FG-GATE-01", 0, 0.0, True), ("SA-PANEL-01", 0, 0.0, False),
            ("FG-FRAME-01", 0, 0.5, True), ("FG-TROLLEY-01", 0, 0.8, True), ("SA-BRKT-01", 1, 0.3, False),
            ("FG-SHELF-01", 1, 0.4, True), ("FG-GATE-01", 1, 0.6, True), ("FG-BENCH-01", 1, 0.2, True),
            ("FG-FRAME-01", 1, 0.9, True), ("SA-GUSSET-01", 1, 0.5, False),
            ("FG-TROLLEY-01", 2, 0.3, True), ("FG-SHELF-01", 2, 0.7, True), ("FG-FRAME-01", 2, 0.5, True),
            ("FG-GATE-01", 3, 0.5, True), ("FG-TROLLEY-01", 3, 0.25, True), ("FG-SHELF-01", 4, 0.6, True),
            ("FG-BENCH-01", 4, 0.5, True),
        ]
        for i, (prod, reach, frac, linked) in enumerate(plans):
            item = self.items[prod]
            lo, hi = JOB_SIZE.get(prod, (SA_BATCH.get(prod, 200),) * 2)
            qty = self.rng.randint(lo, hi) if prod.startswith("FG") else SA_BATCH[prod]
            # start far enough back that the stages reached so far finish, in order, before now
            recipe = active_recipes(db)[item.id]
            ops = sorted(recipe.operations, key=lambda o: o.sequence)
            est = sum((o.setup_minutes + o.run_minutes_per_unit * qty * 1.5) / 60 + 12
                      for o in ops[: (reach or 0) + 1]) if reach is not None else 0
            t0 = self.now - timedelta(hours=est + self.rng.uniform(6, 30))
            so = None
            if linked:
                self.at(t0 - timedelta(days=5))
                so = Demand(item_id=item.id, qty=qty, due_date=(self.now + timedelta(days=self.rng.randint(-1, 18))).date(),
                            source="bc", reference=self.next_so(), customer=self.rng.choice(CUSTOMERS))
                db.add(so)
                db.flush()
            job = self.make_job(item, qty, t0, so)
            if reach is None:
                continue
            self.at(t0 + timedelta(hours=1))
            P.release_job(db, job, "Planner")
            t = t0 + timedelta(hours=2)
            for idx, op in enumerate(job.operations):
                q = op.qty_queued
                if q <= 0 or idx > reach:
                    break
                if idx < reach:
                    t = self.run_op(job, op, q, t) + timedelta(hours=self.rng.uniform(1, 6))
                elif frac > 0:
                    # part done: start most of the queue, complete a fraction of it
                    start_q = max(1, round(q * min(1.0, frac + 0.3)))
                    self.run_op(job, op, start_q, t, complete_all=False, done_qty=round(q * frac))
            # some live issues still open on the floor
            if i in (7, 9, 12, 13, 15, 17):
                op = job.operations[min(reach, len(job.operations) - 1)]
                code = op.stage.code
                cat, title, detail, _ = self.rng.choice(DOWNTIME[code][2])
                self.issue(job, op, cat, title, detail, self.rng.randint(20, 60), 0, self.operator(code),
                           self.now - timedelta(hours=self.rng.randint(1, 5)), "",
                           {7: "low", 12: "medium", 13: "medium"}.get(i, "high"), resolved=False)

    def open_demand_without_jobs(self):
        db = self.db
        for prod, qty, days in [("FG-FRAME-01", 48, 12), ("FG-SHELF-01", 72, 16), ("FG-TROLLEY-01", 30, 20),
                                ("FG-GATE-01", 16, 9), ("FG-BENCH-01", 20, 24)]:
            self.at(self.now - timedelta(hours=self.rng.randint(4, 60)))
            db.add(Demand(item_id=self.items[prod].id, qty=qty, due_date=(self.now + timedelta(days=days)).date(),
                          source="bc", reference=self.next_so(), customer=self.rng.choice(CUSTOMERS)))
        db.flush()

    def finish_up(self, bc: MockBusinessCentral):
        db = self.db
        # top up everything except the deliberately short materials (a recent delivery)
        from .services.planning import build_plan
        for line in build_plan(db)["lines"]:
            if line["action"] == "buy" and line["item_no"] not in SHORT:
                it = self.items[line["item_no"]]
                self.receive(it, line["net"] * 1.6 + 5, self.now - timedelta(days=self.rng.randint(1, 4)))
        # one purchase order still on the water
        self.receive(self.items["RM-TUBE-50"], 60, self.now + timedelta(days=3), status="sent")
        # postings older than a day have gone to BC; the rest are waiting
        cutoff = self.now - timedelta(days=1)
        for p in db.scalars(select(BcPosting)):
            if p.created_at.replace(tzinfo=timezone.utc) < cutoff:
                p.status, p.attempts, p.posted_at = "posted", 1, p.created_at + timedelta(minutes=5)
        db.flush()
        pending = {}
        for p in db.scalars(select(BcPosting).where(BcPosting.status != "posted")):
            pending[p.item_no] = pending.get(p.item_no, 0.0) + (-p.qty if p.kind == "consumption" else p.qty)
        items = []
        for it in db.scalars(select(Item).order_by(Item.id)):
            it.bc_synced_at = self.now - timedelta(hours=2)
            row = {"no": it.no, "description": it.description, "uom": it.uom, "unit_cost": it.unit_cost,
                   "inventory": round(it.qty_on_hand - pending.get(it.no, 0.0), 4)}
            if it.vendor_no:
                row.update(vendor_no=it.vendor_no, vendor_name=it.vendor_name, lead_time_days=it.lead_time_days)
            items.append(row)
        demand = [{"item_no": d.item.no, "qty": d.qty, "due_date": d.due_date.isoformat(), "reference": d.reference,
                   "customer": d.customer} for d in db.scalars(select(Demand).where(Demand.status == "open", Demand.source == "bc"))]
        incoming = []
        for prod, qty, days in [("FG-SHELF-01", 40, 14), ("FG-GATE-01", 10, 11), ("FG-FRAME-01", 36, 15),
                                ("FG-BENCH-01", 12, 21), ("FG-TROLLEY-01", 24, 18), ("FG-SHELF-01", 60, 25),
                                ("FG-FRAME-01", 24, 22), ("FG-GATE-01", 8, 28)]:
            incoming.append({"item_no": prod, "qty": qty, "due_in_days": days, "reference": self.next_so(),
                             "customer": self.rng.choice(CUSTOMERS)})
        pos = [{"no": pr.bc_po_no, "vendor_no": pr.vendor_no, "reference": pr.number,
                "status": "Received" if pr.status == "received" else "Released",
                "lines": [{"item_no": l.item.no, "qty": l.qty, "unit_cost": l.unit_cost,
                           "needed_by": l.needed_by.isoformat() if l.needed_by else None} for l in pr.lines]}
               for pr in db.scalars(select(PurchaseRequest).order_by(PurchaseRequest.id))]
        bc.save_state({"items": items, "demand": demand, "incoming": incoming, "journal": [], "purchase_orders": pos})


def build_trial(db: Session, bc: MockBusinessCentral, now: datetime | None = None, seed: int = 7) -> dict:
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    sim = _Sim(db, now, seed)
    try:
        sim.master_data()
        sim.history()
        sim.open_jobs()
        sim.open_demand_without_jobs()
        sim.finish_up(bc)
        db.commit()
    finally:
        clock.set_now(None)
    count = lambda q: db.scalar(select(func.count()).select_from(Job).where(q))  # noqa: E731
    return {"completed_jobs": count(Job.status == "completed"),
            "open_jobs": count(Job.status.in_(("planned", "released", "in_progress")))}

