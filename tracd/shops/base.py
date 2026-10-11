"""What every shop reader provides."""

from __future__ import annotations

from typing import Protocol
from urllib.parse import parse_qs, urlparse

from ..extract import looks_blocked, norm_label
from ..models import Blocked, Fetched, Product, Reading, ShopError, Stock


class Fetcher(Protocol):
    async def fetch(self, url: str, *, kind: str = "html", page_url: str | None = None) -> Fetched:
        """GET `url` from inside the shop's own tab and return the body. `page_url`
        is the page to open that tab on if it is not open yet (for API calls)."""

    async def final_url(self, url: str) -> str:
        """Follow a short share link (dl.flipkart.com/s/..., amzn.in/d/...) to the product page."""


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def query(url: str, key: str) -> str | None:
    v = parse_qs(urlparse(url).query).get(key)
    return v[0] if v else None


def ensure_ok(f: Fetched, what: str = "page") -> Fetched:
    """Turn bot walls and HTTP errors into the right exception."""
    if looks_blocked(f.text, f.status):
        raise Blocked("verification page" if f.status < 400 else f"the shop refused the request (HTTP {f.status})")
    if f.status == 404 or f.status == 410:
        raise ShopError(f"the shop says this {what} no longer exists (HTTP {f.status})")
    if f.status >= 400 or f.status == 0:
        raise ShopError(f"the shop answered HTTP {f.status}" if f.status else f"no answer from the shop ({f.text[:120]})")
    return f


class Shop:
    key: str = ""
    name: str = ""
    domains: tuple[str, ...] = ()
    color: str = "#64748b"  # brand colour for the dashboard badge
    # Hosts that are share/short links for this shop. They are followed with the
    # browser to find the real product URL.
    short_hosts: tuple[str, ...] = ()
    example: str = ""

    def matches(self, url: str) -> bool:
        h = host_of(url)
        return any(h == d or h.endswith("." + d) for d in self.domains)

    def is_short_link(self, url: str) -> bool:
        h = host_of(url)
        return any(h == d or h.endswith("." + d) for d in self.short_hosts)

    def canonical_url(self, url: str) -> str:
        """The clean product link (raises ShopError if it is not a product link)."""
        return url

    async def load(self, fetcher: Fetcher, url: str) -> Product:
        raise NotImplementedError

    def poll_url(self, product_url: str, size_id: str, size_url: str | None) -> str:
        """The link to fetch on each check. Most shops list every size on the
        product page; shops where each size is its own page override this."""
        return product_url

    def reading(self, product: Product, size_id: str, size_label: str) -> Reading:
        s = product.size(size_id)
        if s is None:
            return Reading(Stock.UNKNOWN, "this size is no longer listed on the page")
        if size_label and norm_label(s.label) != norm_label(size_label):
            return Reading(Stock.UNKNOWN, f"the size id now shows {s.label!r}, expected {size_label!r}")
        if s.available is None:
            return Reading(Stock.UNKNOWN, "the page does not say whether this size is in stock", s.price)
        if s.available:
            return Reading(Stock.IN_STOCK, "in stock", s.price or product.price, s.qty)
        return Reading(Stock.OUT, "sold out", s.price or product.price)

    def info(self) -> dict:
        return {"key": self.key, "name": self.name, "color": self.color, "example": self.example}
