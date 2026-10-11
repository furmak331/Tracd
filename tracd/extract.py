"""Helpers for pulling product data out of shop pages.

Shops change their page layout often, but the data behind the page (JSON-LD,
the state object the page boots from, the JSON its own app calls) changes far
less. Everything here works on that data, and the size finder at the bottom
looks for size lists by their shape rather than by a fixed path, so a renamed
wrapper object does not break a shop.
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Any, Iterable, Iterator

from .models import Size

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)


def first_url(text: str) -> str | None:
    """Pull the link out of whatever was pasted (share sheets add extra words)."""
    text = (text or "").strip()
    m = URL_RE.search(text)
    if m:
        return m.group(0).rstrip(").,;!?]")
    # "www.flipkart.com/..." without the scheme
    m = re.search(r"\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S*", text, re.I)
    if m:
        return "https://" + m.group(0).rstrip(").,;!?]")
    return None


# --------------------------------------------------------------------------
# Embedded JSON
# --------------------------------------------------------------------------


def raw_json_at(text: str, start: int) -> str | None:
    """Return the JSON object or array that begins at `start` (balanced braces,
    string aware), or None if it never closes."""
    if start < 0 or start >= len(text) or text[start] not in "{[":
        return None
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
            continue
        if c == '"' or c == "'":
            in_str, quote = True, c
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _loads_lenient(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        # Some pages embed JS object literals with `undefined` / NaN in them.
        fixed = re.sub(r"(?<=[:\[,])\s*(undefined|NaN|-?Infinity)\s*(?=[,}\]])", "null", raw)
        return json.loads(fixed)


_JS_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _js_unescape(s: str) -> str:
    def rep(m: re.Match) -> str:
        c = m.group(1)
        if c.startswith("u"):
            return chr(int(c[1:], 16))
        if c.startswith("x"):
            return chr(int(c[1:], 16))
        return _JS_ESC.get(c, c)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", rep, s, flags=re.S)


def assigned_json(text: str, *names: str) -> Any | None:
    """Find `window.NAME = {...}` (or `NAME = {...}`) and parse the object."""
    for name in names:
        for m in re.finditer(r"(?:window\.|window\[['\"])?" + re.escape(name) + r"['\"]?\]?\s*=\s*", text):
            j = m.end()
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in "{[":
                raw = raw_json_at(text, j)
                if raw:
                    try:
                        return _loads_lenient(raw)
                    except ValueError:
                        continue
            # window.X = JSON.parse("...")
            if text.startswith("JSON.parse(", j):
                k = j + len("JSON.parse(")
                q = text[k : k + 1]
                if q in "\"'":
                    end = k + 1
                    while end < len(text) and text[end] != q:
                        end += 2 if text[end] == "\\" else 1
                    try:
                        return _loads_lenient(_js_unescape(text[k + 1 : end]))
                    except ValueError:
                        continue
    return None


def json_ld(text: str) -> list[dict]:
    """Every JSON-LD object on the page, with @graph and arrays flattened."""
    out: list[dict] = []
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", text, re.S | re.I):
        body = m.group(1).strip()
        try:
            data = _loads_lenient(body)
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(reversed(d))
            elif isinstance(d, dict):
                if "@graph" in d and isinstance(d["@graph"], list):
                    stack.extend(reversed(d["@graph"]))
                else:
                    out.append(d)
    return out


def ld_products(text: str) -> list[dict]:
    def is_product(d: dict) -> bool:
        t = d.get("@type")
        types = t if isinstance(t, list) else [t]
        return any(str(x) in ("Product", "ProductGroup", "IndividualProduct") for x in types)

    return [d for d in json_ld(text) if is_product(d)]


def schema_availability(value: Any) -> bool | None:
    """Map a schema.org availability value to True / False / None."""
    v = str(value or "").rsplit("/", 1)[-1].strip().lower()
    if v in ("instock", "limitedavailability", "onlineonly", "instoreonly", "preorder", "presale", "backorder"):
        return True
    if v in ("outofstock", "soldout", "discontinued"):
        return False
    return None


def offers_of(product: dict) -> list[dict]:
    o = product.get("offers")
    if isinstance(o, dict):
        if isinstance(o.get("offers"), list):  # AggregateOffer
            return [x for x in o["offers"] if isinstance(x, dict)] or [o]
        return [o]
    if isinstance(o, list):
        return [x for x in o if isinstance(x, dict)]
    return []


def ld_image(product: dict) -> str | None:
    img = product.get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url") or img.get("contentUrl")
    return str(img) if img else None


def ld_brand(product: dict) -> str | None:
    b = product.get("brand")
    if isinstance(b, dict):
        b = b.get("name")
    if isinstance(b, list):
        b = b[0] if b else None
    return str(b) if b else None


# --------------------------------------------------------------------------
# Small text helpers
# --------------------------------------------------------------------------


def strip_tags(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def to_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, dict):
        for k in ("value", "doubleValue", "amount", "price", "formattedValue"):
            if k in v:
                return to_float(v[k])
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v))
    if not m:
        return None
    try:
        f = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return f if f > 0 else None


def to_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except ValueError:
        return None


BLOCK_PATTERNS = re.compile(
    r"captcha|are you a human|are you a robot|unusual traffic|access denied|request unsuccessful|"
    r"enter the characters you see|automated access|verify you are human|pardon our interruption|"
    r"incapsula|perimeterx|px-captcha|cf-chl-|just a moment\.\.\.|bot detection|reference #\d+\.",
    re.I,
)


def looks_blocked(text: str, status: int = 200) -> bool:
    """A verification / bot-wall page rather than real content."""
    if status in (403, 429):
        return True
    head = text[:60000]
    if not BLOCK_PATTERNS.search(head):
        return False
    # Product pages can mention "captcha" in a script; real walls are small.
    return len(text) < 250_000 or status >= 400


# --------------------------------------------------------------------------
# Size labels
# --------------------------------------------------------------------------

_SIZE_WORD = re.compile(
    r"^(?:(?:uk|us|eu|euro|ind|india|in|fr|it|jp|cm|w|waist)[\s/\-:]*)*"
    r"(?:\d{1,3}(?:\.\d)?(?:\s*[-/]\s*\d{1,3}(?:\.\d)?)?(?:\s*(?:y|yrs?|years?|m|months?|cm|in|inch))?"
    r"|x{0,4}s|x{0,4}l|m|xxxl|[2-6]xl|free\s*size|one\s*size|onesize|fs|std|standard)"
    r"(?:[\s\-]*(?:uk|us|eu|euro|ind|india|w|l))?"
    r"(?:\s*[\(/].*)?$",
    re.I,
)


def is_size_label(label: Any) -> bool:
    s = str(label or "").strip()
    return 0 < len(s) <= 24 and bool(_SIZE_WORD.match(s))


def norm_label(label: str) -> str:
    """Normalise a size label for comparing ("UK/IND-9" == "UK 9" == "9 UK")."""
    s = re.sub(r"\bINDIA\b|\bIND\b|(?<=UK/)IND|^IND", " ", str(label).upper())
    s = re.sub(r"[^A-Z0-9.]+", " ", s).strip()
    parts = sorted(s.split())
    return " ".join(parts)


# --------------------------------------------------------------------------
# Finding size lists by shape
# --------------------------------------------------------------------------

ID_KEYS = ("skuId", "childId", "skuCode", "sku", "productCode", "variantId", "code", "asin", "pid", "id", "itemId")
LABEL_KEYS = (
    "scDisplaySize", "sizeLabel", "displaySize", "sizeName", "label", "variantName",
    "size", "sizeValue", "unifiedSize", "name", "title", "value",
)
PRICE_KEYS = ("offerPrice", "discountedPrice", "sellingPrice", "specialPrice", "finalPrice", "salePrice", "price", "priceData")
MRP_KEYS = ("mrp", "wasPriceData", "listPrice", "maxPrice")
QTY_KEYS = ("availableCount", "quantity", "stockCount", "inventory", "stockLevel", "qty", "availableQuantity")


def _truthy(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    if isinstance(v, str):
        s = v.strip().lower().replace("_", "").replace(" ", "")
        if s in ("true", "yes", "instock", "available", "lowstock", "limitedstock", "1", "y"):
            return True
        if s in ("false", "no", "outofstock", "soldout", "unavailable", "notavailable", "0", "n", "nostock"):
            return False
    return None


def stock_of(d: dict) -> bool | None:
    """Read 'is this size buyable' from a variant-ish dict, whatever it calls it."""
    for k in ("available", "isAvailable", "inStock", "in_stock", "isInStock", "buyable", "isBuyable", "availability"):
        if k in d:
            t = _truthy(d[k])
            if t is None and isinstance(d[k], str):
                t = schema_availability(d[k])
            if t is not None:
                return t
    for k in ("isOutOfStock", "outOfStock", "is_out_of_stock", "soldOut", "isSoldOut"):
        if k in d:
            t = _truthy(d[k])
            if t is not None:
                return not t
    st = d.get("stock")
    if isinstance(st, dict):
        for k in ("stockLevelStatus", "status", "availability"):
            if k in st:
                t = _truthy(st[k])
                if t is not None:
                    return t
        lvl = to_int(st.get("stockLevel"))
        if lvl is not None:
            return lvl > 0
    elif st is not None:
        t = _truthy(st)
        if t is not None:
            return t
    for k in ("stockLevelStatus", "stockStatus", "inventoryStatus"):
        if k in d:
            t = _truthy(d[k])
            if t is not None:
                return t
    sellers = d.get("sizeSellerData")
    if isinstance(sellers, list) and sellers:
        n = sum(to_int(s.get("availableCount")) or 0 for s in sellers if isinstance(s, dict))
        return n > 0
    for k in QTY_KEYS:
        if k in d and not isinstance(d[k], (dict, list)):
            n = to_int(d[k])
            if n is not None:
                return n > 0
    return None


def qty_of(d: dict) -> int | None:
    sellers = d.get("sizeSellerData")
    if isinstance(sellers, list) and sellers:
        n = sum(to_int(s.get("availableCount")) or 0 for s in sellers if isinstance(s, dict))
        return n if n > 0 else None
    st = d.get("stock")
    if isinstance(st, dict):
        n = to_int(st.get("stockLevel"))
        if n:
            return n if n > 0 else None
    for k in QTY_KEYS:
        if k in d and not isinstance(d[k], (dict, list, bool)):
            n = to_int(d[k])
            if n is not None:
                return n if n > 0 else None
    return None


def price_of(d: dict, keys: Iterable[str] = PRICE_KEYS) -> float | None:
    for k in keys:
        if k in d:
            p = to_float(d[k])
            if p:
                return p
    sellers = d.get("sizeSellerData")
    if isinstance(sellers, list):
        for s in sellers:
            if isinstance(s, dict):
                p = to_float(s.get("discountedPrice") or s.get("sellingPrice"))
                if p:
                    return p
    return None


def label_of(d: dict) -> str | None:
    for k in LABEL_KEYS:
        v = d.get(k)
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip():
            if k in ("name", "title", "value") and not is_size_label(v):
                continue
            return str(v).strip()
    quals = d.get("variantOptionQualifiers")
    if isinstance(quals, list):
        for q in quals:
            if isinstance(q, dict) and "size" in str(q.get("qualifier", "")).lower() and q.get("value"):
                return str(q["value"]).strip()
    return None


def id_of(d: dict) -> str | None:
    for k in ID_KEYS:
        v = d.get(k)
        if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip():
            return str(v).strip()
    return None


def _walk(obj: Any, depth: int = 0) -> Iterator[list]:
    if depth > 40:
        return
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            yield obj
        for x in obj:
            yield from _walk(x, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v, depth + 1)


def size_list_score(items: list[dict]) -> float:
    if not items:
        return 0
    labelled = [label_of(x) for x in items]
    if not all(labelled):
        return 0
    if len(set(labelled)) != len(labelled):
        return 0
    sizey = sum(1 for lab in labelled if is_size_label(lab))
    stocked = sum(1 for x in items if stock_of(x) is not None)
    ids = sum(1 for x in items if id_of(x))
    if sizey < max(1, len(items) * 0.6):
        return 0
    return sizey + stocked * 1.5 + ids * 0.5 + (2 if len(items) > 1 else 0)


def find_size_dicts(data: Any) -> list[dict]:
    """The list of dicts in `data` that looks most like a size list."""
    best: list[dict] = []
    best_score = 0.0
    for lst in _walk(data):
        sc = size_list_score(lst)
        if sc > best_score:
            best, best_score = lst, sc
    return best


def sizes_from_dicts(items: list[dict]) -> list[Size]:
    out: list[Size] = []
    for d in items:
        label = label_of(d)
        if not label:
            continue
        out.append(
            Size(
                id=id_of(d) or label,
                label=label,
                available=stock_of(d),
                price=price_of(d),
                qty=qty_of(d),
            )
        )
    return out


def find_sizes(data: Any) -> list[Size]:
    return sizes_from_dicts(find_size_dicts(data))


def deep_find(obj: Any, key: str, max_nodes: int = 200_000) -> Iterator[Any]:
    """Yield every value stored under `key` inside obj, shallowest first."""
    queue = [obj]
    seen = 0
    while queue and seen < max_nodes:
        nxt = []
        for o in queue:
            seen += 1
            if isinstance(o, dict):
                if key in o:
                    yield o[key]
                nxt.extend(v for v in o.values() if isinstance(v, (dict, list)))
            elif isinstance(o, list):
                nxt.extend(v for v in o if isinstance(v, (dict, list)))
        queue = nxt


def first_str(obj: Any, *keys: str) -> str | None:
    for k in keys:
        for v in deep_find(obj, k):
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None
