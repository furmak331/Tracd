"""Shop readers. `shop_for(url)` picks the right one for a link."""

from __future__ import annotations

from .ajio import Ajio
from .amazon import Amazon
from .base import Fetcher, Shop
from .flipkart import Flipkart
from .generic import Generic
from .myntra import Myntra
from .nykaa import Nykaa, NykaaFashion
from .tatacliq import TataCliq

SHOPS: list[Shop] = [Flipkart(), Myntra(), Ajio(), Amazon(), TataCliq(), NykaaFashion(), Nykaa()]
GENERIC = Generic()
BY_KEY: dict[str, Shop] = {s.key: s for s in SHOPS + [GENERIC]}


def shop_for(url: str) -> Shop:
    for s in SHOPS:
        if s.matches(url) or s.is_short_link(url):
            return s
    return GENERIC


def is_short_link(url: str) -> bool:
    return any(s.is_short_link(url) for s in SHOPS)


__all__ = ["SHOPS", "GENERIC", "BY_KEY", "Fetcher", "Shop", "shop_for", "is_short_link"]
