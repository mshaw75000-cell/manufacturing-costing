# Manufacturing Costing

A shop-floor costing and tracking tool designed to replace the manufacturing
module in Dynamics 365 Business Central (BC). BC stays the system of record for
items, inventory value and sales. This tool handles everything from raw
material to costed finished goods:

1. **Demand.** Pulls open sales-order lines from BC, or takes them as manual input.
2. **Recipes.** Each recipe is a bill of materials plus a routing: which
   components a product uses, which stages it goes through (cut, press, weld,
   powder coat, assembly...), and at which stage each component is consumed.
   Recipes can be multi-level (sub-assemblies).
3. **Material plan (MRP).** Explodes demand through the recipes, nets it against
   stock on hand and open jobs, and lists what to **make** and which raw
   materials to **buy**, with quantities, need-by dates and shortfall value.
4. **Jobs and the live shop floor.** Jobs move unit quantities stage by stage
   (*waiting → in process → done*, or *scrapped*). The board updates in real time
   in every open browser and shows which material is at which stage.
5. **Consumption.** Components are backflushed when units start at the stage
   that uses them. Local stock drops immediately and a consumption posting is
   queued for BC.
6. **Costing and output.** A job's cost is the material issued (at BC unit cost)
   plus time at each stage multiplied by the stage's hourly rate (labour +
   overhead). Scrap is absorbed by the good units. Finishing a job queues an
   output posting to BC at the computed unit cost, so the units go into
   finished goods inventory.

```
 BC items/stock/costs ─┐                                  ┌─► BC item journal
 BC sales demand ──────┼─► Demand ─► MRP plan ─► Jobs ─────┤   (consumption −,
 Manual demand ────────┘                (make/buy)  │      │    output + @ cost)
                                                    ▼      │
                               Cut → Press → Weld → Powder coat → Assembly
                               (live board: waiting / in process / done / scrap,
                                material at each stage, cost accumulating)
```

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app.seed                 # demo stages, items, recipes, BC demand (mock BC)
uvicorn app.main:app --reload      # http://localhost:8000
pytest                             # tests
```

By default it runs against a **mock BC** (`bc_mock.json`), so you can try the
whole cycle without touching your tenant. Demo walk-through:

1. *Demand & plan*: SO-1001 needs 24 frames. The plan shows 24 frames to make,
   100 brackets to make (5 sheets × 20 per sheet) and the raw materials required.
   Click **Create jobs for all "make" lines**.
2. *Jobs*: open each job and click **Release to floor**.
3. *Shop floor*: click a card and use **Start** / **Complete** / **Scrap**. Open the
   board in a second window to watch it update live.
4. When the last stage is complete, click **Finish job**. The unit cost is frozen
   and the output is queued.
5. *Business Central*: **Send pending postings**, then **Pull items & stock** to
   see BC's inventory and cost updated.

## Connecting to the real Business Central

Set `BC_MODE=live` and the following (see `.env.example`):

| Variable | Meaning |
|---|---|
| `BC_TENANT_ID`, `BC_CLIENT_ID`, `BC_CLIENT_SECRET` | Entra ID app registration (client credentials). Add the app in BC under *Microsoft Entra Applications* with permissions to read items/sales orders and write item journals. |
| `BC_ENVIRONMENT` | e.g. `Production` or `Sandbox` |
| `BC_COMPANY_ID`, `BC_COMPANY_NAME` | Company GUID (API v2.0) and name (OData) |
| `BC_ITEM_JOURNAL_SERVICE` | Name you publish page 40 *Item Journal* under on the *Web Services* page (default `ItemJournal`) |
| `BC_ITEM_JOURNAL_TEMPLATE` / `BC_ITEM_JOURNAL_BATCH` | Journal to write to (default `ITEM` / `MFGCOST`) |
| `BC_LOCATION_CODE` | Optional location for movements |

What it reads and writes:

- **Reads** use standard API v2.0: `items` (number, description, base UoM,
  unit cost, inventory) and `salesOrders` with `salesOrderLines` (outstanding
  quantity, shipment date).
- **Writes** create item journal lines: consumption as *Negative Adjmt.*,
  finished output as *Positive Adjmt.* with *Unit Amount* set to the job's unit
  cost. Lines are created in the journal batch. **Posting the batch is not yet
  automated**: post it in BC, run it from the job queue, or add a small AL
  extension that exposes a post action (recommended next step, see roadmap).
- Postings go through an **outbox** (`bc_postings` table). The shop floor never
  waits on BC, failures are kept and retried, and nothing is lost if BC is
  offline. When stock is pulled from BC, movements still in the outbox are
  re-applied so on-hand quantities don't jump back.

> The live client is written against BC's documented APIs but has not yet been
> run against your tenant. Expect some field-name adjustments on first connection,
> especially the item journal page fields, which depend on how page 40 is published.

## Costing rules (current)

- Material is valued at the item's BC unit cost **at the time of issue**.
- Conversion cost per stage = (setup minutes, charged once when the stage first
  starts, + run minutes) / 60 × stage rate per hour. Operators can enter actual
  minutes on **Complete**; if they leave it blank, standard run minutes are used.
- Unit cost = (material + conversion) / good units. Scrap cost is spread over
  the good output.
- A recipe's **standard cost** rolls sub-assemblies up through their own recipes,
  for comparison with actual cost.

## Project layout

```
app/
  models.py              data model (items, stages, recipes, demand, jobs, events, BC outbox)
  services/planning.py   MRP explosion and netting
  services/production.py job flow, backflush, costing, finish
  bc/                    BC adapters (mock + live) and sync/outbox
  api/                   REST endpoints + SSE stream (/api/stream)
  seed.py                demo data
static/                  web UI (no build step)
tests/                   end-to-end tests of plan, flow, costing and BC round trip
```

Database: SQLite by default; set `DATABASE_URL` (e.g. `postgresql+psycopg://...`)
for production.

## Roadmap / next steps

- **BC posting automation**: an AL extension with an API page and action to post the
  MFGCOST batch, or direct item ledger posting via custom API.
- **Users and roles**: sign-in (Entra ID), with operator, supervisor and planner roles.
  Tablet-friendly operator screen per stage, with barcode/QR scan of job travellers.
- **Partial output posting**: post finished units to BC as they come off the line
  rather than at job close.
- **Lot / heat traceability** for steel and powder batches, and location of WIP
  (racks, ovens).
- **Capacity and scheduling**: stage capacity, queue time, due-date risk.
- **Variance reporting**: standard vs actual by job, stage and material; scrap reasons.
- **Purchasing**: turn "buy" lines into BC purchase requisitions or purchase orders.
- **Schema migrations** (Alembic) before first production deployment.
