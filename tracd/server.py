"""The local web app: a JSON API, a live event stream, and the dashboard."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from . import __version__
from .engine import Engine
from .extract import first_url
from .models import Product, ShopError
from .shops import BY_KEY, GENERIC, SHOPS, is_short_link, shop_for
from .store import INTERNAL_SETTINGS, SECRET_SETTINGS, Store

log = logging.getLogger("tracd")

WEB = Path(__file__).parent / "web"
MIN_INTERVAL, MAX_INTERVAL = 5, 3600
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


def _err(msg: str, status: int = 400) -> web.Response:
    return _json({"error": msg}, status)


class Api:
    def __init__(self, store: Store, engine: Engine, fetcher: Any, *, demo: bool = False):
        self.store = store
        self.engine = engine
        self.fetcher = fetcher
        self.demo = demo
        self.products: dict[str, tuple[Product, float]] = {}  # resolved products, by product url
        self.streams: set[asyncio.Task] = set()  # open live-update connections

    # -- helpers ---------------------------------------------------------------

    def public_settings(self) -> dict:
        s = self.store.settings()
        for k in SECRET_SETTINGS:
            s[k + "_set"] = bool(s.get(k))
            s[k] = ""
        for k in INTERNAL_SETTINGS:
            s.pop(k, None)
        return s

    def state(self) -> dict:
        return {
            "version": __version__,
            "demo": self.demo,
            "watches": [self.engine.watch_view(w) for w in self.store.watches()],
            "settings": self.public_settings(),
            "shops": [s.info() for s in SHOPS],
            "browser": self.fetcher.status() if hasattr(self.fetcher, "status") else {"running": True, "browser": ""},
            "examples": [{"shop": i.shop, "url": i.url} for i in getattr(self.fetcher, "items", [])] if self.demo else [],
            "logs": list(self.engine.logs)[-80:],
            "alert_problem": self.engine.alert_problem,
            "channels": self.engine.notifier.channels(),
        }

    async def _resolve(self, text: str) -> Product:
        url = first_url(text)
        if not url:
            raise ShopError("Paste a product link from a shop (it starts with https://).")
        if is_short_link(url):
            url = await self.fetcher.final_url(url)
            if is_short_link(url):
                raise ShopError("That share link did not open a product page. Open it in your browser and copy the page's link instead.")
        shop = shop_for(url)
        shop.canonical_url(url)  # friendly error for links that are not product pages
        product = await asyncio.wait_for(shop.read(self.fetcher, url), timeout=75)
        self.products[product.url] = (product, time.time())
        # keep the cache small
        for k in [k for k, (_, t) in self.products.items() if time.time() - t > 1800]:
            self.products.pop(k, None)
        return product

    # -- routes ------------------------------------------------------------------

    async def get_state(self, request: web.Request) -> web.Response:
        return _json(self.state())

    async def resolve(self, request: web.Request) -> web.Response:
        body = await _body(request)
        try:
            product = await self._resolve(str(body.get("text") or body.get("url") or ""))
        except ShopError as e:
            return _err(_sentence(e.detail), 422)
        except asyncio.TimeoutError:
            return _err("The shop took too long to answer. Try again in a moment.", 504)
        except Exception as e:  # noqa: BLE001 - an odd page must get an answer, not a crash
            log.exception("reading a pasted link failed")
            return _err(f"Tracd could not read that page ({e.__class__.__name__}). "
                        "If it is a product page, please report the link so the shop can be supported.", 422)
        shop = BY_KEY.get(product.shop, GENERIC)
        existing = {}
        for w in self.store.watches():
            if w.shop == product.shop and w.product_url == product.url:
                existing[w.size_id] = w.id
        return _json({"product": product.to_dict(), "shop": shop.info(), "existing": existing,
                      "default_interval": self.store.settings()["default_interval"]})

    async def add_watch(self, request: web.Request) -> web.Response:
        body = await _body(request)
        url = str(body.get("url") or "")
        size_id = str(body.get("size_id") or "")
        if not url or not size_id:
            return _err("Choose a size to watch.")
        cached = self.products.get(url)
        try:
            product = cached[0] if cached else await self._resolve(url)
        except ShopError as e:
            return _err(_sentence(e.detail), 422)
        except asyncio.TimeoutError:
            return _err("The shop took too long to answer. Try again in a moment.", 504)
        except Exception as e:  # noqa: BLE001
            log.exception("reading a product to watch failed")
            return _err(f"Tracd could not read that page ({e.__class__.__name__}). Try pasting the link again.", 422)
        size = product.size(size_id)
        if size is None:
            return _err("That size is not on the product page any more. Paste the link again.", 409)
        shop = BY_KEY.get(product.shop, GENERIC)
        poll_url = shop.poll_url(product.url, size.id, size.url)
        existing = self.store.find_watch(product.shop, poll_url, size.id)
        if existing:
            if not existing.enabled:
                self.store.update_watch(existing.id, enabled=True)
                self.engine.reschedule(existing.id)
            return _json({"watch": self.engine.watch_view(self.store.watch(existing.id)), "existing": True})
        interval = _interval(body.get("interval"), self.store.settings()["default_interval"])
        w = self.store.add_watch(
            shop=product.shop, product_url=product.url, poll_url=poll_url, title=product.title, brand=product.brand,
            image=product.image, size_id=size.id, size_label=size.label, size_url=size.url or poll_url,
            interval=interval, enabled=True, price=size.price or product.price,
        )
        self.engine.log(f"Now watching {w.size_label} of {w.title} on {shop.name}, every {interval}s", watch_id=w.id)
        self.engine.reschedule(w.id)
        return _json({"watch": self.engine.watch_view(w)}, 201)

    def _watch_or_404(self, request: web.Request):
        try:
            wid = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        w = self.store.watch(wid)
        if w is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "That watch no longer exists."}), content_type="application/json")
        return w

    async def update_watch(self, request: web.Request) -> web.Response:
        w = self._watch_or_404(request)
        body = await _body(request)
        upd: dict[str, Any] = {}
        if "enabled" in body:
            upd["enabled"] = bool(body["enabled"])
        if "interval" in body:
            upd["interval"] = _interval(body["interval"], w.interval)
        w = self.store.update_watch(w.id, **upd)
        if upd.get("enabled") is False:
            self.engine.forget(w.id)
        self.engine.reschedule(w.id)
        view = self.engine.watch_view(w)
        self.engine.publish({"type": "watch", "watch": view})
        return _json({"watch": view})

    async def delete_watch(self, request: web.Request) -> web.Response:
        w = self._watch_or_404(request)
        self.store.delete_watch(w.id)
        self.engine.forget(w.id)
        self.engine.publish({"type": "removed", "watch_id": w.id})
        return _json({"ok": True})

    async def check_watch(self, request: web.Request) -> web.Response:
        w = self._watch_or_404(request)
        if not w.enabled:
            return _err("Resume this watch first.", 409)
        w = await self.engine.check_now(w.id)
        return _json({"watch": self.engine.watch_view(w)}) if w else _err("That watch no longer exists.", 404)

    async def ack_watch(self, request: web.Request) -> web.Response:
        w = self._watch_or_404(request)
        self.engine.ack(w.id)
        return _json({"watch": self.engine.watch_view(self.store.watch(w.id))})

    async def watch_events(self, request: web.Request) -> web.Response:
        w = self._watch_or_404(request)
        return _json({"events": self.store.events(w.id, 100)})

    async def put_settings(self, request: web.Request) -> web.Response:
        body = await _body(request)
        clean = {}
        for k, v in body.items():
            if k in INTERNAL_SETTINGS:
                continue
            if k in SECRET_SETTINGS and (v is None or v == ""):
                continue  # blank = keep the saved secret
            clean[k] = v
        if "repeat_alerts" in clean:
            clean["repeat_alerts"] = _clamp(clean["repeat_alerts"], 1, 30, 5)
        if "repeat_every" in clean:
            clean["repeat_every"] = _clamp(clean["repeat_every"], 5, 600, 20)
        if "blind_after" in clean:
            clean["blind_after"] = _clamp(clean["blind_after"], 30, 3600, 180)
        if "default_interval" in clean:
            clean["default_interval"] = _interval(clean["default_interval"], 15)
        if "ntfy_topic" in clean:
            topic = str(clean["ntfy_topic"] or "").strip()
            if not topic or not all(c.isalnum() or c in "-_" for c in topic) or len(topic) > 64:
                return _err("An ntfy topic can only use letters, numbers, - and _ (up to 64).")
        if "ntfy_server" in clean and not str(clean["ntfy_server"]).startswith(("https://", "http://")):
            return _err("The ntfy server must start with https://")
        self.store.update_settings(**clean)
        return _json({"settings": self.public_settings(), "channels": self.engine.notifier.channels()})

    async def clear_secret(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        if key not in SECRET_SETTINGS or key in INTERNAL_SETTINGS:
            raise web.HTTPNotFound()
        self.store.update_settings(**{key: ""})
        return _json({"settings": self.public_settings(), "channels": self.engine.notifier.channels()})

    async def test_alert(self, request: web.Request) -> web.Response:
        from .notify import Alert

        n = self.engine.notifier
        if not n.channels():
            return _err("Turn on ntfy or Telegram first.", 409)
        res = await n.send(Alert("Tracd test", "If you can read this on your phone, restock alerts will reach you.",
                                 urgent=True))
        ok = any(v is None for v in res.values())
        if ok:
            self.store.update_settings(alerts_verified=True)
            self.engine.alert_problem = None
        return _json({"results": res, "ok": ok, "settings": self.public_settings()})

    async def telegram_find_chat(self, request: web.Request) -> web.Response:
        body = await _body(request)
        token = str(body.get("token") or "").strip() or self.store.settings().get("telegram_token", "")
        if not token:
            return _err("Paste your bot token first.")
        try:
            chat = await self.engine.notifier.telegram_find_chat(token)
        except Exception as e:  # noqa: BLE001
            return _err(f"Telegram did not accept that: {e}", 422)
        if not chat:
            return _err("No messages yet. Open your bot in Telegram, send it any message, then try again.", 404)
        self.store.update_settings(telegram_token=token, telegram_chat_id=chat, telegram_enabled=True)
        return _json({"chat_id": chat, "settings": self.public_settings(), "channels": self.engine.notifier.channels()})

    async def show_browser(self, request: web.Request) -> web.Response:
        body = await _body(request)
        url = str(body.get("url") or "")
        if not url.startswith("https://"):
            return _err("No shop link given.")
        if not hasattr(self.fetcher, "show"):
            return _err("There is no shop window in demo mode.", 409)
        await self.fetcher.show(url)
        return _json({"ok": True})

    async def ntfy_qr(self, request: web.Request) -> web.Response:
        import segno

        s = self.store.settings()
        link = str(s.get("ntfy_server") or "https://ntfy.sh").rstrip("/") + "/" + s["ntfy_topic"]
        buf = io.BytesIO()
        segno.make(link, error="m").save(buf, kind="svg", scale=5, dark="#0f172a", light="#ffffff", border=2, xmldecl=False)
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    async def stream(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        q = self.engine.subscribe()
        me = asyncio.current_task()
        if me:
            self.streams.add(me)
        try:
            await resp.write(b"event: hello\ndata: {}\n\n")
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    await resp.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except ConnectionResetError:
            pass
        finally:
            self.engine.unsubscribe(q)
            self.streams.discard(me)
        return resp

    async def close_streams(self, app: web.Application) -> None:
        """Open dashboards would otherwise hold the shutdown up for a minute."""
        for t in list(self.streams):
            t.cancel()

    async def index(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


def _sentence(s: str) -> str:
    s = s.strip()
    if not s:
        return "Something went wrong."
    s = s[0].upper() + s[1:]
    return s if s.endswith((".", "!", "?")) else s + "."


def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError, OverflowError):
        n = int(default)
    return max(lo, min(hi, n))


def _interval(v: Any, default: int) -> int:
    return _clamp(v, MIN_INTERVAL, MAX_INTERVAL, default)


async def _body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "Bad request."}), content_type="application/json")
    return data if isinstance(data, dict) else {}


def guard(lan_key: str | None):
    """Only this computer (or, with --lan, someone holding the key) may use the app.
    Mutating calls must be JSON from our own page, which stops other websites
    from poking the API through your browser."""

    @web.middleware
    async def mw(request: web.Request, handler):
        host = (request.host or "").rsplit(":", 1)[0].lower()
        local = host in LOCAL_HOSTS and request.remote in ("127.0.0.1", "::1", None)
        if not local:
            if not lan_key:
                return web.Response(status=403, text="Tracd only answers on this computer. Start it with --lan to use it from your phone.")
            given = request.query.get("key") or request.cookies.get("tracd_key") or request.headers.get("X-Tracd-Key")
            if not given or not secrets.compare_digest(given.encode(), lan_key.encode()):
                return web.Response(status=401, text="Open the link with the key that Tracd printed when it started.")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if "application/json" not in request.headers.get("Content-Type", ""):
                return _err("Send JSON.", 415)
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1].lower() != (request.host or "").lower():
                return _err("Cross-site request refused.", 403)
        try:
            resp = await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 - always answer the dashboard in words
            if not request.path.startswith("/api/"):
                raise
            log.exception("request failed: %s %s", request.method, request.path)
            resp = _err(f"Something went wrong ({e.__class__.__name__}). Try again; details are in tracd.log.", 500)
        if not local and lan_key and request.query.get("key"):
            resp.set_cookie("tracd_key", lan_key, httponly=True, samesite="Strict", max_age=3600 * 24 * 365)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    return mw


def create_app(store: Store, engine: Engine, fetcher: Any, *, demo: bool = False, lan_key: str | None = None) -> web.Application:
    api = Api(store, engine, fetcher, demo=demo)
    app = web.Application(middlewares=[guard(lan_key)], client_max_size=256 * 1024)
    app["api"] = api
    app.on_shutdown.append(api.close_streams)
    r = app.router
    r.add_get("/", api.index)
    r.add_get("/api/state", api.get_state)
    r.add_post("/api/resolve", api.resolve)
    r.add_post("/api/watches", api.add_watch)
    r.add_patch("/api/watches/{id}", api.update_watch)
    r.add_delete("/api/watches/{id}", api.delete_watch)
    r.add_post("/api/watches/{id}/check", api.check_watch)
    r.add_post("/api/watches/{id}/ack", api.ack_watch)
    r.add_get("/api/watches/{id}/events", api.watch_events)
    r.add_put("/api/settings", api.put_settings)
    r.add_delete("/api/settings/{key}", api.clear_secret)
    r.add_post("/api/settings/test", api.test_alert)
    r.add_post("/api/telegram/find-chat", api.telegram_find_chat)
    r.add_post("/api/browser/show", api.show_browser)
    r.add_get("/api/ntfy-qr.svg", api.ntfy_qr)
    r.add_get("/api/stream", api.stream)
    r.add_static("/static/", WEB, append_version=False)
    return app
