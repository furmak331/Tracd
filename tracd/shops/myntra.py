"""Myntra.

Product pages boot from `window.__myx`, whose `pdpData.sizes` lists every size
with `available` and, per seller, `availableCount`.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ..extract import assigned_json, first_str, label_of, price_of, qty_of, stock_of, to_float
from ..models import Product, ShopError, Size
from .base import Fetcher, Shop, ensure_ok
from .generic import meta, parse_page

STYLE_RE = re.compile(r"/(\d{5,10})(?:/buy)?/?$")


class Myntra(Shop):
    key = "myntra"
    name = "Myntra"
    domains = ("myntra.com",)
    short_hosts = ("myntr.it", "myntra.page.link")
    color = "#ff3f6c"
    example = "https://www.myntra.com/casual-shoes/asics/asics-men-gel-nyc-sneakers/<number>/buy"

    def style_id(self, url: str) -> str:
        m = STYLE_RE.search(urlparse(url).path)
        if not m:
            raise ShopError("this Myntra link is not a product page (it should end with the item number, like /12345678/buy)")
        return m.group(1)

    def canonical_url(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        self.style_id(path)
        if not path.endswith("/buy") and path.count("/") > 1:
            path += "/buy"
        return "https://www.myntra.com" + path

    async def load(self, fetcher: Fetcher, url: str) -> Product:
        url = self.canonical_url(url)
        f = ensure_ok(await fetcher.fetch(url))
        return self.parse(url, f.text)

    def parse(self, url: str, html: str) -> Product:
        myx = assigned_json(html, "__myx")
        pdp = myx.get("pdpData") if isinstance(myx, dict) else None
        if not isinstance(pdp, dict):
            if "pdpData" not in html and "__myx" not in html:
                p = parse_page(self.key, url, html)
                if p.sizes and p.sizes[0].id != "item":
                    return p
            raise ShopError(f"no product data in the Myntra page ({len(html)} chars)")
        sizes = []
        for d in pdp.get("sizes") or []:
            if not isinstance(d, dict):
                continue
            label = _uk_label(d) or label_of(d)
            sid = d.get("skuId") or d.get("styleId") or label
            if not label or sid is None:
                continue
            sizes.append(Size(str(sid), str(label), stock_of(d), price_of(d), qty_of(d)))
        if not sizes:
            raise ShopError("Myntra's page has no size list for this item")
        price = pdp.get("price") if isinstance(pdp.get("price"), dict) else {}
        brand = pdp.get("brand")
        return Product(
            shop=self.key,
            url=url,
            title=str(pdp.get("name") or meta(html, "og:title") or "Myntra product"),
            brand=brand.get("name") if isinstance(brand, dict) else (brand if isinstance(brand, str) else None),
            image=first_str(pdp.get("media") or {}, "imageURL", "src", "secureSrc") or meta(html, "og:image"),
            price=to_float(price.get("discounted")) or to_float(price.get("mrp")),
            mrp=to_float(price.get("mrp")),
            colour=first_str(pdp, "baseColour", "colour"),
            sizes=sizes,
        )


def _uk_label(d: dict) -> str | None:
    """Myntra keeps the shop-wide label ("9") plus each scale; prefer "UK 9"."""
    label = d.get("label")
    for s in d.get("allSizesList") or []:
        if not isinstance(s, dict):
            continue
        code = str(s.get("scaleCode") or "").lower()
        val = s.get("sizeValue")
        if "uk" in code and val is not None and str(val) == str(label):
            return f"UK {val}"
    return None
