"""The real browser path: a Chromium tab per shop making same-origin requests.

The shops are stood in for by Playwright request interception, so this runs
offline but exercises the exact code that runs on your PC.
"""

import asyncio

import pytest
from conftest import FakeNotifier, chromium_path, settle

from trackyourfavs.browser import BrowserFetcher
from trackyourfavs.engine import Engine
from trackyourfavs.samples import SampleFetcher, demo_catalog
from trackyourfavs.shops import shop_for
from trackyourfavs.store import Store

pytestmark = pytest.mark.skipif(chromium_path() is None, reason="no Chromium available")

FK = "https://www.flipkart.com/asics-gel-nyc-sneakers-men/p/itm9ed5f92dc1b32"


@pytest.fixture
async def shopnet(tmp_path):
    catalog = demo_catalog()
    samples = SampleFetcher(catalog)
    fetcher = BrowserFetcher(tmp_path / "profile", headless=True, browser_path=chromium_path(), timeout=3, log=lambda m: None)
    await fetcher.start()
    state = {"status": None, "hang": False, "hits": []}

    async def handle(route):
        url = route.request.url
        state["hits"].append(url)
        if "dl.flipkart.com/s/" in url:
            # Share links bounce through a tiny page, like the real ones.
            return await route.fulfill(status=200, content_type="text/html",
                                       body=f"<script>location.replace('{FK}?pid=SHOHGFW7FVKGBQR')</script>")
        if state["hang"] and route.request.resource_type == "fetch":
            await asyncio.sleep(10)
            return await route.abort()
        if state["status"]:
            return await route.fulfill(status=state["status"], body="<html>Access Denied</html>", content_type="text/html")
        body = samples._body(url)
        if body is None:  # a product page for a tab, e.g. Tata CLiQ's
            body = "<!doctype html><html><head><title>Shop</title></head><body>shop page</body></html>"
        ctype = "application/json" if body.lstrip().startswith("{") else "text/html; charset=utf-8"
        await route.fulfill(status=200, body=body, content_type=ctype)

    await fetcher._ctx.route("**/*", handle)
    yield fetcher, catalog, state
    await fetcher.close()


async def test_reads_a_product_through_a_real_tab(shopnet):
    fetcher, catalog, state = shopnet
    shop = shop_for(FK)
    p = await shop.load(fetcher, FK + "?pid=SHOHGFW7FVKGBQT")
    assert p.title == "ASICS GEL-NYC Sneakers For Men"
    assert len(p.sizes) == 6 and p.size("SHOHGFW7FVKGBQT").available is False
    assert fetcher.status()["tabs"] == ["https://www.flipkart.com"]


async def test_api_call_opens_the_tab_on_the_product_page(shopnet):
    fetcher, catalog, state = shopnet
    url = "https://www.tatacliq.com/asics-mens-gel-nyc-birch-driftwood-running-shoes/p-mp000000029497693"
    p = await shop_for(url).load(fetcher, url)
    assert p.size("MP000000029497691").available is True
    # First the product page loaded in the tab, then the API was called from it.
    assert state["hits"][0] == url
    assert "productDetails/mp000000029497693" in state["hits"][1]


async def test_share_links_are_followed(shopnet):
    fetcher, _, _ = shopnet
    final = await fetcher.final_url("https://dl.flipkart.com/s/abc123")
    assert final == FK + "?pid=SHOHGFW7FVKGBQR"


async def test_blocked_and_slow_answers(shopnet):
    fetcher, _, state = shopnet
    state["status"] = 403
    f = await fetcher.fetch(FK + "?pid=X")
    assert f.status == 403
    state["status"] = None
    state["hang"] = True
    f = await fetcher.fetch("https://www.myntra.com/x/1234567/buy")
    assert f.status == 0 and "timed out" in f.text


async def test_tab_closed_by_hand_is_reopened(shopnet):
    fetcher, _, _ = shopnet
    await fetcher.fetch(FK + "?pid=SHOHGFW7FVKGBQT")
    page = fetcher._pages["https://www.flipkart.com"]
    await page.close()
    f = await fetcher.fetch(FK + "?pid=SHOHGFW7FVKGBQT")
    assert f.status == 200 and "GEL-NYC" in f.text


async def test_restock_end_to_end_through_the_browser(shopnet):
    fetcher, catalog, _ = shopnet
    store = Store(":memory:")
    n = FakeNotifier()
    e = Engine(store, fetcher, n, jitter=0, opener=lambda u: None, beeper=lambda: None)
    w = store.add_watch(shop="flipkart", product_url=FK, poll_url=FK + "?pid=SHOHGFW7FVKGBQT", title="GEL-NYC",
                        size_id="SHOHGFW7FVKGBQT", size_label="UK 9", interval=10)
    w = await e.check_now(w.id)
    assert w.state == "OUT"
    catalog[0].size("SHOHGFW7FVKGBQT").available = True
    w = await e.check_now(w.id)
    await settle(e)
    assert w.state == "IN_STOCK"
    assert n.urgent and n.urgent[0].title == "UK 9 in stock on Flipkart"
    assert w.last_ms is not None and w.last_ms < 5000
