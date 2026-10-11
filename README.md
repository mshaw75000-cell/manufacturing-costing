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
   in every open browser and shows which material, lot and heat is at which stage.
5. **Consumption.** Components are backflushed when units start at the stage
   that uses them, drawn from lots (the operator's chosen lot, otherwise oldest
   first). Local stock drops immediately and a consumption posting is queued for BC.
6. **Output as completed.** As soon as units complete the last stage, they go
   into stock and an output posting is queued for BC at the item's **standard
   cost**, into the job's output lot.
7. **Standard costing and variances.** Actual cost is material at standard plus
   time at each stage multiplied by its hourly rate. Closing the job freezes
   actual cost and the variance against what BC booked, broken down by
   material, stage and standard.
8. **Lot / heat traceability.** Received lots carry heat number, supplier and
   mill cert. Every issue records its lot, and every job's output is a lot, so
   you can trace a finished lot back to the steel heat, or a heat forward to
   every finished lot it went into.

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
   On the cutting stage, pick which steel lot / heat to use. Finished units
   post to BC as they complete.
4. When every unit is through, click **Close job** to freeze actual cost and
   variances. Open the job to see the variance breakdown.
5. *Lots & trace*: trace the frame's lot back to the steel heat and powder
   batch, or search a heat number and trace it forward.
6. *Business Central*: **Send pending postings**, then **Pull items & stock** to
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
  finished output as *Positive Adjmt.*, one line per completion. Items are on
  **Standard** costing, so no amount is sent: BC values both at standard cost.
  Lines are created in the journal batch. **Posting the batch is not yet
  automated**: post it in BC, run it from the job queue, or add a small AL
  extension that exposes a post action (recommended next step, see roadmap).
- Postings go through an **outbox** (`bc_postings` table). The shop floor never
  waits on BC, failures are kept and retried, and nothing is lost if BC is
  offline. When stock is pulled from BC, movements still in the outbox are
  re-applied so on-hand quantities don't jump back.

### Lots in BC

Lot and heat numbers are recorded in this tool and not yet sent to BC. If the
same items have lot tracking switched on in BC (an item tracking code with lot
tracking), BC won't post the journal lines without item tracking lines, and
page 40 can't create those. There are two options to decide on:

1. **Keep lot tracking off in BC** for these items. This tool is the
   traceability record. Simplest, and it works today.
2. **Turn it on in BC** and add a small AL extension that accepts a lot no. per
   journal line and creates the reservation / tracking entry. BC then shows
   lots too. The `lot_no` is already stored on every posting, ready for this.

> The live client is written against BC's documented APIs but has not yet been
> run against your tenant. Expect some field-name adjustments on first connection,
> especially the item journal page fields, which depend on how page 40 is published.

## Costing rules (standard costing)

- Items are on BC's **Standard** costing method; the BC unit cost is the standard.
- Material is issued at the component's standard cost; finished units go into
  stock (and BC) at the output item's standard cost as they complete.
- Actual conversion cost per stage = (setup minutes, charged once when the
  stage first starts, + run minutes) / 60 × stage rate per hour. Operators can
  enter actual minutes on **Complete**; if they leave it blank, standard minutes are used.
- **Variance** = actual cost − value booked into stock at BC standard, split into:
  - *material usage*: (actual qty − standard qty for good output) × standard cost.
    Scrap and over-use show up here.
  - *labour & overhead*: actual stage cost − standard stage cost for good output.
  - *recipe vs BC standard*: the recipe's standard compared with BC's. A
    non-zero figure means the BC standard is out of date with the recipe.
  Positive = unfavourable.
- A recipe's **standard cost** uses each component's BC standard (rolling up
  through the sub-assembly recipe if BC has none) plus standard stage time, with
  setup spread over the recipe's *standard run size*.
- **WIP value** = actual cost put into open jobs − output already in stock at standard.
- Variances are calculated and shown here; they are **not posted to the GL**.
  How variances should hit the GL (and to which accounts) is a decision for
  your accountant. Once that's agreed, it can be added as general journal
  postings.

## Project layout

```
app/
  models.py              data model (items, stages, recipes, demand, jobs, events, BC outbox)
  services/planning.py   MRP explosion and netting
  services/production.py job flow, lot allocation, backflush, output, close
  services/costing.py    standard cost roll-up, actual cost, variances
  services/trace.py      lot genealogy (backward / forward)
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
- **Lots in BC**: decide option 1 or 2 under "Lots in BC"; pull lots from BC receipts.
- **Variance GL posting** once account mapping is agreed.
- **Location of WIP** (racks, ovens) and lot labels / QR codes.
- **Capacity and scheduling**: stage capacity, queue time, due-date risk.
- **Variance reporting**: standard vs actual by job, stage and material; scrap reasons.
- **Purchasing**: turn "buy" lines into BC purchase requisitions or purchase orders.
- **Schema migrations** (Alembic) before first production deployment.
