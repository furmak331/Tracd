import asyncio
import json

import pytest

from tracd.server import create_app

FK = "https://www.flipkart.com/asics-gel-nyc-sneakers-men/p/itm9ed5f92dc1b32"
J = {"Content-Type": "application/json"}


@pytest.fixture
async def client(aiohttp_client, store, engine, fetcher):
    app = create_app(store, engine, fetcher)
    return await aiohttp_client(app)


async def post(client, path, body=None, method="POST"):
    r = await client.request(method, path, data=json.dumps(body or {}), headers=J)
    return r.status, await r.json()


async def test_state(client):
    r = await client.get("/api/state")
    d = await r.json()
    assert r.status == 200
    assert {s["key"] for s in d["shops"]} >= {"flipkart", "myntra", "ajio", "amazon", "tatacliq", "nykaa", "nykaafashion"}
    assert d["settings"]["ntfy_topic"].startswith("tracd-")
    assert d["watches"] == []


async def test_dashboard_page_and_assets(client):
    r = await client.get("/")
    assert r.status == 200 and "Tracd" in await r.text()
    for f in ("app.js", "styles.css", "icon.svg"):
        assert (await client.get("/static/" + f)).status == 200


async def test_resolve_add_and_manage_a_watch(client, store, catalog):
    st, d = await post(client, "/api/resolve", {"text": f"Look at this {FK}?pid=SHOHGFW7FVKGBQT&lid=x"})
    assert st == 200
    assert d["shop"]["key"] == "flipkart"
    assert d["product"]["selected_size_id"] == "SHOHGFW7FVKGBQT"
    assert len(d["product"]["sizes"]) == 6

    st, d = await post(client, "/api/watches", {"url": d["product"]["url"], "size_id": "SHOHGFW7FVKGBQT", "interval": 2})
    assert st == 201
    w = d["watch"]
    assert w["size_label"] == "UK 9" and w["interval"] == 5  # clamped to the minimum
    assert w["poll_url"] == FK + "?pid=SHOHGFW7FVKGBQT"
    assert w["open_url"].endswith("?pid=SHOHGFW7FVKGBQT")

    # Adding it again returns the same watch.
    st, d2 = await post(client, "/api/watches", {"url": FK, "size_id": "SHOHGFW7FVKGBQT"})
    assert st == 200 and d2["existing"] and d2["watch"]["id"] == w["id"]

    # resolve now reports it as watched
    st, d = await post(client, "/api/resolve", {"text": FK + "?pid=SHOHGFW7FVKGBQT"})
    assert d["existing"] == {"SHOHGFW7FVKGBQT": w["id"]}

    st, d = await post(client, f"/api/watches/{w['id']}/check")
    assert st == 200 and d["watch"]["state"] == "OUT" and d["watch"]["checks"] == 1

    st, d = await post(client, f"/api/watches/{w['id']}", {"interval": 30, "enabled": False}, method="PATCH")
    assert d["watch"]["interval"] == 30 and d["watch"]["enabled"] is False
    st, d = await post(client, f"/api/watches/{w['id']}/check")
    assert st == 409

    r = await client.get(f"/api/watches/{w['id']}/events")
    assert [e["state"] for e in (await r.json())["events"]] == ["OUT"]

    st, d = await post(client, f"/api/watches/{w['id']}", method="DELETE")
    assert d["ok"]
    assert store.watches() == []
    st, d = await post(client, f"/api/watches/{w['id']}/check")
    assert st == 404


async def test_resolve_errors_are_friendly(client):
    st, d = await post(client, "/api/resolve", {"text": "hello"})
    assert st == 422 and "Paste a product link" in d["error"]
    st, d = await post(client, "/api/resolve", {"text": "https://www.flipkart.com/search?q=shoes"})
    assert st == 422 and d["error"].startswith("This Flipkart link is not a product page")
    st, d = await post(client, "/api/resolve", {"text": "https://www.myntra.com/sneakers/x/y/11111111/buy"})
    assert st == 422 and d["error"].endswith(".")


async def test_short_links_are_followed(client, fetcher):
    fetcher.redirects["https://dl.flipkart.com/s/abc123"] = FK + "?pid=SHOHGFW7FVKGBQR"
    st, d = await post(client, "/api/resolve", {"text": "https://dl.flipkart.com/s/abc123"})
    assert st == 200 and d["product"]["selected_size_id"] == "SHOHGFW7FVKGBQR"


async def test_unknown_size_is_refused(client):
    await post(client, "/api/resolve", {"text": FK})
    st, d = await post(client, "/api/watches", {"url": FK, "size_id": "NOPE"})
    assert st == 409


async def test_ack(client, store, catalog):
    catalog[0].size("SHOHGFW7FVKGBQT").available = True
    await post(client, "/api/resolve", {"text": FK})
    _, d = await post(client, "/api/watches", {"url": FK, "size_id": "SHOHGFW7FVKGBQT"})
    wid = d["watch"]["id"]
    _, d = await post(client, f"/api/watches/{wid}/check")
    assert d["watch"]["state"] == "IN_STOCK" and d["watch"]["acked"] is False
    _, d = await post(client, f"/api/watches/{wid}/ack")
    assert d["watch"]["acked"] is True


