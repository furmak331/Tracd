"""Tata CLiQ.

Each size has its own product code (MP0000...), and the shop's own product API
returns the whole size list with `sizelink.isAvailable` for each one, so one
request answers every size of the item.
"""

from __future__ import annotations

import re
import time

from ..extract import to_float, to_int
from ..models import Product, ShopError, Size
from .base import Fetcher, Shop, ensure_ok

CODE_RE = re.compile(r"/p-(mp\d{6,})", re.I)
API = "https://www.tatacliq.com/marketplacewebservices/v2/mpl/products/productDetails/{code}?isPwa=true&isMDE=true&isDynamicVar=true&_={ts}"


class TataCliq(Shop):
    key = "tatacliq"
    name = "Tata CLiQ"
    domains = ("tatacliq.com",)
    short_hosts = ("tatacliq.page.link",)
    color = "#d1105a"
    example = "https://www.tatacliq.com/asics-mens-gel-nyc-birch-driftwood-running-shoes/p-mp000000029497697"

    def code(self, url: str) -> str:
        m = CODE_RE.search(url)
        if not m:
            raise ShopError("this Tata CLiQ link is not a product page (it should contain /p-mp...)")
        return m.group(1).upper()

    def canonical_url(self, url: str) -> str:
        m = CODE_RE.search(url)
        if not m:
            raise ShopError("this Tata CLiQ link is not a product page (it should contain /p-mp...)")
        path = url.split("?")[0].split("#")[0]
        path = path[path.index("tatacliq.com") + len("tatacliq.com"):]
        return "https://www.tatacliq.com" + path

    async def load(self, fetcher: Fetcher, url: str) -> Product:
        url = self.canonical_url(url)
        code = self.code(url)
        f = ensure_ok(await fetcher.fetch(API.format(code=code.lower(), ts=int(time.time() * 1000)), kind="json", page_url=url), "product")
        return self.parse(url, code, f.text)

    def parse(self, url: str, code: str, text: str) -> Product:
        import json

        try:
            j = json.loads(text)
        except ValueError:
            raise ShopError("Tata CLiQ sent something that is not product data")
        if not isinstance(j, dict) or (j.get("status") and str(j.get("status")).lower() in ("failure", "error")):
            raise ShopError("Tata CLiQ could not find this product" + (f" ({j.get('error')})" if isinstance(j, dict) and j.get("error") else ""))
        variants = [v for v in (j.get("variantOptions") or []) if isinstance(v, dict) and isinstance(v.get("sizelink"), dict)]
        # Keep this colour only, when the reply says which colour is selected.
        # Every size of every colour is listed; "selected" marks only the size in
        # the link, so keep the sizes that share its colour name.
        def colour(v):
            c = v.get("colorlink")
            return str(c.get("color") or "").strip().lower() if isinstance(c, dict) else ""

        mine = next((colour(v) for v in variants if isinstance(v.get("colorlink"), dict) and v["colorlink"].get("selected") is True), "")
        if mine:
            variants = [v for v in variants if colour(v) == mine]

        listing = str(j.get("productListingId") or "").upper()
        seller_stock = to_int(j.get("winningSellerAvailableStock"))
        price = to_float(j.get("winningSellerPrice")) or to_float(j.get("mrpPrice"))
        sizes: list[Size] = []
        seen: set[str] = set()
        for v in variants:
            sl = v["sizelink"]
            pc = str(sl.get("productCode") or "").upper()
            label = re.sub(r"^UK/IND-\s*", "UK ", str(sl.get("size") or "").strip())
            if not pc or not label or label in seen:
                continue
            seen.add(label)
            avail = sl.get("isAvailable") if isinstance(sl.get("isAvailable"), bool) else None
            qty = to_int(sl.get("stockCount"))
            if pc == listing and seller_stock is not None:
                avail = bool(avail) or seller_stock > 0
                qty = qty or (seller_stock if seller_stock > 0 else None)
            link = sl.get("url")
            sizes.append(
                Size(
                    pc,
                    label,
                    avail,
                    price=price if pc == listing else None,
                    qty=qty if qty and qty > 0 else None,
                    url=("https://www.tatacliq.com" + link) if isinstance(link, str) and link.startswith("/") else None,
                )
            )
        if not sizes:
            raise ShopError("no size list in Tata CLiQ's reply")

        image = None
        for g in j.get("galleryImagesList") or []:
            for im in (g or {}).get("galleryImages") or []:
                if isinstance(im, dict) and im.get("value") and im.get("key") in ("product", "zoom", "superZoom", "cartPage", None):
                    image = im["value"]
                    break
            if image:
                break
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        return Product(
            shop=self.key,
            url=url,
            title=str(j.get("productTitle") or j.get("productName") or "Tata CLiQ product"),
            brand=j.get("brandName"),
            image=image,
            price=price,
            mrp=to_float(j.get("mrpPrice")),
            colour=j.get("productColor") or j.get("colorfamily") or j.get("colour"),
            sizes=sizes,
            selected_size_id=code if any(s.id == code for s in sizes) else None,
        )
