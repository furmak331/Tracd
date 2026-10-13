"""Shops change their pages without warning. Whatever a page looks like, a
reader must either read it or say it can't: never crash, never hang."""

import json
import random

import pytest

from tracd.models import Fetched, Product, ShopError
from tracd.samples import SampleFetcher, demo_catalog
from tracd.shops import BY_KEY

ODD = [None, [], {}, "", "x", 0, -1, 1e308, True, [None], {"a": None}, "UK 9", [[]]]


def mutate_json(obj, rng):
    if isinstance(obj, dict) and obj:
        k = rng.choice(list(obj))
        if rng.random() < 0.5:
            obj[k] = rng.choice(ODD)
        else:
            mutate_json(obj[k], rng)
    elif isinstance(obj, list) and obj:
        i = rng.randrange(len(obj))
        if rng.random() < 0.5:
            obj[i] = rng.choice(ODD)
        else:
            mutate_json(obj[i], rng)
    return obj


def mutate_text(text, rng):
    n = len(text)
    a = rng.randrange(n)
    b = min(n, a + rng.randint(1, 400))
    roll = rng.random()
    if roll < 0.4:
        return text[:a] + text[b:]
    if roll < 0.7:
        return text[:a]
    return text[:a] + rng.choice(['"', "{", "}", "null", "[]", "<script>", "\\u0000", "é"]) + text[b:]


class Mutating(SampleFetcher):
    def __init__(self, items, rng):
        super().__init__(items)
        self.rng = rng

    async def fetch(self, url, *, kind="html", page_url=None):
        f = await super().fetch(url, kind=kind, page_url=page_url)
        text = f.text
        try:
            text = json.dumps(mutate_json(json.loads(text), self.rng))
        except ValueError:
            text = mutate_text(text, self.rng)
        return Fetched(f.status, text, f.url)


@pytest.mark.parametrize("shop_key", sorted({i.shop for i in demo_catalog()}))
async def test_readers_never_crash_on_odd_pages(shop_key):
    rng = random.Random(shop_key)
    items = demo_catalog()
    item = next(i for i in items if i.shop == shop_key)
    shop = BY_KEY[shop_key]
    read = 0
    for _ in range(300):
        try:
            p = await shop.read(Mutating(items, rng), item.url)
        except ShopError:
            continue
        assert isinstance(p, Product)
        for s in p.sizes:
            shop.reading(p, s.id, s.label)
            assert s.url is None or s.url.startswith("https://")
        read += 1
    assert read > 0  # small changes still read
