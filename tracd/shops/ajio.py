"""AJIO.

AJIO's own product API (`/api/p/<code>`, the call its web app makes) returns
`variantOptions`: one entry per size with `stock.stockLevelStatus`. If that call
moves, the product page's boot state carries the same object.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from ..extract import find_size_dicts, first_str, sizes_from_dicts, to_float
from ..models import Blocked, Product, ShopError, Size
from .base import Fetcher, Shop, ensure_ok
from .generic import page_states

CODE_RE = re.compile(r"/p/([0-9A-Za-z_\-]+)")


class Ajio(Shop):
    key = "ajio"
    name = "AJIO"
    domains = ("ajio.com",)
    short_hosts = ("ajio.me", "ajiolink.page.link")
    color = "#4a7aa5"
    example = "https://www.ajio.com/<item>/p/<code>"

    def code(self, url: str) -> str:
        m = CODE_RE.search(urlparse(url).path)
        if not m:
            raise ShopError("this AJIO link is not a product page (it should contain /p/<code>)")
        return m.group(1)

    def canonical_url(self, url: str) -> str:
        path = urlparse(url).path
        m = CODE_RE.search(path)
        if not m:
            raise ShopError("this AJIO link is not a product page (it should contain /p/<code>)")
        return "https://www.ajio.com" + path[: m.end()]

    async def load(self, fetcher: Fetcher, url: str) -> Product:
        url = self.canonical_url(url)
        code = self.code(url)
        try:
            f = ensure_ok(await fetcher.fetch(f"https://www.ajio.com/api/p/{code}", kind="json", page_url=url), "product")
            return self.parse_api(url, f.text)
        except Blocked:
            raise
        except ShopError as api_error:
            # The product page carries the same data; if that fails too, the
            # API's answer is the more telling one.
            try:
                f = ensure_ok(await fetcher.fetch(url))
                return self.parse_page(url, f.text)
            except Blocked:
                raise
            except ShopError:
                raise api_error from None

    def parse_api(self, url: str, text: str) -> Product:
        try:
            j = json.loads(text)
        except ValueError:
            raise ShopError("AJIO sent something that is not product data")
        return self._from_details(url, j)

    def parse_page(self, url: str, html: str) -> Product:
        for st in page_states(html):
            det = None
            if isinstance(st, dict):
                det = (st.get("product") or {}).get("productDetails") if isinstance(st.get("product"), dict) else None
            if isinstance(det, dict):
                return self._from_details(url, det)
        raise ShopError(f"no product data in the AJIO page ({len(html)} chars)")

    def _from_details(self, url: str, j: dict) -> Product:
        if not isinstance(j, dict):
            raise ShopError("AJIO sent something that is not product data")
        items = j.get("variantOptions") if isinstance(j.get("variantOptions"), list) else None
        if not items:
            for bo in j.get("baseOptions") or []:
                if isinstance(bo, dict) and isinstance(bo.get("options"), list) and len(bo["options"]) > len(items or []):
                    items = bo["options"]
        sizes: list[Size] = sizes_from_dicts([x for x in items if isinstance(x, dict)]) if items else []
        if not sizes:
            sizes = sizes_from_dicts(find_size_dicts(j))
        if not sizes:
            raise ShopError("AJIO's reply has no size list for this item")
        image = None
        for im in j.get("images") or []:
            if isinstance(im, dict) and im.get("url"):
                image = im["url"]
                if im.get("format") in ("productDetail", "zoom", "superZoomPdp"):
                    break
        fnl = j.get("fnlColorVariantData") if isinstance(j.get("fnlColorVariantData"), dict) else {}
        return Product(
            shop=self.key,
            url=url,
            title=str(j.get("name") or fnl.get("name") or "AJIO product"),
            brand=j.get("brandName") or fnl.get("brandName") or first_str(j, "brandName"),
            image=image,
            price=to_float(j.get("price")) or to_float(fnl.get("price")),
            mrp=to_float(j.get("wasPriceData")),
            colour=fnl.get("color") or first_str(j, "color"),
            sizes=sizes,
        )
