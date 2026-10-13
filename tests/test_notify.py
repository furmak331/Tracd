import pytest
from aiohttp import web

from tracd.notify import Alert, Notifier


@pytest.fixture
async def fake_services(aiohttp_server):
    got = {"ntfy": [], "telegram": [], "fail_ntfy": 0, "ntfy_status": 200}

    async def ntfy(request):
        if got["fail_ntfy"] > 0:
            got["fail_ntfy"] -= 1
            return web.Response(status=502, text="bad gateway")
        got["ntfy"].append((dict(request.headers), await request.json()))
        return web.json_response({"id": "x"}, status=got["ntfy_status"])

    async def tg_send(request):
        if request.match_info["token"] == "badtoken":
            return web.json_response({"ok": False, "description": "Unauthorized"}, status=401)
        got["telegram"].append(await request.json())
        return web.json_response({"ok": True})

    async def tg_updates(request):
        return web.json_response({"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 111}, "text": "old"}},
            {"update_id": 2, "message": {"chat": {"id": 424242}, "text": "hi"}},
        ]})

    app = web.Application()
    app.router.add_post("/", ntfy)
    app.router.add_post("/bot{token}/sendMessage", tg_send)
    app.router.add_get("/bot{token}/getUpdates", tg_updates)
    server = await aiohttp_server(app)
    got["url"] = str(server.make_url("")).rstrip("/")
    return got


def make(settings, svc):
    return Notifier(lambda: settings, telegram_api=svc["url"], log=lambda m: None, retry_delay=0.01)


async def test_ntfy_urgent_alert_payload(fake_services):
    s = {"ntfy_enabled": True, "ntfy_server": fake_services["url"], "ntfy_topic": "tracd-abc", "ntfy_token": "tk_1"}
    n = make(s, fake_services)
    res = await n.send(Alert("UK 9 in stock on Flipkart", "Buy now", url="https://fk/p", image="https://img", urgent=True, button="Open on Flipkart"))
    await n.close()
    assert res == {"ntfy": None}
    headers, body = fake_services["ntfy"][0]
    assert body["topic"] == "tracd-abc" and body["priority"] == 5
    assert body["click"] == "https://fk/p" and body["icon"] == "https://img"
    assert body["actions"][0]["label"] == "Open on Flipkart"
    assert headers["Authorization"] == "Bearer tk_1"


async def test_ntfy_skips_inline_pictures_it_cannot_show(fake_services):
    s = {"ntfy_enabled": True, "ntfy_server": fake_services["url"], "ntfy_topic": "tracd-abc"}
    n = make(s, fake_services)
    await n.send(Alert("UK 9 in stock", "Buy now", url="https://fk/p", image="data:image/png;base64,AAAA", urgent=True))
    await n.close()
    assert "icon" not in fake_services["ntfy"][0][1]


async def test_retries_then_succeeds(fake_services):
    fake_services["fail_ntfy"] = 2
    s = {"ntfy_enabled": True, "ntfy_server": fake_services["url"], "ntfy_topic": "t"}
    n = make(s, fake_services)
    assert await n.send(Alert("a", "b")) == {"ntfy": None}
    await n.close()
    assert len(fake_services["ntfy"]) == 1


async def test_gives_up_after_retries(fake_services):
    fake_services["fail_ntfy"] = 10
    s = {"ntfy_enabled": True, "ntfy_server": fake_services["url"], "ntfy_topic": "t"}
    n = make(s, fake_services)
    res = await n.send(Alert("a", "b"))
    await n.close()
    assert res["ntfy"] and "502" in res["ntfy"]


async def test_telegram_and_ntfy_together(fake_services):
    s = {"ntfy_enabled": True, "ntfy_server": fake_services["url"], "ntfy_topic": "t",
         "telegram_enabled": True, "telegram_token": "123:abc", "telegram_chat_id": "42"}
    n = make(s, fake_services)
    res = await n.send(Alert("UK 9 <in stock>", "on Flipkart & more", url="https://fk", urgent=True, button="Open"))
    await n.close()
    assert res == {"ntfy": None, "telegram": None}
    tg = fake_services["telegram"][0]
    assert tg["chat_id"] == "42" and tg["parse_mode"] == "HTML"
    assert "&lt;in stock&gt;" in tg["text"] and "&amp;" in tg["text"]
    assert tg["disable_notification"] is False
    assert tg["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://fk"


async def test_bad_telegram_token_is_not_retried(fake_services):
    s = {"telegram_enabled": True, "telegram_token": "badtoken", "telegram_chat_id": "42"}
    n = make(s, fake_services)
    res = await n.send(Alert("a", "b"))
    await n.close()
    assert "Unauthorized" in res["telegram"]


async def test_find_telegram_chat(fake_services):
    n = make({}, fake_services)
    assert await n.telegram_find_chat("123:abc") == "424242"
    await n.close()


async def test_no_channels():
    n = Notifier(lambda: {"ntfy_enabled": False, "telegram_enabled": True, "telegram_token": ""})
    assert n.channels() == []
    assert await n.send(Alert("a", "b")) == {}


async def test_unreachable_server_message():
    s = {"ntfy_enabled": True, "ntfy_server": "http://127.0.0.1:9", "ntfy_topic": "t"}
    n = Notifier(lambda: s, log=lambda m: None, retries=1, retry_delay=0)
    res = await n.send(Alert("a", "b"))
    await n.close()
    assert res["ntfy"] == "could not reach ntfy. Is this computer online?"
