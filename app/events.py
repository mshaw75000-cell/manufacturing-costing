"""In-process pub/sub that pushes shop-floor changes to browsers over SSE.

Route handlers run in a thread pool, so publishing hands the message to the
event loop thread-safely. For several app instances behind a load balancer,
swap this for Redis pub/sub or Postgres LISTEN/NOTIFY.
"""
import asyncio
import json


class Broker:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    def _deliver(self, message: str):
        for q in list(self._subscribers):
            if q.full():
                q.get_nowait()  # drop oldest for slow clients
            q.put_nowait(message)

    def publish(self, topic: str, data: dict | None = None):
        message = json.dumps({"topic": topic, "data": data or {}}, default=str)
        if self._loop is None or self._loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._deliver(message)
        else:
            self._loop.call_soon_threadsafe(self._deliver, message)


broker = Broker()
