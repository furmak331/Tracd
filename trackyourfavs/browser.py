"""The real browser the tracker reads shops through.

Shops answer a normal browser on a home connection and often refuse scripts and
cloud servers. So, like the original GEL-NYC tracker, every request is made
from inside one real tab per shop (a same-origin `fetch`, the same call the
shop's own page makes), with that tab's cookies.

The tracker never logs in, never adds to cart, and never tries to get around a
verification page. If a shop shows one, the dashboard says so and offers to
bring that tab to the front so you can clear it yourself.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .models import Fetched

JS_FETCH = r"""
async ({url, timeoutMs, kind, maxChars}) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  const t0 = performance.now();
  try {
    const r = await fetch(url, {
      credentials: 'include', cache: 'no-store', redirect: 'follow', signal: ctl.signal,
      headers: kind === 'json' ? {'Accept': 'application/json, text/plain, */*'}
                               : {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'},
    });
    let text = await r.text();
    if (text.length > maxChars) text = text.slice(0, maxChars);
    return {status: r.status, text, url: r.url, ms: Math.round(performance.now() - t0)};
  } catch (e) {
    const msg = e && e.name === 'AbortError' ? 'timed out' : String(e).slice(0, 200);
    return {status: 0, text: msg, url, ms: Math.round(performance.now() - t0)};
  } finally {
    clearTimeout(timer);
  }
}
"""

LAUNCH_ARGS = [
    # Keep background tabs running at full speed: the shop tabs are never visible.
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--window-size=1100,800",
    "--no-first-run",
    "--no-default-browser-check",
]

TAB_REFRESH_AFTER = 30 * 60  # reload each shop tab every half hour to keep its session fresh


def origin_of(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}".lower()


class BrowserFetcher:
    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool = False,
        show: bool = False,
        browser_path: str | None = None,
        timeout: float = 20.0,
        log: Callable[[str], None] = print,
    ):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.show_window = show
        self.browser_path = browser_path
        self.timeout = timeout
        self.log = log
        self.label = ""
        self._pw = None
        self._ctx = None
        self._pages: dict[str, object] = {}
        self._opened: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()
        self._closed = False

    # -- lifecycle -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._ctx is not None

    async def start(self) -> None:
        async with self._start_lock:
            if self._ctx is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:  # pragma: no cover - checked by the installer
                raise RuntimeError("Playwright is missing. Run: pip install playwright") from e
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._pw = await async_playwright().start()
            common = dict(user_data_dir=str(self.profile_dir), headless=self.headless, args=LAUNCH_ARGS)
            if not self.headless:
                common["no_viewport"] = True
            attempts: list[tuple[str, dict]] = []
            if self.browser_path:
                attempts.append(("your browser", {"executable_path": self.browser_path}))
            else:
                attempts += [("Chrome", {"channel": "chrome"}), ("Edge", {"channel": "msedge"}), ("Chromium", {})]
            errors = []
            for label, extra in attempts:
                try:
                    self._ctx = await self._pw.chromium.launch_persistent_context(**common, **extra)
                    self.label = label + (" (hidden)" if self.headless else "")
                    break
                except Exception as e:  # noqa: BLE001 - try the next browser
                    errors.append(f"{label}: {str(e).splitlines()[0][:160]}")
            if self._ctx is None:
                await self._pw.stop()
                self._pw = None
                raise RuntimeError(
                    "Could not start a browser.\n  " + "\n  ".join(errors)
                    + "\nInstall Google Chrome or Microsoft Edge, or run: python -m playwright install chromium"
                )
            self._ctx.on("close", lambda *_: self._on_closed())
            self.log(f"browser started: {self.label}")
            if not self.headless and not self.show_window:
                await self._minimise()

    def _on_closed(self) -> None:
        if not self._closed:
            self.log("the tracker's browser window was closed; it will reopen on the next check")
        self._ctx = None
        self._pages.clear()

    async def close(self) -> None:
        self._closed = True
        try:
            if self._ctx is not None:
                await self._ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._ctx = None
        self._pw = None

    async def _minimise(self) -> None:
        try:
            page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            cdp = await self._ctx.new_cdp_session(page)
            win = await cdp.send("Browser.getWindowForTarget")
            await cdp.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": {"windowState": "minimized"}})
        except Exception:  # noqa: BLE001 - cosmetic
            pass

    # -- tabs ----------------------------------------------------------------

    async def _page_for(self, url: str, page_url: str | None = None):
        if self._ctx is None:
            self._closed = False
            await self.start()
        origin = origin_of(url)
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            page = self._pages.get(origin)
            stale = time.monotonic() - self._opened.get(origin, 0) > TAB_REFRESH_AFTER
            if page is None or page.is_closed():
                # Reuse the blank first tab if there is one.
                blank = [p for p in self._ctx.pages if p.url in ("about:blank", "") and p not in self._pages.values()]
                page = blank[0] if blank else await self._ctx.new_page()
                self._pages[origin] = page
                await self._goto(page, page_url or url)
                self._opened[origin] = time.monotonic()
            elif stale or origin_of(page.url) != origin:
                await self._goto(page, page_url or url)
                self._opened[origin] = time.monotonic()
            return page

    async def _goto(self, page, url: str) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:  # noqa: BLE001 - the fetch below reports what is wrong
            self.log(f"{urlparse(url).hostname}: page load problem: {str(e).splitlines()[0][:140]}")

    # -- the Fetcher interface ---------------------------------------------------

    async def fetch(self, url: str, *, kind: str = "html", page_url: str | None = None) -> Fetched:
        t0 = time.monotonic()
        err = ""
        for attempt in range(2):
            page = await self._page_for(url, page_url)
            try:
                res = await page.evaluate(
                    JS_FETCH, {"url": url, "timeoutMs": int(self.timeout * 1000), "kind": kind, "maxChars": 8_000_000}
                )
                return Fetched(int(res.get("status") or 0), res.get("text") or "", res.get("url") or url, int(res.get("ms") or 0))
            except Exception as e:  # noqa: BLE001 - the tab navigated or closed mid-call: try once more
                err = str(e).splitlines()[0][:200]
                if page.is_closed():
                    self._pages.pop(origin_of(url), None)
                await asyncio.sleep(0.3)
        return Fetched(0, err, url, int((time.monotonic() - t0) * 1000))

    async def final_url(self, url: str) -> str:
        """Open a share link in a scratch tab and return where it lands."""
        if self._ctx is None:
            await self.start()
        start_host = urlparse(url).hostname
        page = await self._ctx.new_page()
        try:
            try:
                await page.goto(url, wait_until="commit", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                h = urlparse(page.url).hostname
                if page.url.startswith("http") and h and h != start_host:
                    break
                await asyncio.sleep(0.2)
            return page.url if page.url.startswith("http") else url
        finally:
            await page.close()

    async def refresh(self, url: str) -> None:
        """Reload the shop's tab (after a few unreadable answers in a row)."""
        page = self._pages.get(origin_of(url))
        if page is not None and not page.is_closed():
            await self._goto(page, url)
            self._opened[origin_of(url)] = time.monotonic()

    async def show(self, url: str) -> None:
        """Bring the shop's tab to the front (to clear a verification page)."""
        page = await self._page_for(url)
        await self._goto(page, url)
        try:
            cdp = await self._ctx.new_cdp_session(page)
            win = await cdp.send("Browser.getWindowForTarget")
            await cdp.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": {"windowState": "normal"}})
        except Exception:  # noqa: BLE001
            pass
        await page.bring_to_front()

    def status(self) -> dict:
        return {"running": self.running, "browser": self.label, "tabs": sorted(self._pages)}
