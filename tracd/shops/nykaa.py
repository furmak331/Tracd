"""Nykaa Man / Nykaa and Nykaa Fashion.

Nykaa Man (and nykaa.com) pages boot from `window.__PRELOADED_STATE__`, whose
`productPage.product.variants` lists every size with `inStock` and `quantity`.
Nykaa Fashion is a different site with its own state object; its sizes are found
by shape.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from ..extract import assigned_json, find_size_dicts, first_str, sizes_from_dicts, to_float, to_int
from ..models import Product, ShopError, Size
from .base import Fetcher, Shop, ensure_ok, query
from .generic import meta, page_states, parse_page

ID_RE = re.compile(r"/p/(\d+)")


class Nykaa(Shop):
    key = "nykaa"
    name = "Nykaa Man"
    domains = ("nykaaman.com", "nykaa.com")
    short_hosts = ("nykaa.app.link", "nykaa.onelink.me")
    color = "#e80071"
    example = "https://www.nykaaman.com/asics-gel-nyc-unisex-sneakers/p/18954652?skuId=18954544"

    def canonical_url(self, url: str) -> str:
        u = urlparse(url)
        m = ID_RE.search(u.path)
        if not m:
            raise ShopError("this Nykaa link is not a product page (it should contain /p/<number>)")
        host = (u.hostname or "www.nykaaman.com").lower()
        if host.count(".") == 1:
            host = "www." + host
        path = u.path[: m.end()]
        return f"https://{host}{path}"

    async def load(self, fetcher: Fetcher, url: str) -> Product:
        sku = query(url, "skuId")
        canon = self.canonical_url(url)
        f = ensure_ok(await fetcher.fetch(canon + (f"?skuId={sku}" if sku else "")))
        p = self.parse(canon, f.text)
        if sku and p.size(sku):
            p.selected_size_id = sku
        return p

    def parse(self, url: str, html: str) -> Product:
        st = assigned_json(html, "__PRELOADED_STATE__")
        prod = None
        if isinstance(st, dict):
            prod = (st.get("productPage") or {}).get("product")
        variants = prod.get("variants") if isinstance(prod, dict) else None
        if isinstance(variants, list) and variants:
            sizes = []
            for v in variants:
                if not isinstance(v, dict):
                    continue
                vid = v.get("childId") or v.get("id") or v.get("sku")
                label = v.get("variantName") or v.get("name")
                if vid is None or not label:
                    continue
                qty = to_int(v.get("quantity"))
                in_stock = v.get("inStock")
                avail = bool(in_stock) or (qty or 0) > 0 if isinstance(in_stock, bool) or qty is not None else None
                sizes.append(Size(str(vid), str(label), avail, to_float(v.get("offerPrice")),
                                  qty if qty and qty > 0 else None,
                                  url=f"{url}?skuId={vid}"))
        else:
            data = prod if prod is not None else st
            sizes = sizes_from_dicts(find_size_dicts(data)) if data is not None else []
        if not sizes:
            if looks_like_product(html):
                raise ShopError("Nykaa's page has no size list for this item")
            raise ShopError(f"no product data in the Nykaa page ({len(html)} chars)")
        src = prod if isinstance(prod, dict) else (st or {})
        image = first_str(src, "imageUrl", "image", "productImage") or meta(html, "og:image")
        return Product(
            shop=self.key,
            url=url,
            title=first_str(src, "name", "title") or meta(html, "og:title") or "Nykaa product",
            brand=first_str(src, "brandName", "brand_name"),
            image=image,
            price=to_float(src.get("offerPrice")) if isinstance(src, dict) else None,
            mrp=to_float(src.get("mrp")) if isinstance(src, dict) else None,
            sizes=sizes,
        )


def looks_like_product(html: str) -> bool:
    return bool(meta(html, "og:title"))


class NykaaFashion(Shop):
    key = "nykaafashion"
    name = "Nykaa Fashion"
    domains = ("nykaafashion.com",)
    short_hosts = ("nykaafashion.app.link",)
    color = "#fc2779"
    example = "https://www.nykaafashion.com/<item>/p/<number>"

    def canonical_url(self, url: str) -> str:
        u = urlparse(url)
        m = ID_RE.search(u.path)
        if not m:
            raise ShopError("this Nykaa Fashion link is not a product page (it should contain /p/<number>)")
        return "https://www.nykaafashion.com" + u.path[: m.end()]

    async def load(self, fetcher: Fetcher, url: str) -> Product:
        url = self.canonical_url(url)
        f = ensure_ok(await fetcher.fetch(url))
        return self.parse(url, f.text)

    def parse(self, url: str, html: str) -> Product:
        states = page_states(html)
        # Prefer the product's own object so that "similar products" lists elsewhere
        # in the state never win.
        pid = ID_RE.search(url).group(1)
        for st in states:
            for obj in _objects_with_id(st, pid):
                sizes = sizes_from_dicts(find_size_dicts(obj))
                if sizes:
                    p = parse_page(self.key, url, html, state=obj)
                    p.sizes = sizes
                    p.title = first_str(obj, "title", "name") or p.title
                    p.brand = first_str(obj, "brandName", "brand") or p.brand
                    p.image = p.image or first_str(obj, "imageUrl", "image")
                    p.price = p.price or to_float(obj.get("discountedPrice") or obj.get("price"))
                    return p
        p = parse_page(self.key, url, html)
        if not p.sizes or p.sizes[0].id == "item" and p.sizes[0].available is None:
            raise ShopError("could not find the size list on this Nykaa Fashion page")
        return p


def _objects_with_id(obj, pid: str, depth: int = 0):
    if depth > 30:
        return
    if isinstance(obj, dict):
        for k in ("id", "productId", "product_id", "parentId"):
            if str(obj.get(k, "")) == pid:
                yield obj
                break
        for v in obj.values():
            yield from _objects_with_id(v, pid, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _objects_with_id(v, pid, depth + 1)
