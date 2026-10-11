"""Amazon.in.

Every size is its own ASIN with its own page. The size list comes from the
variation data on the product page; a check fetches the watched size's ASIN
page and reads its availability box and Add to Cart button.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from ..extract import strip_tags, to_float
from ..models import Product, Reading, ShopError, Size, Stock
from .base import Fetcher, Shop, ensure_ok
from .generic import meta

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|d|exec/obidos/asin)/([A-Z0-9]{10})(?:[/?]|$)", re.I)


class Amazon(Shop):
    key = "amazon"
    name = "Amazon.in"
    domains = ("amazon.in",)
    short_hosts = ("amzn.in", "amzn.to", "amzn.eu", "a.co")
    color = "#ff9900"
    example = "https://www.amazon.in/dp/<ASIN>"

    def asin(self, url: str) -> str:
        m = ASIN_RE.search(urlparse(url).path + "/")
        if not m:
            raise ShopError("this Amazon link is not a product page (it should contain /dp/ followed by the item code)")
        return m.group(1).upper()

    def canonical_url(self, url: str) -> str:
        return f"https://www.amazon.in/dp/{self.asin(url)}"

    def size_url(self, asin: str) -> str:
        return f"https://www.amazon.in/dp/{asin}?th=1&psc=1"

    def poll_url(self, product_url: str, size_id: str, size_url: str | None) -> str:
        return self.size_url(size_id)

    async def load(self, fetcher: Fetcher, url: str) -> Product:
        asin = self.asin(url)
        f = ensure_ok(await fetcher.fetch(self.size_url(asin)))
        return self.parse(asin, f.text)

    def reading(self, product: Product, size_id: str, size_label: str) -> Reading:
        if product.selected_size_id != size_id:
            return Reading(Stock.UNKNOWN, f"Amazon returned a different size ({product.selected_size_id})")
        s = product.size(size_id)
        if s is None or s.available is None:
            return Reading(Stock.UNKNOWN, "Amazon's page does not say whether this size is in stock")
        if s.available:
            return Reading(Stock.IN_STOCK, product.note or "in stock", s.price, s.qty)
        return Reading(Stock.OUT, "currently unavailable", s.price)

    def parse(self, asin: str, html: str) -> Product:
        m = re.search(r'<span[^>]*id="productTitle"[^>]*>(.*?)</span>', html, re.S)
        title = strip_tags(m.group(1)) if m else meta(html, "og:title") or meta(html, "title")
        if not title:
            raise ShopError(f"no product data in the Amazon page ({len(html)} chars)")
        m = re.search(r'<input[^>]*id="ASIN"[^>]*value="([A-Z0-9]{10})"', html) or re.search(
            r'"currentAsin"\s*:\s*"([A-Z0-9]{10})"', html)
        cur = m.group(1) if m else asin

        avail, qty, how = availability(html)
        price = _price(html)
        sizes = variation_sizes(html, cur)
        sel = next((s for s in sizes if s.id == cur), None)
        if sel is None:
            sel = Size(cur, "This item" if not sizes else "Size in your link")
            sizes.append(sel)
        sel.available, sel.qty, sel.price = avail, qty, price
        for s in sizes:
            s.url = self.size_url(s.id)
        image = None
        for pat in (r'data-old-hires="([^"]+)"', r'"hiRes"\s*:\s*"([^"]+)"', r'id="landingImage"[^>]*src="([^"]+)"', r'"large"\s*:\s*"([^"]+)"'):
            mm = re.search(pat, html)
            if mm and mm.group(1).startswith(("http", "data:image")):
                image = mm.group(1)
                break
        bm = re.search(r'id="bylineInfo"[^>]*>(.*?)</a>', html, re.S)
        brand = None
        if bm:
            brand = re.sub(r"^(Visit the|Brand:)\s*|\s*Store$", "", strip_tags(bm.group(1))).strip() or None
        return Product(
            shop=self.key,
            url=self.canonical_url("/dp/" + asin),
            title=title,
            brand=brand,
            image=image or meta(html, "og:image"),
            price=price,
            sizes=sizes,
            selected_size_id=cur,
            note=how if how and avail else None,
        )


def availability(html: str) -> tuple[bool | None, int | None, str | None]:
    """(buyable?, units left, note) from the buy box of an Amazon page."""
    m = re.search(r'<div[^>]*id="availability"[^>]*>(.*?)</div>', html, re.S)
    text = strip_tags(m.group(1)).lower() if m else ""
    qty = None
    q = re.search(r"only (\d+) left", text)
    if q:
        qty = int(q.group(1))
    has_cart = bool(re.search(r'id="(?:add-to-cart-button|buy-now-button)"', html))
    if re.search(r"currently unavailable|out of stock|temporarily out|not available|unavailable", text):
        return False, None, None
    if re.search(r"in stock|left in stock|usually dispatched|ships within|available to ship|dispatched in", text):
        return True, qty, None
    if has_cart:
        return True, qty, None
    if re.search(r'id="(?:buybox-see-all-buying-choices|outOfStockBuyBox_feature_div)"', html) and "see all buying options" in html.lower():
        return True, None, "only from other sellers"
    if re.search(r'id="outOfStock"', html):
        return False, None, None
    return None, None, None


def _price(html: str) -> float | None:
    for anchor in ("corePriceDisplay_desktop_feature_div", "corePrice_feature_div", "apex_desktop", "corePrice_desktop"):
        i = html.find(anchor)
        if i >= 0:
            m = re.search(r'a-price-whole">([\d,]+)', html[i : i + 20000])
            if m:
                return to_float(m.group(1))
    m = re.search(r'"priceAmount"\s*:\s*([\d.]+)', html)
    if m:
        return to_float(m.group(1))
    m = re.search(r'a-price-whole">([\d,]+)', html)
    return to_float(m.group(1)) if m else None


def _json_after(html: str, key: str):
    from ..extract import raw_json_at

    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*', html)
    if not m:
        return None
    raw = raw_json_at(html, m.end())
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def variation_sizes(html: str, cur: str) -> list[Size]:
    dims = _json_after(html, "dimensions")
    display = _json_after(html, "dimensionValuesDisplayData")
    if not isinstance(display, dict):
        # Newer layout: index map + value table.
        idx = _json_after(html, "dimensionToAsinMap")
        values = _json_after(html, "dimensionValuesData")
        if isinstance(idx, dict) and isinstance(values, list):
            display = {}
            for key, a in idx.items():
                try:
                    parts = [int(x) for x in str(key).split("_")]
                    display[a] = [values[d][i] for d, i in enumerate(parts)]
                except (ValueError, IndexError, TypeError):
                    continue
    if not isinstance(display, dict) or not display:
        return []
    names = [str(d).lower() for d in dims] if isinstance(dims, list) else []
    width = len(next(iter(display.values())) or [])
    size_i = next((i for i, n in enumerate(names) if "size" in n), None)
    if size_i is None:
        if width == 1:
            size_i = 0
        else:
            return []
    curv = display.get(cur)
    order = _json_after(html, "variationValues")
    order_list = order.get(names[size_i]) if isinstance(order, dict) and names else None
    out = []
    for a, vals in display.items():
        if not isinstance(vals, list) or len(vals) <= size_i:
            continue
        if curv and len(curv) == len(vals) and any(v != c for i, (v, c) in enumerate(zip(vals, curv)) if i != size_i):
            continue  # another colour
        out.append(Size(a, str(vals[size_i])))

    def key(s: Size):
        if isinstance(order_list, list) and s.label in order_list:
            return (0, order_list.index(s.label))
        n = re.search(r"\d+(?:\.\d+)?", s.label)
        return (1, float(n.group(0)) if n else 0)

    return sorted(out, key=key)
