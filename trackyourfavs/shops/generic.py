"""Reading any product page: the page's boot state, JSON-LD and meta tags.

Used as the fallback for the named shops when their usual data moves, and as
the reader for any other shop link (Shopify stores get their exact variant
feed).
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Any
from urllib.parse import urlparse

from ..extract import (
    assigned_json,
    find_size_dicts,
    is_size_label,
    ld_brand,
    ld_image,
    ld_products,
    offers_of,
    raw_json_at,
    schema_availability,
    sizes_from_dicts,
    to_float,
)
from ..models import Product, ShopError, Size
from .base import Fetcher, Shop, ensure_ok, host_of

STATE_NAMES = ("__PRELOADED_STATE__", "__INITIAL_STATE__", "__myx", "__NUXT__", "__APOLLO_STATE__", "__STATE__", "__data")


def page_states(html: str) -> list[Any]:
    out = []
    for name in STATE_NAMES:
        st = assigned_json(html, name)
        if st is not None:
            out.append(st)
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>', html)
    if m:
        raw = raw_json_at(html, html.find("{", m.end()))
        if raw:
            try:
                out.append(json.loads(raw))
            except ValueError:
                pass
    return out


def meta(html: str, prop: str) -> str | None:
    for pat in (
        r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]*content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']' + re.escape(prop) + r'["\']',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return _html.unescape(m.group(1)).strip()
    return None


def ld_sizes(products: list[dict]) -> list[Size]:
    """Sizes from JSON-LD: a ProductGroup's hasVariant, or one offer per size."""
    out: list[Size] = []
    for p in products:
        variants = p.get("hasVariant")
        if isinstance(variants, list):
            for v in variants:
                if not isinstance(v, dict):
                    continue
                label = v.get("size") or v.get("name")
                if isinstance(label, dict):
                    label = label.get("name")
                off = (offers_of(v) or [{}])[0]
                if label and (is_size_label(label) or v.get("size")):
                    out.append(Size(str(v.get("sku") or v.get("productID") or label), str(label),
                                    schema_availability(off.get("availability")), to_float(off.get("price")),
                                    url=off.get("url") or v.get("url")))
        offs = offers_of(p)
        if len(offs) > 1:
            for o in offs:
                item = o.get("itemOffered") if isinstance(o.get("itemOffered"), dict) else {}
                label = o.get("size") or item.get("size") or o.get("name")
                if label and is_size_label(label):
                    out.append(Size(str(o.get("sku") or label), str(label), schema_availability(o.get("availability")),
                                    to_float(o.get("price")), url=o.get("url")))
    seen: set[str] = set()
    uniq = []
    for s in out:
        if s.label not in seen:
            seen.add(s.label)
            uniq.append(s)
    return uniq


def parse_page(shop: str, url: str, html: str, *, state: Any = None) -> Product:
    """Best-effort product + sizes from any product page."""
    states = [state] if state is not None else page_states(html)
    lds = ld_products(html)
    ld = lds[0] if lds else {}

    sizes: list[Size] = []
    for st in states:
        found = sizes_from_dicts(find_size_dicts(st))
        if len(found) > len(sizes):
            sizes = found
    if not sizes:
        sizes = ld_sizes(lds)

    offer = (offers_of(ld) or [{}])[0] if ld else {}
    single_avail = schema_availability(offer.get("availability")) if offer else None
    if not sizes and ld:
        sizes = [Size("item", "One size", single_avail, to_float(offer.get("price")))]
    title = (ld.get("name") if ld else None) or meta(html, "og:title") or _title_tag(html)
    if not title:
        raise ShopError("could not find a product on this page")
    return Product(
        shop=shop,
        url=url,
        title=str(title),
        brand=ld_brand(ld) if ld else None,
        image=(ld_image(ld) if ld else None) or meta(html, "og:image"),
        price=to_float(offer.get("price")) if offer else to_float(meta(html, "product:price:amount")),
        sizes=sizes,
    )


def _title_tag(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else None


class Generic(Shop):
    """Any other shop. Shopify stores are read from their variant feed."""

    key = "other"
    name = "Other shop"
    color = "#475569"

    def matches(self, url: str) -> bool:
        return bool(host_of(url))

    def canonical_url(self, url: str) -> str:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ShopError("that does not look like a web link")
        return url.split("#")[0]

    async def load(self, fetcher: Fetcher, url: str) -> Product:
        url = self.canonical_url(url)
        f = ensure_ok(await fetcher.fetch(url))
        html = f.text
        if "/products/" in urlparse(url).path and ("cdn.shopify.com" in html or "Shopify.shop" in html):
            try:
                return await self._shopify(fetcher, url)
            except ShopError:
                pass
        p = parse_page(self.key, url, html)
        p.note = "This shop is not one of the built-in ones, so sizes are read from the page's product data."
        return p

    async def _shopify(self, fetcher: Fetcher, url: str) -> Product:
        u = urlparse(url)
        m = re.search(r"/products/([^/?#]+)", u.path)
        if not m:
            raise ShopError("not a Shopify product link")
        base = f"{u.scheme}://{u.netloc}"
        f = ensure_ok(await fetcher.fetch(f"{base}/products/{m.group(1)}.js", kind="json", page_url=url))
        return parse_shopify(url, f.text)


def parse_shopify(url: str, text: str) -> Product:
    try:
        j = json.loads(text)
    except ValueError:
        raise ShopError("the shop's variant feed is not readable")
    opts = j.get("options") or []
    names = [str(o.get("name") if isinstance(o, dict) else o).lower() for o in opts]
    size_i = next((i for i, n in enumerate(names) if "size" in n), None)
    sizes = []
    for v in j.get("variants") or []:
        label = v.get(f"option{size_i + 1}") if size_i is not None else v.get("title")
        if size_i is not None and len(opts) > 1:
            label = v.get("title")  # e.g. "9 / Black" -- keep the colour visible
        price = v.get("price")
        sizes.append(Size(str(v.get("id")), str(label or v.get("title")), bool(v.get("available")),
                          (price / 100 if isinstance(price, (int, float)) else to_float(price)),
                          url=url.split("?")[0] + f"?variant={v.get('id')}"))
    img = j.get("featured_image") or ((j.get("images") or [None])[0])
    if isinstance(img, str) and img.startswith("//"):
        img = "https:" + img
    price = j.get("price")
    return Product(
        shop="other",
        url=url.split("?")[0],
        title=str(j.get("title") or "Product"),
        brand=j.get("vendor"),
        image=img,
        price=price / 100 if isinstance(price, (int, float)) else None,
        sizes=sizes,
    )
