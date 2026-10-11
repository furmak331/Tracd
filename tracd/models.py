"""Plain data types shared by the shop readers, the checker and the web app."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Stock(str, Enum):
    IN_STOCK = "IN_STOCK"
    OUT = "OUT"
    UNKNOWN = "UNKNOWN"  # the shop answered but we could not read this size
    BLOCKED = "BLOCKED"  # the shop showed a verification page or refused us

    @property
    def definite(self) -> bool:
        return self in (Stock.IN_STOCK, Stock.OUT)


@dataclass
class Size:
    """One size of a product, as the shop lists it."""

    id: str  # the shop's own id for this size (pid, skuId, ASIN, product code...)
    label: str  # what the shop shows, e.g. "UK 9"
    available: bool | None = None  # None = the shop did not say
    price: float | None = None
    qty: int | None = None  # units left, when the shop tells us
    url: str | None = None  # a link that opens this size directly, when one exists

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Product:
    shop: str  # shop key, e.g. "flipkart"
    url: str  # canonical product link
    title: str
    sizes: list[Size] = field(default_factory=list)
    brand: str | None = None
    image: str | None = None
    price: float | None = None
    mrp: float | None = None
    colour: str | None = None
    selected_size_id: str | None = None  # the size the pasted link points at, if any
    note: str | None = None  # anything the user should know about this listing

    def size(self, size_id: str) -> Size | None:
        for s in self.sizes:
            if s.id == size_id:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sizes"] = [s.to_dict() for s in self.sizes]
        return d


@dataclass
class Reading:
    """What one check found out about one watched size."""

    state: Stock
    detail: str = ""
    price: float | None = None
    qty: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state.value, "detail": self.detail, "price": self.price, "qty": self.qty}


class ShopError(Exception):
    """A check could not produce an answer. `state` says whether we were blocked."""

    def __init__(self, detail: str, state: Stock = Stock.UNKNOWN):
        super().__init__(detail)
        self.detail = detail
        self.state = state


class Blocked(ShopError):
    def __init__(self, detail: str = "the shop is showing a verification page"):
        super().__init__(detail, Stock.BLOCKED)


@dataclass
class Fetched:
    """A response fetched from a shop."""

    status: int
    text: str
    url: str
    elapsed_ms: int = 0
