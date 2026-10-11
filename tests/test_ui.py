"""The dashboard, driven in a real browser against the real server."""

import asyncio

import pytest
from aiohttp.test_utils import TestServer
from conftest import FakeNotifier, chromium_path

from trackyourfavs.engine import Engine
from trackyourfavs.samples import SampleFetcher, demo_catalog
from trackyourfavs.server import create_app
from trackyourfavs.store import Store

pytestmark = pytest.mark.skipif(chromium_path() is None, reason="no Chromium available")

FK = "https://www.flipkart.com/asics-gel-nyc-sneakers-men/p/itm9ed5f92dc1b32?pid=SHOHGFW7FVKGBQT"


@pytest.fixture
async def app():
    from playwright.async_api import async_playwright

    catalog = demo_catalog()
    store = Store(":memory:")
    fetcher = SampleFetcher(catalog, delay=0.05)
    engine = Engine(store, fetcher, FakeNotifier(), opener=lambda u: None, beeper=lambda: None)
    server = TestServer(create_app(store, engine, fetcher), host="127.0.0.1")
    await server.start_server()
    loop_task = asyncio.create_task(engine.run())
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(executable_path=chromium_path())
    page = await browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    base = str(server.make_url("/"))
    yield page, base, catalog, store, errors
    await browser.close()
    await pw.stop()
    await engine.stop()
    loop_task.cancel()
    await server.close()


async def test_add_watch_restock_and_remove(app):
    page, base, catalog, store, errors = app
    await page.goto(base)
    await page.get_by_role("button", name="Later").click()
    await page.wait_for_selector("text=Nothing watched yet")

    await page.fill("#link", "Hey look " + FK)
    await page.click("#find-btn")
    await page.wait_for_selector(".size")
    assert await page.locator(".size").count() == 6
    # The size in the link is picked already.
    assert await page.locator('.size[aria-pressed="true"]').inner_text() == "UK 9\nSold out"
    await page.get_by_role("button", name="30s").click()
    await page.click("#watch-btn")
    card = page.locator("[data-watch]")
    await card.wait_for()
    await page.wait_for_selector("text=Sold out", timeout=5000)
    assert "every 30s" in await card.inner_text()
    assert store.watches()[0].interval == 30

    # Restock: "Check now" shows it at once.
    catalog[0].size("SHOHGFW7FVKGBQT").available = True
    await page.get_by_role("button", name="Check now").click()
    await page.wait_for_selector("text=In stock now", timeout=5000)
    buy = page.get_by_role("link", name="Buy on Flipkart")
    assert await buy.get_attribute("href") == FK
    await page.get_by_role("button", name="Got it").click()
    await page.wait_for_function("!document.body.innerText.includes('Got it')")

    # History, then remove.
    await page.get_by_role("button", name="More").click()
    await page.get_by_role("menuitem", name="History").click()
    await page.wait_for_selector(".history .ev.IN_STOCK")
    await page.get_by_role("button", name="More").click()
    await page.get_by_role("menuitem", name="Stop watching").click()
    await page.wait_for_selector("text=Nothing watched yet")
    assert store.watches() == []
    assert errors == []


async def test_pick_several_sizes_and_pause(app):
    page, base, catalog, store, errors = app
    await page.goto(base)
    await page.get_by_role("button", name="Later").click()
    await page.fill("#link", "https://www.myntra.com/casual-shoes/new-balance/new-balance-men-530-sneakers/24681357/buy")
    await page.press("#link", "Enter")
    await page.wait_for_selector(".size")
    await page.locator(".size", has_text="UK 9").click()
    await page.locator(".size", has_text="UK 10").click()
    assert await page.inner_text("#watch-btn") == "Watch 2 sizes"
    await page.click("#watch-btn")
    await page.wait_for_function("document.querySelectorAll('[data-watch]').length === 2")
    await page.wait_for_selector("text=In stock now", timeout=5000)  # UK 10 is in stock in the sample
    first = page.locator("[data-watch]", has_text="Sold out")
    await first.get_by_role("button", name="Pause").click()
    await page.wait_for_selector("text=Paused")
    assert sum(1 for w in store.watches() if not w.enabled) == 1
    assert errors == []


async def test_bad_link_shows_a_clear_error(app):
    page, base, *_, errors = app
    await page.goto(base)
    await page.get_by_role("button", name="Later").click()
    await page.fill("#link", "https://www.flipkart.com/search?q=asics")
    await page.click("#find-btn")
    note = page.locator(".note.warn")
    await note.wait_for()
    assert "not a product page" in await note.inner_text()


async def test_settings_save(app):
    page, base, catalog, store, errors = app
    await page.goto(base)
    await page.get_by_role("button", name="Set up phone alerts").click()
    await page.wait_for_selector("#sheet:not([hidden])")
    topic = store.settings()["ntfy_topic"]
    assert topic in await page.inner_text("#sheet")
    assert await page.locator(".qr img").evaluate("img => img.complete && img.naturalWidth > 0")
    await page.get_by_role("switch").nth(3).click()  # "Beep on a restock"
    await page.wait_for_function("document.body.innerText.includes('Saved')")
    assert store.settings()["beep"] is False
    await page.get_by_role("button", name="Send a test alert").click()
    await page.wait_for_selector("text=Test sent")
    assert store.settings()["alerts_verified"] is True
    await page.keyboard.press("Escape")
    assert await page.locator("#sheet").is_hidden()
    assert errors == []
