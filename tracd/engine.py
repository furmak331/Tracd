"""The checker: polls every watched size, decides what changed, sends alerts.

Behaviour (kept from the original GEL-NYC tracker, which got these right):
  * A size is only "in stock" when the shop's own data says so. Anything
    unreadable is UNKNOWN or BLOCKED and never triggers a restock alert.
  * On a restock you get an urgent push at once, then reminders every
    `repeat_every` seconds (up to `repeat_alerts`) until you tap "Got it" or it
    sells out again. The page also opens on this computer and it beeps.
  * When a shop stays unreadable for `blind_after` seconds you are told, so a
    silent tracker never looks like a quiet one.
New here:
  * Every shop is checked in parallel, so a slow shop never delays another.
  * Two sizes of the same item cost one request when the shop lists all sizes
    on one page.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import webbrowser
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable

from .models import Reading, ShopError, Stock
from .notify import Alert, Notifier, beep
from .shops import BY_KEY, GENERIC
from .shops.base import host_of
from .store import Store, Watch

log = logging.getLogger("tracd")

PER_HOST = 2  # at most this many requests in flight to one shop


@dataclass
class Runtime:
    state: Stock | None = None  # last definite answer
    next_due: float = 0.0
    alerts_sent: int = 0
    next_alert_at: float = 0.0
    acked: bool = False
    unknown_streak: int = 0
    unknown_since: float | None = None
    blind_alerted: bool = False


def rupees(v: float | None) -> str:
    return f"₹{int(round(v)):,}" if v else ""


class Engine:
    def __init__(
        self,
        store: Store,
        fetcher: Any,
        notifier: Notifier,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        jitter: float = 0.2,
        opener: Callable[[str], Any] = webbrowser.open,
        beeper: Callable[[], None] = beep,
        load_timeout: float = 60.0,
    ):
        self.store = store
        self.fetcher = fetcher
        self.notifier = notifier
        self.clock = clock
        self.wall = wall
        self.jitter = jitter
        self.opener = opener
        self.beeper = beeper
        self.load_timeout = load_timeout
        self.rt: dict[int, Runtime] = {}
        self.logs: deque[dict] = deque(maxlen=300)
        self.listeners: set[asyncio.Queue] = set()
        self.alert_problem: str | None = None
        self._busy: set[tuple[str, str]] = set()
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._wake = asyncio.Event()
        self._running = False
        self._tasks: set[asyncio.Task] = set()
        self._group_done: dict[tuple[str, str], asyncio.Event] = {}

    # -- plumbing ---------------------------------------------------------------

    def log(self, msg: str, level: str = "info", watch_id: int | None = None) -> None:
        entry = {"at": self.wall(), "msg": msg, "level": level, "watch_id": watch_id}
        self.logs.append(entry)
        (log.warning if level == "warn" else log.info)(msg)
        self.publish({"type": "log", "log": entry})

    def publish(self, event: dict) -> None:
        for q in list(self.listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.listeners.discard(q)

    def wake(self) -> None:
        self._wake.set()

    def _rt(self, watch_id: int) -> Runtime:
        return self.rt.setdefault(watch_id, Runtime())

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def _sem(self, host: str) -> asyncio.Semaphore:
        return self._sems.setdefault(host, asyncio.Semaphore(PER_HOST))

    def watch_view(self, w: Watch) -> dict:
        d = w.to_dict()
        rt = self._rt(w.id)
        d["shop_name"] = BY_KEY.get(w.shop, GENERIC).name
        d["shop_color"] = BY_KEY.get(w.shop, GENERIC).color
        d["alerts_sent"] = rt.alerts_sent
        d["acked"] = rt.acked
        d["checking"] = (w.shop, w.poll_url) in self._busy
        d["next_check_in"] = max(0.0, rt.next_due - self.clock()) if w.enabled else None
        return d

    # -- the loop -------------------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        while self._running:
            now = self.clock()
            groups: dict[tuple[str, str], list[Watch]] = defaultdict(list)
            for w in self.store.watches(enabled_only=True):
                groups[(w.shop, w.poll_url)].append(w)
            soonest = now + 1.0
            for key, ws in groups.items():
                if key in self._busy:
                    continue
                due = min(self._rt(w.id).next_due for w in ws)
                if due <= now:
                    self._start_poll(key, ws)
                else:
                    soonest = min(soonest, due)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.02, soonest - self.clock()))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def stop(self) -> None:
        self._running = False
        self.wake()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _start_poll(self, key: tuple[str, str], ws: list[Watch]) -> asyncio.Event:
        self._busy.add(key)
        done = asyncio.Event()
        self._group_done[key] = done
        for w in ws:
            self.publish({"type": "watch", "watch": self.watch_view(w)})
        self._spawn(self._poll_group(key, ws, done))
        return done

    async def check_now(self, watch_id: int) -> Watch | None:
        """Check one watch right away and return it once the answer is in."""
        w = self.store.watch(watch_id)
        if w is None:
            return None
        key = (w.shop, w.poll_url)
        if key in self._busy:
            await self._group_done[key].wait()
        else:
            ws = [x for x in self.store.watches(enabled_only=True) if (x.shop, x.poll_url) == key] or [w]
            await self._start_poll(key, ws).wait()
        return self.store.watch(watch_id)

    async def _poll_group(self, key: tuple[str, str], ws: list[Watch], done: asyncio.Event) -> None:
        shop_key, url = key
        shop = BY_KEY.get(shop_key, GENERIC)
        t0 = self.clock()
        try:
            async with self._sem(host_of(url)):
                try:
                    product = await asyncio.wait_for(shop.load(self.fetcher, url), timeout=self.load_timeout)
                    readings = {w.id: shop.reading(product, w.size_id, w.size_label) for w in ws}
                except ShopError as e:
                    readings = {w.id: Reading(e.state, e.detail) for w in ws}
                except asyncio.TimeoutError:
                    readings = {w.id: Reading(Stock.UNKNOWN, "the shop took too long to answer") for w in ws}
                except Exception as e:  # noqa: BLE001 - a bug in a reader must not stop the tracker
                    detail = f"{e.__class__.__name__}: {e}"[:200]
                    readings = {w.id: Reading(Stock.UNKNOWN, detail) for w in ws}
            ms = int((self.clock() - t0) * 1000)
            self._busy.discard(key)
            for w in ws:
                await self._handle(w.id, readings[w.id], ms)
        finally:
            self._busy.discard(key)
            done.set()
            self.wake()

    # -- deciding ---------------------------------------------------------------------

    def _next(self, interval: float) -> float:
        j = self.jitter
        return self.clock() + interval * (random.uniform(1 - j, 1 + j) if j else 1)

    async def _handle(self, watch_id: int, r: Reading, ms: int) -> None:
        w = self.store.watch(watch_id)
        if w is None or not w.enabled:
            return
        s = self.store.settings()
        rt = self._rt(w.id)
        now, t = self.clock(), self.wall()
        shop = BY_KEY.get(w.shop, GENERIC)
        upd: dict[str, Any] = {
            "state": r.state.value, "detail": r.detail, "last_checked": t, "checks": w.checks + 1, "last_ms": ms,
        }
        if r.price:
            upd["price"] = r.price
        if r.state.definite:
            upd["qty"] = r.qty
            if rt.blind_alerted:
                self.log(f"{shop.name}: reading {w.size_label} again", watch_id=w.id)
                self._send(Alert(f"Reading {shop.name} again", f"{_short(w.title)} {w.size_label}: back to normal checks.",
                                 url=w.open_url, button=f"Open on {shop.name}"))
            rt.unknown_streak, rt.unknown_since, rt.blind_alerted = 0, None, False

        if r.state == Stock.IN_STOCK:
            if rt.state != Stock.IN_STOCK:
                rt.state, rt.alerts_sent, rt.next_alert_at, rt.acked = Stock.IN_STOCK, 0, 0.0, False
                upd["last_change"] = upd["last_in_stock"] = t
                self.log(f"{w.size_label} IN STOCK on {shop.name}" + (f" at {rupees(r.price)}" if r.price else "")
                         + f": {_short(w.title)}", "hit", w.id)
                if s.get("beep"):
                    self.beeper()
                if s.get("auto_open"):
                    self._spawn(asyncio.to_thread(self.opener, w.open_url))
                self.publish({"type": "hit", "watch_id": w.id})
            limit = max(1, int(s.get("repeat_alerts") or 1))
            if not rt.acked and rt.alerts_sent < limit and now >= rt.next_alert_at:
                rt.alerts_sent += 1
                rt.next_alert_at = now + float(s.get("repeat_every") or 20)
                price = f" at {rupees(r.price)}" if r.price else ""
                left = f" Only {r.qty} left." if r.qty and r.qty <= 10 else ""
                again = "" if rt.alerts_sent == 1 else f" (reminder {rt.alerts_sent}/{limit})"
                self._send(Alert(
                    f"{w.size_label} in stock on {shop.name}",
                    f"{_short(w.title)} is buyable{price}.{left} Tap to open.{again}",
                    url=w.open_url, image=w.image, urgent=True, button=f"Open on {shop.name}",
                ))
            more = not rt.acked and rt.alerts_sent < limit
            rt.next_due = self.clock() + min(w.interval, float(s.get("repeat_every") or 20)) if more else self._next(w.interval)

        elif r.state == Stock.OUT:
            if rt.state == Stock.IN_STOCK:
                upd["last_change"] = t
                self.log(f"{w.size_label} sold out again on {shop.name}", "info", w.id)
                if s.get("notify_sold_out_again"):
                    self._send(Alert(f"{w.size_label} sold out again on {shop.name}",
                                     f"{_short(w.title)} went back out of stock. Still watching.", url=w.open_url,
                                     button=f"Open on {shop.name}"))
            elif rt.state is None:
                self.log(f"Watching {w.size_label} on {shop.name}: sold out for now", "info", w.id)
            rt.state = Stock.OUT
            rt.next_due = self._next(w.interval)

        else:
            rt.unknown_streak += 1
            if rt.unknown_since is None:
                rt.unknown_since = now
            if rt.unknown_streak in (1, 2) or rt.unknown_streak % 10 == 0:
                what = "verification page" if r.state == Stock.BLOCKED else "can't read stock"
                self.log(f"{shop.name}: {what} ({r.detail}), retrying", "warn", w.id)
            refresh = getattr(self.fetcher, "refresh", None)
            if refresh and rt.unknown_streak % 3 == 2:
                self._spawn(_quiet(refresh(w.poll_url)))
            base = 30 if r.state == Stock.BLOCKED else w.interval
            cap = 300 if r.state == Stock.BLOCKED else 120
            rt.next_due = self.clock() + min(cap, base * 2 ** min(rt.unknown_streak - 1, 5))
            if not rt.blind_alerted and now - rt.unknown_since >= float(s.get("blind_after") or 180):
                rt.blind_alerted = True
                why = ("it is showing a verification page. Open the dashboard and tap 'Open shop window' to clear it"
                       if r.state == Stock.BLOCKED else r.detail)
                self._send(Alert(f"Can't read {shop.name} right now",
                                 f"Not able to watch {w.size_label} of {_short(w.title)}: {why}.", url=w.open_url,
                                 button=f"Open on {shop.name}"))

        if w.state != r.state.value:
            self.store.add_event(w.id, r.state.value, r.detail, r.price, t)
        w2 = self.store.update_watch(w.id, **upd)
        if w2:
            self.publish({"type": "watch", "watch": self.watch_view(w2)})

    def _send(self, alert: Alert) -> None:
        async def go() -> None:
            res = await self.notifier.send(alert)
            if not res:
                self.alert_problem = "No phone alert channel is set up."
            else:
                bad = {k: v for k, v in res.items() if v}
                self.alert_problem = ("; ".join(f"{k}: {v}" for k, v in bad.items())) if len(bad) == len(res) else None
            self.publish({"type": "alert_status", "problem": self.alert_problem})

        self._spawn(go())

    # -- user actions ---------------------------------------------------------------

    def ack(self, watch_id: int) -> None:
        rt = self._rt(watch_id)
        rt.acked = True
        w = self.store.watch(watch_id)
        if w:
            rt.next_due = self._next(w.interval)
            self.publish({"type": "watch", "watch": self.watch_view(w)})

    def forget(self, watch_id: int) -> None:
        self.rt.pop(watch_id, None)

    def reschedule(self, watch_id: int) -> None:
        self._rt(watch_id).next_due = 0.0
        self.wake()


def _short(title: str, n: int = 60) -> str:
    title = " ".join(str(title).split())
    return title if len(title) <= n else title[: n - 1].rstrip() + "…"


async def _quiet(coro) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001
        pass
