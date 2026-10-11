"""Flipkart.

On Flipkart every size of a shoe or shirt is its own product id (`pid`). The
product page's JSON-LD block always describes exactly the pid in the URL, so a
check fetches that size's page and reads `offers.availability` from it. That is
the same reading furqan's original tracker used, and it is exact.

The size list for the picker comes from the swatch data in
`window.__INITIAL_STATE__`; when Flipkart reshuffles that, the size links on the
page are used instead, and as a last resort the user is told to pick the size
on Flipkart and paste that link.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from ..extract import (
    assigned_json,
    is_size_label,
    ld_brand,
    ld_image,
    ld_products,
    offers_of,
    schema_availability,
    strip_tags,
    to_float,
)
from ..models import Product, Reading, ShopError, Size, Stock
from .base import Fetcher, Shop, ensure_ok, query

PID_RE = re.compile(r"^[A-Z0-9]{10,20}$")


class Flipkart(Shop):
    key = "flipkart"
    name = "Flipkart"
    domains = ("flipkart.com",)
    short_hosts = ("dl.flipkart.com", "fkrt.it", "fkrt.co", "fkrt.cc")
    color = "#2874f0"
    example = "https://www.flipkart.com/asics-gel-nyc-sneakers-men/p/itm9ed5f92dc1b32?pid=SHOHGFW7FVKGBQTY"

    def is_short_link(self, url: str) -> bool:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if host in ("fkrt.it", "fkrt.co", "fkrt.cc"):
            return True
        return host == "dl.flipkart.com" and u.path.startswith("/s/")

    def canonical_url(self, url: str) -> str:
        u = urlparse(url)
        path = u.path
        if path.startswith("/dl/"):
            path = path[3:]
        if "/p/itm" not in path:
            raise ShopError("this Flipkart link is not a product page (it should contain /p/itm...)")
        path = path[: path.index("/p/itm")] + "/" + "/".join(path[path.index("/p/itm") + 1 :].split("/")[:2])
        pid = query(url, "pid")
        return "https://www.flipkart.com" + path + (f"?pid={pid}" if pid else "")

    def size_url(self, product_url: str, pid: str) -> str:
        return product_url.split("?")[0] + "?pid=" + pid

    def poll_url(self, product_url: str, size_id: str, size_url: str | None) -> str:
        return self.size_url(product_url, size_id)

    async def load(self, fetcher: Fetcher, url: str) -> Product:
        url = self.canonical_url(url)
        f = ensure_ok(await fetcher.fetch(url))
        return self.parse(url, f.text)

    # ------------------------------------------------------------------

    def parse(self, url: str, html: str) -> Product:
        products = [p for p in ld_products(html) if p.get("offers")]
        if not products:
            raise ShopError(f"no product data in the Flipkart page ({len(html)} chars)")
        pid_wanted = query(url, "pid")
        ld = next((p for p in products if pid_wanted and str(p.get("sku")) == pid_wanted), products[0])
        pid = str(ld.get("sku") or pid_wanted or "")
        offer = (offers_of(ld) or [{}])[0]
        avail = schema_availability(offer.get("availability"))
        price = to_float(offer.get("price"))

        sizes = self._atlas_sizes(html, url) or self._swatch_sizes(html, url) or self._link_sizes(html, url)
        if sizes and "UK/India Size" in html and all(re.fullmatch(r"\d{1,2}(?:\.\d)?", s.label) for s in sizes):
            for s in sizes:
                s.label = "UK " + s.label
        note = None
        sel = next((s for s in sizes if s.id == pid), None)
        if sel is None:
            if sizes:
                # The page lists sizes but not the one we are on (rare): keep it anyway.
                sizes.append(Size(pid, "Size in your link", avail, price, url=self.size_url(url, pid)))
            else:
                sizes = [Size(pid, "Size in your link", avail, price, url=self.size_url(url, pid))]
                note = ("Flipkart did not show its size list for this item. Choose your size on Flipkart, "
                        "then paste that page's link here.")
        else:
            sel.available = avail
            sel.price = price or sel.price
        return Product(
            shop=self.key,
            url=url.split("?")[0],
            title=str(ld.get("name") or "Flipkart product"),
            brand=ld_brand(ld),
            image=ld_image(ld),
            price=price,
            colour=str(ld.get("color")) if ld.get("color") else None,
            sizes=sizes,
            selected_size_id=pid or None,
            note=note,
        )

    def reading(self, product: Product, size_id: str, size_label: str) -> Reading:
        if product.selected_size_id != size_id:
            return Reading(Stock.UNKNOWN, f"Flipkart returned a different size ({product.selected_size_id})")
        s = product.size(size_id)
        if s is None or s.available is None:
            return Reading(Stock.UNKNOWN, "Flipkart did not say whether this size is in stock")
        if s.available:
            return Reading(Stock.IN_STOCK, "in stock", s.price)
        return Reading(Stock.OUT, "sold out", s.price)

    # -- size list ---------------------------------------------------------

    def _atlas_sizes(self, html: str, url: str) -> list[Size]:
        """The size selector as Flipkart builds it today: each size tile carries
        a tracking record with its pid, its label and a stock word
        (`InStock:Variant`, `Scarcity:Variant` for "few left", `OutOfStock:Variant`)."""
        state = assigned_json(html, "__INITIAL_STATE__")
        if state is None:
            return []
        out: dict[str, Size] = {}
        for t in _atlas_tiles(state):
            pid, label = str(t["productId"]), str(t["contentTitle"]).strip()
            if pid in out or not PID_RE.match(pid) or not is_size_label(label):
                continue
            word = str(t["contentType"]).split(":")[0].lower()
            avail = True if word in ("instock", "scarcity") else False if word == "outofstock" else None
            out[pid] = Size(pid, label, avail, url=self.size_url(url, pid))
        return list(out.values()) if len(out) >= 2 else []

    def _swatch_sizes(self, html: str, url: str) -> list[Size]:
        state = assigned_json(html, "__INITIAL_STATE__")
        if state is None:
            return []
        for sw in _find_swatches(state):
            sizes = _sizes_from_swatch(sw, query(url, "pid"))
            if sizes:
                for s in sizes:
                    s.url = s.url and _abs(s.url) or self.size_url(url, s.id)
                return sizes
        return []

    def _link_sizes(self, html: str, url: str) -> list[Size]:
        """Size tiles are links to the same item with a different pid."""
        item = re.search(r"/p/(itm[0-9a-z]+)", url)
        if not item:
            return []
        out: dict[str, Size] = {}
        pat = re.compile(
            r'<a\b[^>]*href="([^"]*/p/' + item.group(1) + r'[^"]*?[?&](?:amp;)?pid=([A-Z0-9]{10,20})[^"]*)"[^>]*>(.*?)</a>',
            re.S,
        )
        for m in pat.finditer(html):
            label = strip_tags(m.group(3))
            pid = m.group(2)
            if is_size_label(label) and pid not in out:
                out[pid] = Size(pid, label, None, url=self.size_url(url, pid))
        return list(out.values()) if len(out) >= 2 else []


def _abs(u: str) -> str:
    return u if u.startswith("http") else "https://www.flipkart.com" + (u if u.startswith("/") else "/" + u)


def _atlas_tiles(obj: Any, depth: int = 0):
    if depth > 60:
        return
    if isinstance(obj, dict):
        if (
            obj.get("moduleId") == "ATLAS_SWATCH_ATTRIBUTE"
            and str(obj.get("contentType") or "").endswith(":Variant")
            and obj.get("productId")
            and obj.get("contentTitle")
            and not obj.get("eventName")
        ):
            yield obj
        for v in obj.values():
            yield from _atlas_tiles(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _atlas_tiles(v, depth + 1)


def _find_swatches(obj: Any, depth: int = 0):
    if depth > 40:
        return
    if isinstance(obj, dict):
        if isinstance(obj.get("attributeOptions"), list) and isinstance(obj.get("products"), dict):
            yield obj
        for v in obj.values():
            yield from _find_swatches(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_swatches(v, depth + 1)


def _attr_name(a: Any) -> str:
    if isinstance(a, dict):
        for k in ("id", "attributeName", "name", "display", "key"):
            if a.get(k):
                return str(a[k]).lower()
    return str(a or "").lower()


def _sizes_from_swatch(sw: dict, pid_now: str | None) -> list[Size]:
    options = sw["attributeOptions"]
    attrs = sw.get("attributes") or []
    names = [_attr_name(a) for a in attrs] if isinstance(attrs, list) else []
    size_i = next((i for i, n in enumerate(names) if "size" in n), None)
    if size_i is None:
        # No names: the size dimension is the one whose options look like sizes.
        for i, opts in enumerate(options):
            vals = [o.get("value") for o in opts if isinstance(o, dict)] if isinstance(opts, list) else []
            if vals and sum(is_size_label(v) for v in vals) >= max(1, len(vals) * 0.6):
                size_i = i
                break
    if size_i is None or size_i >= len(options) or not isinstance(options[size_i], list):
        return []
    products: dict = sw["products"]
    size_attr = attrs[size_i] if isinstance(attrs, list) and size_i < len(attrs) else {}
    uk = "uk" in str(size_attr.get("display") or size_attr.get("name") or "").lower() if isinstance(size_attr, dict) else False

    def idx(info: dict) -> list:
        ix = info.get("attributeIndexes") or info.get("attributeIndex") or []
        return ix if isinstance(ix, list) else []

    now = products.get(pid_now) if pid_now else None
    if now is None:
        sel = sw.get("selectedSwatch") or {}
        pid_sel = sel.get("pid") or sel.get("productId") if isinstance(sel, dict) else None
        now = products.get(pid_sel) if pid_sel else None
    others_fixed = [v for i, v in enumerate(idx(now)) if i != size_i] if isinstance(now, dict) else None

    out: list[tuple[int, Size]] = []
    seen: set[str] = set()
    for pid, info in products.items():
        if not isinstance(info, dict) or not PID_RE.match(str(pid)):
            continue
        ix = idx(info)
        if len(ix) <= size_i:
            continue
        if others_fixed is not None and [v for i, v in enumerate(ix) if i != size_i] != others_fixed:
            continue  # another colour
        opt_i = ix[size_i]
        if not isinstance(opt_i, int) or opt_i >= len(options[size_i]):
            continue
        opt = options[size_i][opt_i]
        label = str(opt.get("value") or opt.get("title") or "").strip() if isinstance(opt, dict) else str(opt)
        if not label or label in seen:
            continue
        if uk and re.fullmatch(r"\d{1,2}(?:\.\d)?", label):
            label = "UK " + label
        seen.add(label)
        avail = None
        for src in (info, opt if isinstance(opt, dict) else {}):
            for k in ("available", "isAvailable", "inStock"):
                if isinstance(src.get(k), bool):
                    avail = src[k]
                    break
            if avail is not None:
                break
        out.append((opt_i, Size(str(pid), label, avail, url=info.get("url") or info.get("productUrl"))))
    return [s for _, s in sorted(out, key=lambda t: t[0])]