async def test_settings_validation_and_secrets(client, store):
    st, d = await post(client, "/api/settings", {"repeat_alerts": 100, "telegram_token": "123:secret", "beep": False}, method="PUT")
    assert st == 200
    s = d["settings"]
    assert s["repeat_alerts"] == 30 and s["beep"] is False
    assert s["telegram_token"] == "" and s["telegram_token_set"] is True  # never sent back
    assert store.settings()["telegram_token"] == "123:secret"
    # blank keeps the saved secret
    await post(client, "/api/settings", {"telegram_token": ""}, method="PUT")
    assert store.settings()["telegram_token"] == "123:secret"
    st, d = await post(client, "/api/settings", {"ntfy_topic": "bad topic!"}, method="PUT")
    assert st == 400
    st, d = await post(client, "/api/settings", {"ntfy_server": "ftp://x"}, method="PUT")
    assert st == 400
    st, d = await post(client, "/api/settings/telegram_token", method="DELETE")
    assert store.settings()["telegram_token"] == ""
    r = await client.get("/api/state")
    assert "123:secret" not in await r.text()


async def test_test_alert(client, notifier, store):
    st, d = await post(client, "/api/settings/test")
    assert st == 200 and d["ok"] and store.settings()["alerts_verified"] is True
    assert notifier.sent[-1].title == "Tracd test"
    notifier.on = False
    st, d = await post(client, "/api/settings/test")
    assert st == 409


async def test_qr_code(client):
    r = await client.get("/api/ntfy-qr.svg")
    body = await r.text()
    assert r.status == 200 and r.content_type == "image/svg+xml"
    assert body.startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in body


async def test_cross_site_and_non_json_writes_are_refused(client):
    r = await client.post("/api/resolve", data="text=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status == 415
    r = await client.post("/api/resolve", data="{}", headers={**J, "Origin": "https://evil.example"})
    assert r.status == 403
    r = await client.get("/api/state", headers={"Host": "evil.example"})
    assert r.status == 403


async def test_lan_mode_needs_the_key(aiohttp_client, store, engine, fetcher):
    client = await aiohttp_client(create_app(store, engine, fetcher, lan_key="sekret"))
    r = await client.get("/api/state", headers={"Host": "192.168.1.5:8723"})
    assert r.status == 401
    r = await client.get("/?key=sekret", headers={"Host": "192.168.1.5:8723"})
    assert r.status == 200 and "tracd_key" in r.cookies
    r = await client.get("/api/state", headers={"Host": "192.168.1.5:8723", "X-Tracd-Key": "sekret"})
    assert r.status == 200


async def test_live_stream(client, store):
    await post(client, "/api/resolve", {"text": FK})
    r = await client.get("/api/stream")
    assert r.status == 200
    line = await asyncio.wait_for(r.content.readline(), 2)
    assert line.startswith(b"event: hello")
    await r.content.readline()
    _, d = await post(client, "/api/watches", {"url": FK, "size_id": "SHOHGFW7FVKGBQT"})
    seen = []
    for _ in range(20):
        line = await asyncio.wait_for(r.content.readline(), 3)
        if line.startswith(b"data: "):
            seen.append(json.loads(line[6:])["type"])
            break
    assert seen == ["log"]
    r.close()


async def test_a_key_with_odd_characters_is_refused_not_a_crash(aiohttp_client, store, engine, fetcher):
    client = await aiohttp_client(create_app(store, engine, fetcher, lan_key="sekret"))
    r = await client.get("/api/state", headers={"Host": "192.168.1.5:8723"}, params={"key": "sékrét✓"})
    assert r.status == 401


@pytest.mark.parametrize("body", [
    {"repeat_alerts": "lots"}, {"repeat_every": "1e999"}, {"default_interval": None}, {"blind_after": "abc"},
    {"repeat_alerts": [1]}, {"lan_key": "mine"},
])
async def test_odd_settings_values_never_break_the_app(client, store, body):
    st, d = await post(client, "/api/settings", body, method="PUT")
    assert st == 200, d
    assert "lan_key" not in d["settings"]
    assert store.settings()["lan_key"] != "mine"
    assert 1 <= store.settings()["repeat_alerts"] <= 30 and 5 <= store.settings()["repeat_every"] <= 600


async def test_a_page_that_trips_up_a_reader_gets_a_friendly_answer(client, monkeypatch):
    from tracd.shops.flipkart import Flipkart

    async def boom(self, fetcher, url):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(Flipkart, "load", boom)
    st, d = await post(client, "/api/resolve", {"text": FK})
    assert st == 422 and "could not read that page" in d["error"]
    st, d = await post(client, "/api/watches", {"url": FK, "size_id": "SHOHGFW7FVKGBQT"})
    assert st == 422 and "could not read" in d["error"]


async def test_any_unexpected_api_error_is_answered_in_words(client, monkeypatch, store):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "events", boom)
    st, d = await post(client, "/api/resolve", {"text": FK})
    _, d = await post(client, "/api/watches", {"url": FK, "size_id": "SHOHGFW7FVKGBQT"})
    r = await client.get(f"/api/watches/{d['watch']['id']}/events")
    assert r.status == 500 and "Something went wrong" in (await r.json())["error"]


async def test_shutdown_is_quick_with_a_dashboard_open(store, engine, fetcher):
    import time

    from aiohttp import ClientSession, web

    app = create_app(store, engine, fetcher)
    runner = web.AppRunner(app, shutdown_timeout=3)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    async with ClientSession() as http:
        r = await http.get(f"http://127.0.0.1:{port}/api/stream")
        await r.content.readline()
        t = time.monotonic()
        await runner.cleanup()
        assert time.monotonic() - t < 2
        r.close()
    assert engine.listeners == set()


def test_lan_key_survives_a_restart(tmp_path):
    from tracd.store import Store

    s = Store(tmp_path / "tracd.db")
    s.update_settings(lan_key="abc123")
    s.close()
    assert Store(tmp_path / "tracd.db").settings()["lan_key"] == "abc123"
