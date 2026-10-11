import asyncio
import time

from conftest import FakeNotifier, settle

from trackyourfavs.engine import Engine
from trackyourfavs.samples import SampleFetcher
from trackyourfavs.store import Store

FK = "https://www.flipkart.com/asics-gel-nyc-sneakers-men/p/itm9ed5f92dc1b32"
UK9 = "SHOHGFW7FVKGBQT"


def add_flipkart(store, size_id=UK9, label="UK 9", interval=10):
    return store.add_watch(shop="flipkart", product_url=FK, poll_url=f"{FK}?pid={size_id}", title="ASICS GEL-NYC Sneakers For Men",
                           size_id=size_id, size_label=label, interval=interval)


def add_myntra(store, size_id, label, interval=10):
    url = "https://www.myntra.com/casual-shoes/new-balance/new-balance-men-530-sneakers/24681357/buy"
    return store.add_watch(shop="myntra", product_url=url, poll_url=url, title="New Balance 530", size_id=size_id,
                           size_label=label, interval=interval)


async def test_sold_out_then_restock_alerts_once_then_reminds(engine, store, catalog, notifier, clock):
    store.update_settings(repeat_alerts=3, repeat_every=20)
    w = add_flipkart(store)
    w = await engine.check_now(w.id)
    await settle(engine)
    assert w.state == "OUT" and w.checks == 1
    assert notifier.sent == []

    catalog[0].size(UK9).available = True
    w = await engine.check_now(w.id)
    await settle(engine)
    assert w.state == "IN_STOCK" and w.last_in_stock
    assert len(notifier.urgent) == 1
    a = notifier.urgent[0]
    assert a.title == "UK 9 in stock on Flipkart"
    assert "₹11,999" in a.body and a.url == f"{FK}?pid={UK9}"
    assert engine.opened == [f"{FK}?pid={UK9}"] and engine.beeps == [1]

    # Next check within repeat_every: no reminder yet.
    clock.advance(5)
    await engine.check_now(w.id)
    await settle(engine)
    assert len(notifier.urgent) == 1
    # After repeat_every: reminder 2/3, then 3/3, then no more.
    for n in (2, 3, 3):
        clock.advance(21)
        await engine.check_now(w.id)
        await settle(engine)
        assert len(notifier.urgent) == n
    assert "reminder 3/3" in notifier.urgent[-1].body
    assert engine.opened == [f"{FK}?pid={UK9}"]  # the page opens once per restock

    # Sells out again: one quiet note.
    catalog[0].size(UK9).available = False
    w = await engine.check_now(w.id)
    await settle(engine)
    assert w.state == "OUT"
    assert notifier.sent[-1].title == "UK 9 sold out again on Flipkart" and not notifier.sent[-1].urgent
    events = [e["state"] for e in store.events(w.id)]
    assert events == ["OUT", "IN_STOCK", "OUT"]


async def test_got_it_stops_reminders_until_the_next_restock(engine, store, catalog, notifier, clock):
    store.update_settings(repeat_alerts=5, repeat_every=20)
    catalog[0].size(UK9).available = True
    w = add_flipkart(store)
    await engine.check_now(w.id)
    await settle(engine)
    engine.ack(w.id)
    for _ in range(3):
        clock.advance(30)
        await engine.check_now(w.id)
        await settle(engine)
    assert len(notifier.urgent) == 1
    # Out and back in: alerts again.
    catalog[0].size(UK9).available = False
    await engine.check_now(w.id)
    catalog[0].size(UK9).available = True
    await engine.check_now(w.id)
    await settle(engine)
    assert len(notifier.urgent) == 2


async def test_unreadable_shop_never_alerts_restock_and_warns_after_a_while(engine, store, fetcher, notifier, clock):
    store.update_settings(blind_after=180)
    w = add_flipkart(store)
    fetcher.block.add("www.flipkart.com")
    w = await engine.check_now(w.id)
    await settle(engine)
    assert w.state == "BLOCKED"
    assert notifier.sent == []
    rt = engine.rt[w.id]
    first_wait = rt.next_due - clock()
    clock.advance(100)
    await engine.check_now(w.id)
    assert engine.rt[w.id].next_due - clock() > first_wait  # backs off
    clock.advance(100)
    await engine.check_now(w.id)
    await settle(engine)
    assert len(notifier.sent) == 1
    assert notifier.sent[0].title == "Can't read Flipkart right now" and not notifier.sent[0].urgent
    assert "verification page" in notifier.sent[0].body
    clock.advance(500)
    await engine.check_now(w.id)
    await settle(engine)
    assert len(notifier.sent) == 1  # told once

    fetcher.block.clear()
    w = await engine.check_now(w.id)
    await settle(engine)
    assert w.state == "OUT"
    assert notifier.sent[-1].title == "Reading Flipkart again"


