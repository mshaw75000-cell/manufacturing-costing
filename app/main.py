import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import bc, lots, master, ops, planning, purchasing, shopfloor
from .db import Base, engine
from .events import broker

STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(engine)
    broker.bind_loop(asyncio.get_running_loop())
    yield


app = FastAPI(title="Manufacturing Costing", lifespan=lifespan)
for r in (master.router, planning.router, shopfloor.router, lots.router, ops.router,
          purchasing.router, bc.router):
    app.include_router(r)
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
