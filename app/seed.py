"""Demo data.

    python -m app.seed           small starter set: two recipes and one BC order (used by tests)
    python -m app.seed --trial   full trial dataset: 10 weeks of history, 20 open jobs (see trial_data.py)
"""
import sys

from sqlalchemy import select

from .bc.sync import get_bc, sync_demand, sync_items
from .db import Base, SessionLocal, engine
from .models import Item, Lot, Recipe, RecipeComponent, RecipeOperation, Stage

STAGES = [
    ("CUT", "Cutting", 10, 85.0, "#0ea5e9"),
    ("PRESS", "Pressing", 20, 110.0, "#8b5cf6"),
    ("WELD", "Welding", 30, 95.0, "#f97316"),
    ("PCOAT", "Powder coating", 40, 120.0, "#10b981"),
    ("ASSY", "Assembly", 50, 75.0, "#eab308"),
]


def seed():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if db.scalar(select(Stage)):
            print("Already seeded")
            return
        for code, name, seq, rate, color in STAGES:
            db.add(Stage(code=code, name=name, sequence=seq, rate_per_hour=rate, color=color))
        db.flush()
        sync_items(db, get_bc())
        db.flush()
        st = {s.code: s.id for s in db.scalars(select(Stage))}
        it = {i.no: i.id for i in db.scalars(select(Item))}

        # Steel and powder are lot tracked: heat numbers / powder batches.
        for no in ("RM-STL-3MM", "RM-PWD-BLK"):
            db.get(Item, it[no]).lot_tracked = True
        for no, lot_no, heat, qty, supplier, cert in [
            ("RM-STL-3MM", "L24-0911", "H-778120", 20, "BlueScope", "MC-55120"),
            ("RM-STL-3MM", "L24-0930", "H-779455", 20, "BlueScope", "MC-55871"),
            ("RM-PWD-BLK", "PB-6611", "", 15, "Dulux", "COA-6611"),
            ("RM-PWD-BLK", "PB-6702", "", 10, "Dulux", "COA-6702"),
        ]:
            db.add(Lot(item_id=it[no], lot_no=lot_no, heat_no=heat, supplier=supplier, cert_ref=cert,
                       qty_received=qty, qty_on_hand=qty))

        # Sub-assembly: bracket pressed from sheet, 20 brackets per sheet.
        db.add(Recipe(item_id=it["SA-BRKT-01"], output_qty=20, notes="20 brackets nested per sheet",
                      operations=[RecipeOperation(stage_id=st["CUT"], sequence=10, setup_minutes=15, run_minutes_per_unit=0.5),
                                  RecipeOperation(stage_id=st["PRESS"], sequence=20, setup_minutes=30, run_minutes_per_unit=0.75)],
                      components=[RecipeComponent(item_id=it["RM-STL-3MM"], qty_per=1, scrap_pct=5, operation_seq=10)]))
        # Finished frame: cut tube, weld with 4 brackets, powder coat, assemble.
        db.add(Recipe(item_id=it["FG-FRAME-01"], output_qty=1, std_batch_qty=24,
                      operations=[RecipeOperation(stage_id=st["CUT"], sequence=10, setup_minutes=10, run_minutes_per_unit=4),
                                  RecipeOperation(stage_id=st["WELD"], sequence=20, setup_minutes=15, run_minutes_per_unit=18),
                                  RecipeOperation(stage_id=st["PCOAT"], sequence=30, setup_minutes=20, run_minutes_per_unit=6),
                                  RecipeOperation(stage_id=st["ASSY"], sequence=40, setup_minutes=0, run_minutes_per_unit=5)],
                      components=[RecipeComponent(item_id=it["RM-TUBE-50"], qty_per=1.5, scrap_pct=3, operation_seq=10),
                                  RecipeComponent(item_id=it["SA-BRKT-01"], qty_per=4, operation_seq=20),
                                  RecipeComponent(item_id=it["RM-WIRE-MIG"], qty_per=0.2, operation_seq=20),
                                  RecipeComponent(item_id=it["RM-PWD-BLK"], qty_per=0.6, scrap_pct=10, operation_seq=30),
                                  RecipeComponent(item_id=it["RM-BOLT-M10"], qty_per=8, operation_seq=40)]))
        db.flush()
        sync_demand(db, get_bc())
        db.commit()
        print("Seeded demo data")
    finally:
        db.close()


def seed_trial():
    from .trial_data import build_trial
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if db.scalar(select(Stage)):
            print("Database already has data; delete mfgcost.db (and bc_mock.json) first")
            return
        print("Trial data:", build_trial(db, get_bc()))
    finally:
        db.close()


if __name__ == "__main__":
    seed_trial() if "--trial" in sys.argv else seed()