async def test_server_errors_are_unknown(engine, store, fetcher):
    w = add_flipkart(store)
    fetcher.fail.add("www.flipkart.com")
    w = await engine.check_now(w.id)
    assert w.state == "UNKNOWN" and "HTTP 500" in w.detail


async def test_a_bug_in_a_reader_does_not_stop_the_tracker(engine, store, monkeypatch):
    from trackyourfavs.shops import BY_KEY

    def boom(*a, **k):
        raise KeyError("surprise")

    monkeypatch.setattr(BY_KEY["flipkart"], "parse", boom)
    w = add_flipkart(store)
    w = await engine.check_now(w.id)
    assert w.state == "UNKNOWN" and "surprise" in w.detail


async def test_two_sizes_of_one_item_share_one_request(engine, store, fetcher):
    a = add_myntra(store, "80003", "UK 9")
    b = add_myntra(store, "80002", "UK 8")
    await engine.check_now(a.id)
    assert len(fetcher.calls) == 1
    assert store.watch(a.id).state == "OUT"
    assert store.watch(b.id).state == "IN_STOCK"


async def test_removed_size_is_unknown(engine, store):
    w = add_myntra(store, "99999", "UK 12")
    w = await engine.check_now(w.id)
    assert w.state == "UNKNOWN" and "no longer listed" in w.detail


async def test_paused_watch_is_not_updated(engine, store):
    w = add_flipkart(store)
    store.update_watch(w.id, enabled=False)
    await engine._poll_group(("flipkart", w.poll_url), [w], asyncio.Event())
    assert store.watch(w.id).checks == 0


async def test_failed_alert_is_reported(store, fetcher, catalog, clock):
    n = FakeNotifier(fail="ntfy said HTTP 500")
    e = Engine(store, fetcher, n, clock=clock, jitter=0, opener=lambda u: None, beeper=lambda: None)
    catalog[0].size(UK9).available = True
    w = add_flipkart(store)
    await e.check_now(w.id)
    await settle(e)
    assert e.alert_problem and "500" in e.alert_problem


async def test_loop_checks_on_schedule_and_shops_run_in_parallel(catalog):
    """Real clock: a slow shop must not hold up another one."""
    store = Store(":memory:")

    class Slow(SampleFetcher):
        async def fetch(self, url, **kw):
            if "myntra" in url:
                await asyncio.sleep(1.5)
            return await super().fetch(url, **kw)

    f = Slow(catalog)
    n = FakeNotifier()
    e = Engine(store, f, n, jitter=0, opener=lambda u: None, beeper=lambda: None)
    fk = add_flipkart(store, interval=1)
    add_myntra(store, "80003", "UK 9", interval=1)
    task = asyncio.create_task(e.run())
    t0 = time.monotonic()
    while store.watch(fk.id).checks < 3 and time.monotonic() - t0 < 6:
        await asyncio.sleep(0.05)
    elapsed = time.monotonic() - t0
    await e.stop()
    task.cancel()
    assert store.watch(fk.id).checks >= 3
    assert elapsed < 3.0, elapsed  # three Flipkart checks happened while Myntra was still answering its first


async def test_events_reach_listeners(engine, store):
    q = engine.subscribe()
    w = add_flipkart(store)
    await engine.check_now(w.id)
    types = []
    while not q.empty():
        types.append(q.get_nowait()["type"])
    assert "watch" in types and "log" in types
    engine.unsubscribe(q)


async def test_last_update_after_a_check_is_not_marked_checking(engine, store):
    q = engine.subscribe()
    w = add_flipkart(store)
    await engine.check_now(w.id)
    views = []
    while not q.empty():
        ev = q.get_nowait()
        if ev["type"] == "watch":
            views.append(ev["watch"])
    assert views[0]["checking"] is True
    assert views[-1]["checking"] is False and views[-1]["state"] == "OUT"
