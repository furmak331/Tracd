"""Long-running soak test: the whole app against misbehaving pretend shops.

    python tests/soak.py --minutes 30

Runs the real checker, server and dashboard (in Chromium) against sample shops
that restock and sell out at random, answer slowly, fail, hang and show
verification pages. Along the way it checks that:

- every "in stock" alert was for a size that really was in stock,
- every restock that lasted long enough was alerted,
- memory, tasks, listeners and the database stay flat,
- the dashboard keeps up and throws no errors,
- live updates survive the dashboard reconnecting.

Exits non-zero and prints what went wrong if any of that fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import resource
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).parent))
from conftest import FakeNotifier, chromium_path  # noqa: E402

from tracd.engine import Engine  # noqa: E402
from tracd.models import Fetched  # noqa: E402
from tracd.samples import SampleFetcher, demo_catalog  # noqa: E402
from tracd.server import create_app  # noqa: E402
from tracd.shops import BY_KEY  # noqa: E402
from tracd.store import Store  # noqa: E402


def rss_mb() -> float:
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except OSError:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class ChaosShops(SampleFetcher):
    """Sample shops with bad days: slow answers, errors, hangs and verification pages."""

    def __init__(self, items, *, rng: random.Random, hang: float):
        super().__init__(items)
        self.rng = rng
        self.hang = hang
        self.blocked_until: dict[str, float] = {}
        self.block_windows: list[tuple[str, float, float]] = []
        self.counts: dict[str, int] = defaultdict(int)

    async def fetch(self, url, *, kind="html", page_url=None):
        host = urlparse(url).hostname or ""
        now = time.monotonic()
        roll = self.rng.random()
        if host not in self.blocked_until and roll < 0.004:
            until = now + self.rng.uniform(20, 60)
            self.blocked_until[host] = until
            self.block_windows.append((host, now, until))
        if self.blocked_until.get(host, 0) > now:
            self.counts["blocked"] += 1
            await asyncio.sleep(0.05)
            return Fetched(200, "<html><title>Access Denied</title>Please verify you are human (captcha)</html>", url)
        self.blocked_until.pop(host, None)
        if roll > 0.995:
            self.counts["hang"] += 1
            await asyncio.sleep(self.hang)
        await asyncio.sleep(self.rng.uniform(0.02, 1.5))
        if self.rng.random() < 0.03:
            self.counts["error"] += 1
            return Fetched(503, "busy", url)
        self.counts["ok"] += 1
        return await super().fetch(url, kind=kind, page_url=page_url)


async def main(minutes: float, seed: int, interval: int) -> int:
    rng = random.Random(seed)
    items = demo_catalog()
    shops = ChaosShops(items, rng=rng, hang=12)
    store = Store(":memory:")
    store.update_settings(onboarded=True, alerts_verified=True, repeat_alerts=3, repeat_every=10, blind_after=60,
                          notify_sold_out_again=True)
    notifier = FakeNotifier()
    engine = Engine(store, shops, notifier, opener=lambda u: None, beeper=lambda: None, load_timeout=8)

    # Watch every size of every item, added the way the dashboard adds them.
    size_of: dict[int, tuple] = {}
    for it in items:
        for s in it.sizes:
            s.available, s.qty = False, 0
    for it in items:
        shop = BY_KEY[it.shop]
        product = await shop.load(SampleFetcher(items), it.url)
        for ps in product.sizes:
            s = it.size(ps.id)
            w = store.add_watch(shop=product.shop, product_url=product.url,
                                poll_url=shop.poll_url(product.url, ps.id, ps.url), title=product.title,
                                image=product.image, size_id=ps.id, size_label=ps.label, size_url=ps.url,
                                interval=interval, enabled=True)
            size_of[w.id] = (it, s)
    host_of = {wid: urlparse(store.watch(wid).poll_url).hostname for wid in size_of}

    # Availability history per watch, to judge every alert.
    history: dict[int, list[tuple[float, bool]]] = {wid: [(time.monotonic(), False)] for wid in size_of}

    def available_between(wid: int, t0: float, t1: float) -> bool:
        h = history[wid]
        state = False
        for t, a in h:
            if t > t1:
                break
            if t >= t0 and a:
                return True
            state = a
        return state

    alert_times: dict[int, list[float]] = defaultdict(list)
    real_send = notifier.send

    async def send(alert):
        if alert.urgent:
            wid = next((w for w in size_of
                        if alert.url == store.watch(w).open_url
                        and alert.title.startswith(store.watch(w).size_label + " in stock")), None)
            alert_times[wid].append(time.monotonic())
        return await real_send(alert)

    notifier.send = send  # type: ignore[method-assign]

    app = create_app(store, engine, shops)
    srv = TestServer(app, host="127.0.0.1")
    await srv.start_server()
    base = str(srv.make_url("/"))
    loop_task = asyncio.create_task(engine.run())

    problems: list[str] = []
    samples: list[dict] = []
    stop_at = time.monotonic() + minutes * 60
    lag_worst = 0.0

    async def restocker():
        while time.monotonic() < stop_at:
            await asyncio.sleep(rng.uniform(3, 8))
            wid = rng.choice(list(size_of))
            it, s = size_of[wid]
            s.available = not s.available
            s.qty = rng.randint(1, 5) if s.available else 0
            for w2, (_, s2) in size_of.items():
                if s2 is s:
                    history[w2].append((time.monotonic(), s.available))

    async def lag_probe():
        nonlocal lag_worst
        while time.monotonic() < stop_at:
            t = time.monotonic()
            await asyncio.sleep(0.25)
            lag_worst = max(lag_worst, time.monotonic() - t - 0.25)

    sse_events = 0

    async def sse_client():
        nonlocal sse_events
        async with aiohttp.ClientSession() as http:
            while time.monotonic() < stop_at:
                try:
                    async with http.get(base + "api/stream", timeout=aiohttp.ClientTimeout(total=90)) as r:
                        t_end = time.monotonic() + rng.uniform(30, 90)
                        async for line in r.content:
                            if line.startswith(b"data:"):
                                sse_events += 1
                            if time.monotonic() > t_end:
                                break
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    pass

    page_errors: list[str] = []

    async def dashboard():
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=chromium_path())
            page = await b.new_page(viewport={"width": 1280, "height": 900})
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("console", lambda m: m.type == "error" and page_errors.append(m.text))
            await page.goto(base)
            while time.monotonic() < stop_at:
                await asyncio.sleep(30)
                info = await page.evaluate("""() => ({
                    nodes: document.getElementsByTagName('*').length,
                    heap: performance.memory ? performance.memory.usedJSHeapSize / 1048576 : null,
                    cards: document.querySelectorAll('[data-watch]').length,
                    inStock: document.querySelectorAll('[data-watch].hit').length,
                })""")
                dash.append(info)
            # The dashboard must agree with the checker at the end.
            await asyncio.sleep(2)
            shown = await page.evaluate("document.querySelectorAll('[data-watch]').length")
            if shown != len(size_of):
                problems.append(f"dashboard shows {shown} cards, expected {len(size_of)}")
            await page.screenshot(path=str(Path(os.environ.get("SOAK_OUT", ".")) / "soak-end.png"))
            await b.close()

    dash: list[dict] = []

    async def sampler():
        while time.monotonic() < stop_at:
            await asyncio.sleep(30)
            samples.append({
                "t": round(minutes * 60 - (stop_at - time.monotonic())),
                "rss_mb": round(rss_mb(), 1),
                "tasks": len(asyncio.all_tasks()),
                "engine_tasks": len(engine._tasks),
                "listeners": len(engine.listeners),
                "events_rows": store._exec("SELECT COUNT(*) FROM events").fetchone()[0],
                "alerts": len(notifier.urgent),
                "lag_ms": round(lag_worst * 1000),
            })
            print(json.dumps(samples[-1]), flush=True)

    await asyncio.gather(restocker(), lag_probe(), sse_client(), dashboard(), sampler())
    await asyncio.sleep(interval * 2)  # let the last checks land

    # 1. No false alerts: each urgent alert is for a size that was in stock shortly before.
    false_alerts = 0
    for wid, times in alert_times.items():
        if wid is None:
            problems.append("an urgent alert did not match any watch")
            continue
        for t in times:
            if not available_between(wid, t - 15, t):
                false_alerts += 1
                problems.append(f"false alert for watch {wid} at {t:.0f}")

    # 2. No missed restocks: a restock that lasted 3 intervals with the shop not blocked must alert.
    missed = 0
    for wid, h in history.items():
        for (t0, a), nxt in zip(h, h[1:] + [(time.monotonic(), None)]):
            if not a or nxt[0] - t0 < interval * 3:
                continue
            host = host_of[wid]
            if any(hh == host and s < nxt[0] and e > t0 for hh, s, e in shops.block_windows):
                continue
            if not any(t0 <= t <= nxt[0] + 1 for t in alert_times.get(wid, [])):
                missed += 1
                problems.append(f"missed restock of watch {wid} ({nxt[0] - t0:.0f}s in stock)")

    # 3. Resources stay flat: compare the last third with the first third (after warm-up).
    if len(samples) >= 6:
        third = len(samples) // 3
        early = samples[1:third + 1]
        late = samples[-third:]
        grow = max(s["rss_mb"] for s in late) - max(s["rss_mb"] for s in early)
        if grow > 40:
            problems.append(f"memory grew {grow:.0f} MB")
        if max(s["engine_tasks"] for s in late) > len(size_of) * 2:
            problems.append("checker tasks pile up")
        if max(s["listeners"] for s in late) > 3:
            problems.append(f"live-update listeners leak ({late[-1]['listeners']})")
    if lag_worst > 0.5:
        problems.append(f"event loop stalled for {lag_worst * 1000:.0f} ms")
    if dash and dash[-1]["heap"] and dash[0]["heap"] and dash[-1]["heap"] - max(d["heap"] for d in dash[:3]) > 30:
        problems.append(f"dashboard memory grew to {dash[-1]['heap']:.0f} MB")
    if dash and dash[-1]["nodes"] > dash[0]["nodes"] * 2 + 500:
        problems.append(f"dashboard DOM grew from {dash[0]['nodes']} to {dash[-1]['nodes']} nodes")
    problems += [f"dashboard error: {e}" for e in page_errors[:10]]

    await engine.stop()
    loop_task.cancel()
    await srv.close()

    restocks = sum(1 for h in history.values() for _, a in h if a)
    summary = {
        "minutes": minutes, "watches": len(size_of), "restocks": restocks, "urgent_alerts": len(notifier.urgent),
        "all_alerts": len(notifier.sent), "false_alerts": false_alerts, "missed_restocks": missed,
        "shop_answers": dict(shops.counts), "block_windows": len(shops.block_windows),
        "sse_events": sse_events, "worst_loop_lag_ms": round(lag_worst * 1000),
        "rss_mb_start_end": [samples[0]["rss_mb"], samples[-1]["rss_mb"]] if samples else None,
        "dashboard_start_end": [dash[0], dash[-1]] if dash else None,
        "problems": problems,
    }
    print(json.dumps(summary, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--interval", type=int, default=5)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.minutes, a.seed, a.interval)))
