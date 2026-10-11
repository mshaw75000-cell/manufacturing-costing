"""Items, stages and recipes (BOM + routing)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Item, Recipe, RecipeComponent, RecipeOperation, Stage
from ..services import assumptions as A
from ..services.costing import active_recipes as _active
from ..services.planning import RecipeCycleError, _low_level_codes
from . import serialize as S

router = APIRouter(prefix="/api")


class ItemIn(BaseModel):
    no: str
    description: str = ""
    item_type: str = Field("raw", pattern="^(raw|sub|finished)$")
    uom: str = "EA"
    unit_cost: float = 0.0
    qty_on_hand: float = 0.0
    lot_tracked: bool = False


@router.get("/items")
def list_items(db: Session = Depends(get_db)):
    return [S.item(i) for i in db.scalars(select(Item).order_by(Item.no))]


@router.post("/items")
def create_item(body: ItemIn, db: Session = Depends(get_db)):
    if db.scalar(select(Item).where(Item.no == body.no)):
        raise HTTPException(409, "Item number already exists")
    item = Item(**body.model_dump())
    db.add(item)
    db.commit()
    return S.item(item)


@router.patch("/items/{item_id}")
def update_item(item_id: int, body: dict, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if not item:
        _404()
    for k in ("description", "item_type", "uom", "unit_cost", "lot_tracked", "vendor_no", "vendor_name", "lead_time_days"):
        if k in body:
            setattr(item, k, body[k])
    db.commit()
    return S.item(item)


class StageIn(BaseModel):
    code: str
    name: str
    sequence: int = 10
    rate_per_hour: float = 0.0
    capacity_hours_per_day: float = 7.6
    color: str = "#64748b"


@router.get("/stages")
def list_stages(db: Session = Depends(get_db)):
    return [S.stage(s) for s in db.scalars(select(Stage).order_by(Stage.sequence))]


@router.post("/stages")
def create_stage(body: StageIn, db: Session = Depends(get_db)):
    s = Stage(**body.model_dump())
    db.add(s)
    db.commit()
    return S.stage(s)


@router.patch("/stages/{stage_id}")
def update_stage(stage_id: int, body: dict, db: Session = Depends(get_db)):
    s = db.get(Stage, stage_id)
    if not s:
        _404()
    for k in ("name", "sequence", "rate_per_hour", "capacity_hours_per_day", "color"):
        if k in body:
            setattr(s, k, body[k])
    if "rate_per_hour" in body:
        s.rate_from_assumptions = False  # a typed-in rate overrides the build-up
    elif s.rate_from_assumptions:
        A.recompute_rates(db)
    db.commit()
    return S.stage(s)


class OperationIn(BaseModel):
    stage_id: int
    sequence: int
    setup_minutes: float = 0.0
    run_minutes_per_unit: float = 0.0


class ComponentIn(BaseModel):
    item_id: int
    qty_per: float = Field(gt=0)
    scrap_pct: float = Field(0.0, ge=0)
    operation_seq: int | None = None


class RecipeIn(BaseModel):
    item_id: int
    version: str = "1"
    output_qty: float = Field(1.0, gt=0)
    std_batch_qty: float | None = Field(None, gt=0)
    active: bool = True
    notes: str = ""
    operations: list[OperationIn]
    components: list[ComponentIn]


def _apply_recipe(db: Session, r: Recipe, body: RecipeIn):
    seqs = {o.sequence for o in body.operations}
    if not body.operations:
        raise HTTPException(422, "A recipe needs at least one operation (stage)")
    for c in body.components:
        if c.operation_seq is not None and c.operation_seq not in seqs:
            raise HTTPException(422, f"Component consumed at operation {c.operation_seq}, which doesn't exist")
        if c.item_id == body.item_id:
            raise HTTPException(422, "A recipe can't consume its own output")
    r.item_id, r.version, r.output_qty, r.std_batch_qty, r.active, r.notes = (
        body.item_id, body.version, body.output_qty, body.std_batch_qty, body.active, body.notes)
    r.operations = [RecipeOperation(**o.model_dump()) for o in body.operations]
    r.components = [RecipeComponent(**c.model_dump()) for c in body.components]
    if body.active:
        for other in db.scalars(select(Recipe).where(Recipe.item_id == body.item_id, Recipe.id != r.id)):
            other.active = False
    db.flush()
    recipes = {x.item_id: x for x in db.scalars(select(Recipe).where(Recipe.active.is_(True)))}
    try:
        _low_level_codes(recipes)
    except RecipeCycleError as e:
        db.rollback()
        raise HTTPException(422, str(e))
    item = db.get(Item, body.item_id)
    if item.item_type == "raw":
        item.item_type = "sub"


@router.get("/recipes")
def list_recipes(db: Session = Depends(get_db)):
    active, settings = _active(db), A.get_settings(db)
    return [S.recipe(r, active, settings) for r in db.scalars(select(Recipe).order_by(Recipe.id))]


@router.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: int, db: Session = Depends(get_db)):
    r = db.get(Recipe, recipe_id)
    if not r:
        _404()
    return S.recipe(r, _active(db), A.get_settings(db))


@router.post("/recipes")
def create_recipe(body: RecipeIn, db: Session = Depends(get_db)):
    r = Recipe()
    db.add(r)
    _apply_recipe(db, r, body)
    db.commit()
    return S.recipe(r, _active(db), A.get_settings(db))


@router.put("/recipes/{recipe_id}")
def replace_recipe(recipe_id: int, body: RecipeIn, db: Session = Depends(get_db)):
    r = db.get(Recipe, recipe_id)
    if not r:
        _404()
    _apply_recipe(db, r, body)
    db.commit()
    return S.recipe(r, _active(db), A.get_settings(db))


@router.get("/assumptions")
def get_assumptions(db: Session = Depends(get_db)):
    return A.summary(db)


@router.patch("/assumptions/settings")
def update_settings(body: dict[str, float], db: Session = Depends(get_db)):
    try:
        A.set_settings(db, body)
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return A.summary(db)


@router.patch("/stages/{stage_id}/assumptions")
def update_stage_assumptions(stage_id: int, body: dict, db: Session = Depends(get_db)):
    s = db.get(Stage, stage_id)
    if not s:
        _404()
    for k, v in body.items():
        if k not in A.ASSUMPTION_FIELDS:
            raise HTTPException(422, f"Unknown assumption {k}")
        if k != "assumption_notes" and (not isinstance(v, (int, float)) or v < 0):
            raise HTTPException(422, f"{k} must be a number of 0 or more")
    A.apply(db, s, body)
    db.commit()
    return A.summary(db)


def _404():
    raise HTTPException(404, "Not found")
