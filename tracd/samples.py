"""Sample shop pages, built the way each shop builds its real ones.

Used by the tests, and by `tracd --demo`, which runs the whole app against these
instead of the real shops (stock flips every so often, so you can see an alert
end to end without waiting for a real restock).
"""

from __future__ import annotations

import html as _html
import json
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from .models import Fetched


@dataclass
class SampleSize:
    id: str
    label: str
    available: bool
    qty: int = 0


@dataclass
class SampleItem:
    shop: str
    url: str
    title: str
    brand: str
    price: float
    mrp: float
    sizes: list[SampleSize]
    image: str = ""
    colour: str = "Birch"
    item_id: str = ""  # Flipkart itm id / Myntra style / AJIO code / Nykaa product id / ASIN
    extra: dict = field(default_factory=dict)

    def size(self, sid: str) -> SampleSize | None:
        return next((s for s in self.sizes if s.id == sid), None)


def shoe_image(hue: int = 28, label: str = "") -> str:
    """A small product picture for samples (no network needed)."""
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'>
<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='hsl({hue},60%,92%)'/>
<stop offset='1' stop-color='hsl({hue},45%,80%)'/></linearGradient></defs>
<rect width='200' height='200' fill='url(#g)'/>
<path d='M30 128c18 2 40-6 58-24 6 10 18 16 32 18 18 2 34 8 44 18 4 4 4 12-2 14H34c-8 0-10-6-4-26z' fill='hsl({hue},30%,35%)'/>
<path d='M32 142h134' stroke='hsl({hue},20%,96%)' stroke-width='6' stroke-linecap='round'/>
<text x='100' y='178' font-family='sans-serif' font-size='13' text-anchor='middle' fill='hsl({hue},30%,30%)'>{_html.escape(label)}</text>
</svg>"""
    return "data:image/svg+xml;utf8," + re.sub(r"\s+", " ", svg).replace("#", "%23").replace("<", "%3C").replace(">", "%3E").replace("'", "%27")


def shoe_sizes(prefix: str, labels: list[str], stock: dict[str, int]) -> list[SampleSize]:
    return [SampleSize(f"{prefix}{i}", lab, stock.get(lab, 0) > 0, stock.get(lab, 0)) for i, lab in enumerate(labels)]


# --------------------------------------------------------------------------
# Page builders, one per shop
# --------------------------------------------------------------------------


def _ld(obj: dict) -> str:
    return '<script type="application/ld+json">' + json.dumps(obj) + "</script>"


def _shell(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_html.escape(title)}</title>"
        f"<meta property='og:title' content='{_html.escape(title, quote=True)}'>"
        "<script>window.dataLayer = window.dataLayer || [];</script>"
        f"</head><body><div id='root'></div>{body}</body></html>"
    )


def flipkart_page(item: SampleItem, pid: str, legacy_swatch: bool = False) -> str:
    """A product page the way Flipkart builds it: JSON-LD for the pid in the
    link, and a size selector whose tiles carry each size's pid and stock."""
    s = item.size(pid) or item.sizes[0]
    path = urlparse(item.url).path
    if legacy_swatch:
        labels = [x.label for x in item.sizes]
        products = {x.id: {"attributeIndexes": [0, i], "url": f"{path}?pid={x.id}"} for i, x in enumerate(item.sizes)}
        products["OTHERCOLOURPID01"] = {"attributeIndexes": [1, 0], "url": "/x/p/itmother?pid=OTHERCOLOURPID01"}
        selector = {"10002": [{"widget": {"type": "SWATCH_VARIANTS", "data": {"swatchComponent": {"value": {
            "attributeOptions": [[{"value": item.colour}, {"value": "Black"}], [{"value": lab} for lab in labels]],
            "attributes": [{"id": "color", "display": "Colour"}, {"id": "size", "display": "Size - UK/India"}],
            "products": products,
            "selectedSwatch": {"pid": s.id},
        }}}}}]}
        state = {"pageDataV4": {"page": {"data": selector}}}
    else:
        def tracking(x: SampleSize) -> dict:
            word = "OutOfStock" if not x.available else "Scarcity" if (x.qty or 9) < 3 else "InStock"
            return {"moduleId": "ATLAS_SWATCH_ATTRIBUTE", "contentType": f"{word}:Variant", "productId": x.id,
                    "contentTitle": x.label, "widgetTitle": "N", "widgetType": "atlas_swatch_attribute"}

        tiles = [{"value": {
            "trackerData_0": {"tracking": tracking(x)},
            "label_0": {"value": {"text": x.label}},
            # Every tile's tap action is tracked against the page's own pid.
            "col_0": {"action": {"url": f"{path}?pid={x.id}&swatchAttr=size", "tracking": {
                "moduleId": "ATLAS_SWATCH_ATTRIBUTE", "contentType": "InStock:Variant", "productId": s.id,
                "contentTitle": "Custom Amount", "eventName": "PSOS", "selectedPid": x.id, "selectedOptionValue": x.label}}},
        }} for x in item.sizes]
        dls = {"size-selector-view_0": {"value": {
            "selector-header_0": {"value": {"label_0": {"value": {"text": "Select Size"}}}},
            "scrollToListData_0": {"value": tiles},
        }}}
        state = {"multiWidgetState": {"widgetsData": {"slots": [{"slotData": {"widget": {"data": {"dlsData": dls}}}}]}}}
    state["recommendations"] = [{"title": "Similar products", "items": [{"value": "Puma Smash", "pid": "SHOXXXXXXXXXXXXX"}]}]
    ld = {
        "@context": "https://schema.org", "@type": "Product", "name": item.title, "sku": s.id,
        "image": item.image, "brand": {"@type": "Brand", "name": item.brand}, "color": item.colour,
        "offers": {"@type": "Offer", "priceCurrency": "INR", "price": item.price,
                   "availability": "http://schema.org/" + ("InStock" if s.available else "OutOfStock")},
    }
    body = (
        "<script>window.__INITIAL_STATE__ = " + json.dumps(state) + ";</script>"
        + "".join(f'<a href="{path}?pid={x.id}&amp;lid=LST"><div>{x.label}</div></a>' for x in item.sizes)
        + '<table><tr><td><div>UK/India Size</div></td><td>' + s.label + '</td></tr></table>'
        + _ld({"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []})
        + _ld([ld])
    )
    return _shell(item.title, body)


def tatacliq_json(item: SampleItem, code: str) -> str:
    me = item.size(code) or item.sizes[0]
    return json.dumps({
        "type": "productDetailsWsDTO", "status": "Success", "productListingId": me.id,
        "productTitle": item.title, "brandName": item.brand, "productColor": item.colour,
        "winningSellerPrice": {"doubleValue": item.price, "formattedValue": f"₹{int(item.price)}"},
        "mrpPrice": {"doubleValue": item.mrp},
        "winningSellerAvailableStock": str(me.qty if me.available else 0),
        "galleryImagesList": [{"mediaType": "Image", "galleryImages": [{"key": "product", "value": item.image}]}],
        # Tata CLiQ marks only the size in the link as "selected".
        "variantOptions": [
            {"colorlink": {"color": item.colour or "Birch & Driftwood", "selected": x.id == me.id},
             "sizelink": {"size": x.label, "productCode": x.id, "isAvailable": x.available,
                          "url": f"/x/p-{x.id.lower()}"}}
            for x in item.sizes
        ] + [{"colorlink": {"color": "Black", "selected": False},
              "sizelink": {"size": item.sizes[0].label, "productCode": "MP000000099999999", "isAvailable": True}}],
    })


def nykaa_page(item: SampleItem) -> str:
    state = {
        "productPage": {"product": {
            "id": item.item_id, "name": item.title, "brandName": item.brand, "imageUrl": item.image,
            "offerPrice": item.price, "mrp": item.mrp,
            "variants": [{"childId": x.id, "variantName": x.label, "inStock": x.available, "quantity": x.qty,
                          "offerPrice": item.price, "mrp": item.mrp} for x in item.sizes],
        }},
        "widgets": {"similar": [{"name": "Other sneaker", "variantName": "UK 8", "inStock": True}]},
    }
    return _shell(item.title, "<script>window.__PRELOADED_STATE__ = " + json.dumps(state) + "</script>")


def myntra_page(item: SampleItem) -> str:
    pdp = {
        "id": int(item.item_id or 0), "name": item.title, "brand": {"name": item.brand},
        "price": {"mrp": item.mrp, "discounted": item.price},
        "baseColour": item.colour,
        "media": {"albums": [{"name": "default", "images": [{"imageURL": item.image}]}]},
        "sizes": [{
            "skuId": int(x.id), "styleId": int(item.item_id or 0), "label": x.label, "available": x.available,
            "allSizesList": [{"scaleCode": "uk_size", "sizeValue": x.label}],
            "sizeSellerData": [{"sellerPartnerId": 1, "availableCount": x.qty if x.available else 0, "discountedPrice": item.price}],
        } for x in item.sizes],
    }
    return _shell(item.title, "<script>window.__myx = " + json.dumps({"pdpData": pdp, "deviceData": {}}) + "</script>")


def ajio_json(item: SampleItem) -> str:
    return json.dumps({
        "code": item.item_id, "name": item.title, "brandName": item.brand,
        "price": {"value": item.price, "formattedValue": f"Rs. {int(item.price)}"},
        "wasPriceData": {"value": item.mrp},
        "images": [{"format": "productDetail", "url": item.image}],
        "fnlColorVariantData": {"color": item.colour, "brandName": item.brand},
        "variantOptions": [{
            "code": x.id, "scDisplaySize": x.label,
            "variantOptionQualifiers": [{"qualifier": "size", "value": x.label.split()[-1]}],
            "stock": {"stockLevelStatus": "inStock" if x.available else "outOfStock", "stockLevel": x.qty if x.available else 0},
            "priceData": {"value": item.price},
        } for x in item.sizes],
    })


def amazon_page(item: SampleItem, asin: str) -> str:
    me = item.size(asin) or item.sizes[0]
    display = {x.id: [x.label, item.colour] for x in item.sizes}
    display["B0OTHERCLR"] = [item.sizes[0].label, "Black"]
    avail = ('<span class="a-size-medium a-color-success">In stock</span>' if me.available
             else '<span class="a-size-medium a-color-price">Currently unavailable.</span>')
    cart = '<input id="add-to-cart-button" type="submit">' if me.available else ""
    body = (
        f'<input type="hidden" id="ASIN" name="ASIN" value="{me.id}">'
        f'<a id="bylineInfo" href="/stores/x">Visit the {item.brand} Store</a>'
        f'<span id="productTitle" class="a-size-large">   {_html.escape(item.title)}   </span>'
        f'<div id="corePriceDisplay_desktop_feature_div"><span class="a-price-whole">{int(item.price):,}</span></div>'
        f'<img id="landingImage" data-old-hires="{item.image}" src="{item.image}">'
        f'<div id="availability" class="a-section">{avail}</div>{cart}'
        '<script type="text/javascript">P.register("twister-js-init-dpx-data", function() { var dataToReturn = {'
        f'"currentAsin" : "{me.id}", "parentAsin" : "B0PARENT00", '
        '"dimensions" : ["size_name","color_name"], '
        f'"variationValues" : {json.dumps({"size_name": [x.label for x in item.sizes], "color_name": [item.colour, "Black"]})}, '
        f'"dimensionValuesDisplayData" : {json.dumps(display)} '
        "}; return dataToReturn; });</script>"
    )
    return _shell(item.title, body)


def nykaafashion_page(item: SampleItem) -> str:
    state = {
        "details": {"skuData": {"product": {
            "id": item.item_id, "title": item.title, "brandName": item.brand, "imageUrl": item.image,
            "discountedPrice": item.price, "price": item.mrp,
            "sizeOptions": [{"sku": x.id, "sizeName": x.label, "isOutOfStock": not x.available, "quantity": x.qty}
                            for x in item.sizes],
        }}},
        "listing": {"products": [{"id": "1", "title": "Other", "sizeOptions": [{"sizeName": "M", "isOutOfStock": False}]}]},
    }
    return _shell(item.title, "<script>window.__PRELOADED_STATE__ = " + json.dumps(state) + ";</script>")


# --------------------------------------------------------------------------
# A fetcher that serves the samples
# --------------------------------------------------------------------------


class SampleFetcher:
    """Serves SampleItems as if it were the shops. Tests flip `available` on the
    sizes, or set `fail` / `block` to simulate trouble."""

    def __init__(self, items: list[SampleItem], *, delay: float = 0.0):
        self.items = items
        self.delay = delay
        self.calls: list[str] = []
        self.block: set[str] = set()  # hosts that show a verification page
        self.fail: set[str] = set()  # hosts that answer 500
        self.redirects: dict[str, str] = {}

    def _find(self, shop: str, pred) -> SampleItem | None:
        return next((i for i in self.items if i.shop == shop and pred(i)), None)

    async def final_url(self, url: str) -> str:
        return self.redirects.get(url, url)

    async def fetch(self, url: str, *, kind: str = "html", page_url: str | None = None) -> Fetched:
        import asyncio

        self.calls.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        host = urlparse(url).hostname or ""
        if host in self.block:
            return Fetched(200, "<html><title>Access Denied</title>Please verify you are human (captcha)</html>", url)
        if host in self.fail:
            return Fetched(500, "oops", url)
        body = self._body(url)
        return Fetched(200, body, url, 5) if body is not None else Fetched(404, "not found", url)

    def _body(self, url: str) -> str | None:
        u = urlparse(url)
        path, q = u.path, parse_qs(u.query)
        host = u.hostname or ""
        if host.endswith("flipkart.com"):
            pid = (q.get("pid") or [""])[0]
            it = self._find("flipkart", lambda i: path.startswith(urlparse(i.url).path))
            return flipkart_page(it, pid or it.sizes[0].id) if it else None
        if host.endswith("tatacliq.com"):
            m = re.search(r"productDetails/(mp\d+)", path, re.I)
            code = m.group(1).upper() if m else ""
            it = self._find("tatacliq", lambda i: i.size(code) is not None)
            return tatacliq_json(it, code) if it else json.dumps({"status": "FAILURE", "error": "Product not found"})
        if host.endswith("nykaaman.com") or host.endswith("nykaa.com"):
            it = self._find("nykaa", lambda i: f"/p/{i.item_id}" in path)
            return nykaa_page(it) if it else None
        if host.endswith("nykaafashion.com"):
            it = self._find("nykaafashion", lambda i: f"/p/{i.item_id}" in path)
            return nykaafashion_page(it) if it else None
        if host.endswith("myntra.com"):
            it = self._find("myntra", lambda i: f"/{i.item_id}" in path)
            return myntra_page(it) if it else None
        if host.endswith("ajio.com"):
            m = re.search(r"/api/p/([^/?]+)", path)
            if m:
                it = self._find("ajio", lambda i: i.item_id == m.group(1))
                return ajio_json(it) if it else None
            it = self._find("ajio", lambda i: path == urlparse(i.url).path)
            return ("<script>window.__PRELOADED_STATE__ = {\"product\": {\"productDetails\": "
                    + ajio_json(it) + "}};</script>") if it else None
        if host.endswith("amazon.in"):
            m = re.search(r"/dp/([A-Z0-9]{10})", path)
            it = self._find("amazon", lambda i: m and i.size(m.group(1)) is not None) if m else None
            return amazon_page(it, m.group(1)) if it else None
        return None


def demo_catalog() -> list[SampleItem]:
    uk = ["UK 6", "UK 7", "UK 8", "UK 9", "UK 10", "UK 11"]
    return [
        SampleItem("flipkart", "https://www.flipkart.com/asics-gel-nyc-sneakers-men/p/itm9ed5f92dc1b32",
                   "ASICS GEL-NYC Sneakers For Men", "ASICS", 11999, 11999,
                   [SampleSize(f"SHOHGFW7FVKGBQ{c}", lab.split()[-1], lab != "UK 9", 3) for c, lab in zip("PQRTUV", uk)],
                   image=shoe_image(30, "GEL-NYC"), item_id="itm9ed5f92dc1b32"),
        SampleItem("tatacliq", "https://www.tatacliq.com/asics-mens-gel-nyc-birch-driftwood-running-shoes/p-mp000000029497693",
                   "ASICS Men's GEL-NYC Birch & Driftwood Running Shoes", "ASICS", 11999, 11999,
                   [SampleSize(f"MP0000000294976{90 + i}", f"UK/IND-{lab.split()[-1]}", lab in ("UK 7", "UK 10"), 2)
                    for i, lab in enumerate(uk)],
                   image=shoe_image(22, "GEL-NYC")),
        SampleItem("nykaa", "https://www.nykaaman.com/asics-gel-nyc-unisex-sneakers/p/18954652",
                   "ASICS GEL-NYC Unisex Sneakers", "ASICS", 11999, 11999,
                   shoe_sizes("189545", uk, {"UK 8": 2, "UK 10": 1}), image=shoe_image(215, "GEL-NYC"), item_id="18954652"),
        SampleItem("myntra", "https://www.myntra.com/casual-shoes/new-balance/new-balance-men-530-sneakers/24681357/buy",
                   "New Balance Men 530 Sneakers", "New Balance", 9999, 10999,
                   [SampleSize(str(80000 + i), lab.split()[-1], lab != "UK 9", 4) for i, lab in enumerate(uk)],
                   image=shoe_image(0, "530"), item_id="24681357"),
        SampleItem("ajio", "https://www.ajio.com/puma-men-palermo-sneakers/p/469583961_white",
                   "Puma Men Palermo Lace-Up Sneakers", "PUMA", 7999, 9999,
                   [SampleSize(f"4695839610{i:02d}", lab, lab == "UK 8", 1) for i, lab in enumerate(uk)],
                   image=shoe_image(150, "Palermo"), item_id="469583961_white"),
        SampleItem("amazon", "https://www.amazon.in/dp/B0CSAMBA03",
                   "adidas Originals Men's Samba OG Sneakers", "adidas", 10999, 10999,
                   [SampleSize(f"B0CSAMBA0{i}", lab, lab != "UK 9", 5) for i, lab in enumerate(uk)],
                   image=shoe_image(260, "Samba"), item_id="B0CSAMBA03"),
    ]


class DemoFetcher(SampleFetcher):
    """Samples that come and go: every so often a sold-out size restocks for a
    while, so the dashboard and phone alerts can be tried without waiting."""

    def __init__(self, period: float = 45.0):
        super().__init__(demo_catalog(), delay=0.25)
        self.period = period
        self._next = time.monotonic() + period
        self._restocked: list[tuple[SampleSize, float]] = []

    def _tick(self) -> None:
        now = time.monotonic()
        for s, until in list(self._restocked):
            if now >= until:
                s.available = False
                self._restocked.remove((s, until))
        if now >= self._next:
            self._next = now + self.period
            out = [s for i in self.items for s in i.sizes if not s.available]
            if out:
                s = random.choice(out)
                s.available, s.qty = True, random.randint(1, 4)
                self._restocked.append((s, now + self.period * 0.6))

    async def fetch(self, url: str, *, kind: str = "html", page_url: str | None = None) -> Fetched:
        self._tick()
        return await super().fetch(url, kind=kind, page_url=page_url)
