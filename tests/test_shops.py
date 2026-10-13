import json

import pytest

from tracd.models import Blocked, Fetched, ShopError, Stock
from tracd.samples import SampleFetcher, amazon_page, demo_catalog
from tracd.shops import GENERIC, shop_for
from tracd.shops.ajio import Ajio
from tracd.shops.amazon import Amazon
from tracd.shops.flipkart import Flipkart
from tracd.shops.generic import Generic, parse_page, parse_shopify
from tracd.shops.myntra import Myntra
from tracd.shops.nykaa import Nykaa, NykaaFashion
from tracd.shops.tatacliq import TataCliq


class StaticFetcher:
    """Answers fixed bodies by URL substring."""

    def __init__(self, routes: dict[str, tuple[int, str]]):
        self.routes = routes
        self.calls: list[str] = []

    async def fetch(self, url, *, kind="html", page_url=None):
        self.calls.append(url)
        for k, (status, body) in self.routes.items():
            if k in url:
                return Fetched(status, body, url)
        return Fetched(404, "nope", url)

    async def final_url(self, url):
        return url


# --------------------------------------------------------------------------
# routing and links
# --------------------------------------------------------------------------


@pytest.mark.parametrize("url,key", [
    ("https://www.flipkart.com/x/p/itm123?pid=ABC", "flipkart"),
    ("https://dl.flipkart.com/s/aBc12", "flipkart"),
    ("https://www.myntra.com/shoes/x/y/123456/buy", "myntra"),
    ("https://myntr.it/abc", "myntra"),
    ("https://www.ajio.com/x/p/469583961_white", "ajio"),
    ("https://www.amazon.in/x/dp/B0ABCDEF12", "amazon"),
    ("https://amzn.in/d/abc", "amazon"),
    ("https://www.tatacliq.com/x/p-mp000000029497697", "tatacliq"),
    ("https://www.nykaaman.com/x/p/18954652?skuId=1", "nykaa"),
    ("https://www.nykaa.com/x/p/123", "nykaa"),
    ("https://www.nykaafashion.com/x/p/123", "nykaafashion"),
    ("https://www.bewakoof.com/p/some-tee", "other"),
])
def test_shop_for(url, key):
    assert shop_for(url).key == key


def test_canonical_urls():
    assert Flipkart().canonical_url("https://dl.flipkart.com/dl/asics-gel/p/itm9ed5f92dc1b32?pid=SHOX&lid=L&marketplace=FLIPKART") == \
        "https://www.flipkart.com/asics-gel/p/itm9ed5f92dc1b32?pid=SHOX"
    assert Flipkart().canonical_url("https://www.flipkart.com/a/p/itmabc/extra?x=1") == "https://www.flipkart.com/a/p/itmabc"
    assert TataCliq().canonical_url("https://www.tatacliq.com/a-b/p-mp000000029497697?utm=x#top") == "https://www.tatacliq.com/a-b/p-mp000000029497697"
    assert TataCliq().code("https://www.tatacliq.com/a/p-mp000000029497697") == "MP000000029497697"
    assert Nykaa().canonical_url("https://nykaaman.com/a/p/18954652?productId=1&skuId=2") == "https://www.nykaaman.com/a/p/18954652"
    assert Myntra().canonical_url("https://www.myntra.com/casual-shoes/asics/x/24681357/buy?src=share") == \
        "https://www.myntra.com/casual-shoes/asics/x/24681357/buy"
    assert Myntra().canonical_url("https://www.myntra.com/24681357") == "https://www.myntra.com/24681357"
    assert Ajio().canonical_url("https://www.ajio.com/puma/p/469583961_white?utm=1") == "https://www.ajio.com/puma/p/469583961_white"
    assert Amazon().canonical_url("https://www.amazon.in/adidas-Samba/dp/B0CSAMBA03/ref=sr_1?th=1") == "https://www.amazon.in/dp/B0CSAMBA03"
    assert Amazon().canonical_url("https://www.amazon.in/gp/product/B0CSAMBA03") == "https://www.amazon.in/dp/B0CSAMBA03"
    assert Flipkart().is_short_link("https://dl.flipkart.com/s/abc")
    assert not Flipkart().is_short_link("https://dl.flipkart.com/dl/x/p/itm1?pid=A")


@pytest.mark.parametrize("shop,url", [
    (Flipkart(), "https://www.flipkart.com/search?q=asics"),
    (TataCliq(), "https://www.tatacliq.com/mens-footwear/c-msh13"),
    (Nykaa(), "https://www.nykaaman.com/brands/asics"),
    (Myntra(), "https://www.myntra.com/men-sneakers"),
    (Ajio(), "https://www.ajio.com/men-sneakers/c/830207010"),
    (Amazon(), "https://www.amazon.in/s?k=samba"),
])
def test_non_product_links_get_a_friendly_error(shop, url):
    with pytest.raises(ShopError) as e:
        shop.canonical_url(url)
    assert "not a product page" in e.value.detail


# --------------------------------------------------------------------------
# each shop against pages built like the real ones
# --------------------------------------------------------------------------


async def test_flipkart_lists_sizes_and_reads_the_linked_size(fetcher):
    url = "https://www.flipkart.com/asics-gel-nyc-sneakers-men/p/itm9ed5f92dc1b32?pid=SHOHGFW7FVKGBQT"
    p = await Flipkart().load(fetcher, url)
    assert p.title == "ASICS GEL-NYC Sneakers For Men"
    assert p.brand == "ASICS" and p.price == 11999
    assert [s.label for s in p.sizes] == ["UK 6", "UK 7", "UK 8", "UK 9", "UK 10", "UK 11"]  # other colour left out
    assert p.selected_size_id == "SHOHGFW7FVKGBQT"
    assert p.size("SHOHGFW7FVKGBQT").available is False
    assert p.size("SHOHGFW7FVKGBQT").url.endswith("?pid=SHOHGFW7FVKGBQT")
    assert Flipkart().reading(p, "SHOHGFW7FVKGBQT", "UK 9").state == Stock.OUT
    # Each size is checked on its own page.
    assert Flipkart().poll_url(p.url, "SHOHGFW7FVKGBQP", None).endswith("?pid=SHOHGFW7FVKGBQP")


async def test_flipkart_restock_is_seen(catalog, fetcher):
    catalog[0].size("SHOHGFW7FVKGBQT").available = True
    shop = Flipkart()
    p = await shop.load(fetcher, shop.poll_url(catalog[0].url, "SHOHGFW7FVKGBQT", None))
    r = shop.reading(p, "SHOHGFW7FVKGBQT", "UK 9")
    assert r.state == Stock.IN_STOCK and r.price == 11999


def test_flipkart_size_tiles_give_every_size_its_stock(catalog):
    """Flipkart's size selector says which sizes are buyable; "few left" counts as in stock."""
    from tracd.samples import flipkart_page

    item = catalog[0]
    item.size("SHOHGFW7FVKGBQP").qty = 1  # UK 6: "Scarcity"
    url = item.url + "?pid=SHOHGFW7FVKGBQR"
    p = Flipkart().parse(url, flipkart_page(item, "SHOHGFW7FVKGBQR"))
    assert [(s.label, s.available) for s in p.sizes] == [
        ("UK 6", True), ("UK 7", True), ("UK 8", True), ("UK 9", False), ("UK 10", True), ("UK 11", True)]
    assert p.selected_size_id == "SHOHGFW7FVKGBQR"


def test_flipkart_older_swatch_layout_still_reads(catalog):
    from tracd.samples import flipkart_page

    item = catalog[0]
    url = item.url + "?pid=SHOHGFW7FVKGBQT"
    p = Flipkart().parse(url, flipkart_page(item, "SHOHGFW7FVKGBQT", legacy_swatch=True))
    assert [s.label for s in p.sizes] == ["UK 6", "UK 7", "UK 8", "UK 9", "UK 10", "UK 11"]
    assert p.size("SHOHGFW7FVKGBQT").available is False


def test_flipkart_different_variant_is_unknown_not_a_false_alert():
    html = '<script type="application/ld+json">{"@type":"Product","name":"X","sku":"OTHERPID00000001","offers":{"price":1,"availability":"InStock"}}</script>'
    shop = Flipkart()
    p = shop.parse("https://www.flipkart.com/x/p/itmabc?pid=WANTEDPID0000001", html)
    r = shop.reading(p, "WANTEDPID0000001", "UK 9")
    assert r.state == Stock.UNKNOWN


def test_flipkart_product_data_without_a_sku_is_not_trusted_for_the_size_in_the_link():
    html = '<script type="application/ld+json">{"@type":"Product","name":"X","offers":{"price":1,"availability":"InStock"}}</script>'
    shop = Flipkart()
    p = shop.parse("https://www.flipkart.com/x/p/itmabc?pid=WANTEDPID0000001", html)
    assert shop.reading(p, "WANTEDPID0000001", "Size in your link").state == Stock.UNKNOWN


def test_flipkart_size_tile_and_product_data_disagreeing_is_unknown(catalog):
    from tracd.samples import flipkart_page

    item = catalog[0]
    pid = "SHOHGFW7FVKGBQT"  # UK 9, sold out in the tiles
    html = flipkart_page(item, pid).replace("schema.org/OutOfStock", "schema.org/InStock")
    shop = Flipkart()
    p = shop.parse(item.url + "?pid=" + pid, html)
    assert shop.reading(p, pid, "UK 9").state == Stock.UNKNOWN


def test_flipkart_falls_back_to_size_links_when_swatch_data_moves():
    html = (
        '<script>window.__INITIAL_STATE__ = {"somethingElse": {}};</script>'
        '<a href="/asics/p/itmabc?pid=SHOAAAAAAAAAAAA1&amp;lid=1"><div>8</div></a>'
        '<a href="/asics/p/itmabc?pid=SHOAAAAAAAAAAAA2&amp;lid=1"><div>9</div></a>'
        '<a href="/asics/p/itmabc?pid=SHOAAAAAAAAAAAA3&amp;lid=1"><div>10</div></a>'
        '<a href="/asics/p/itmzzz?pid=SHOBBBBBBBBBBBB1"><div>Puma Smash</div></a>'
        '<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Product","name":"ASICS",'
        '"sku":"SHOAAAAAAAAAAAA2","offers":{"@type":"Offer","price":"11999","availability":"https://schema.org/InStock"}}]}</script>'
    )
    p = Flipkart().parse("https://www.flipkart.com/asics/p/itmabc?pid=SHOAAAAAAAAAAAA2", html)
    assert [s.label for s in p.sizes] == ["8", "9", "10"]
    assert p.size("SHOAAAAAAAAAAAA2").available is True
    assert p.note is None


def test_flipkart_without_any_size_list_asks_for_the_size_link():
    html = '<script type="application/ld+json">{"@type":"Product","name":"Tee","sku":"TSHAAAAAAAAAAAA1","offers":{"price":499,"availability":"http://schema.org/OutOfStock"}}</script>'
    p = Flipkart().parse("https://www.flipkart.com/tee/p/itmabc?pid=TSHAAAAAAAAAAAA1", html)
    assert len(p.sizes) == 1 and p.sizes[0].available is False
    assert "Choose your size on Flipkart" in p.note


def test_flipkart_page_without_product_data():
    with pytest.raises(ShopError):
        Flipkart().parse("https://www.flipkart.com/a/p/itmabc", "<html><body>Hello</body></html>")


async def test_tatacliq_reads_all_sizes_from_one_call(catalog, fetcher):
    url = "https://www.tatacliq.com/asics-mens-gel-nyc-birch-driftwood-running-shoes/p-mp000000029497693"
    p = await TataCliq().load(fetcher, url)
    assert [s.label for s in p.sizes] == ["UK 6", "UK 7", "UK 8", "UK 9", "UK 10", "UK 11"]
    assert p.selected_size_id == "MP000000029497693"
    assert p.size("MP000000029497691").available is True
    assert p.size("MP000000029497693").available is False
    assert p.title.startswith("ASICS Men's GEL-NYC")
    assert len(fetcher.calls) == 1 and "productDetails/mp000000029497693" in fetcher.calls[0]
    # The stored label from the shop's own spelling still matches.
    assert TataCliq().reading(p, "MP000000029497693", "UK/IND-9").state == Stock.OUT


def test_tatacliq_keeps_every_size_of_the_linked_colour():
    """Only the size in the link is marked selected; its colour decides the list."""
    def v(code, size, colour, sel, avail):
        return {"colorlink": {"color": colour, "selected": sel},
                "sizelink": {"size": size, "productCode": code, "isAvailable": avail}}

    body = json.dumps({"productListingId": "MP0000003", "productTitle": "Shoe", "variantOptions": [
        v("MP0000003", "UK/IND-3", "Birch & Driftwood", True, False),
        v("MP0000009", "UK/IND-9", "Birch & Driftwood", False, False),
        v("MP0000008", "UK/IND-8", "Birch & Driftwood", False, True),
        v("MP0000099", "UK/IND-9", "Black", False, True),
    ]})
    p = TataCliq().parse("https://www.tatacliq.com/x/p-mp0000003", "MP0000003", body)
    assert [(s.label, s.available) for s in p.sizes] == [("UK 3", False), ("UK 9", False), ("UK 8", True)]


async def test_tatacliq_seller_stock_counts_for_the_listing():
    body = json.dumps({
        "productListingId": "MP1000001", "productTitle": "Shoe", "winningSellerAvailableStock": "3",
        "winningSellerPrice": {"doubleValue": 999},
        "variantOptions": [{"sizelink": {"size": "UK/IND-9", "productCode": "MP1000001", "isAvailable": False}}],
    })
    p = TataCliq().parse("https://www.tatacliq.com/x/p-mp1000001", "MP1000001", body)
    s = p.size("MP1000001")
    assert s.available is True and s.qty == 3


async def test_tatacliq_error_reply():
    f = StaticFetcher({"productDetails": (200, json.dumps({"status": "FAILURE", "error": "Product not found"}))})
    with pytest.raises(ShopError) as e:
        await TataCliq().load(f, "https://www.tatacliq.com/x/p-mp000000000000001")
    assert "could not find" in e.value.detail


async def test_nykaa_man(fetcher):
    url = "https://www.nykaaman.com/asics-gel-nyc-unisex-sneakers/p/18954652?productId=18954652&pps=5&skuId=1895453"
    p = await Nykaa().load(fetcher, url)
    assert [s.label for s in p.sizes] == ["UK 6", "UK 7", "UK 8", "UK 9", "UK 10", "UK 11"]  # similar items ignored
    assert p.selected_size_id == "1895453"
    assert p.size("1895452").available is True and p.size("1895452").qty == 2
    assert p.size("1895453").available is False
    assert p.url == "https://www.nykaaman.com/asics-gel-nyc-unisex-sneakers/p/18954652"
    assert Nykaa().reading(p, "1895452", "UK 8").state == Stock.IN_STOCK


def test_nykaa_size_id_now_means_another_size_is_unknown(catalog):
    from tracd.samples import nykaa_page

    p = Nykaa().parse("https://www.nykaaman.com/a/p/18954652", nykaa_page(catalog[2]))
    r = Nykaa().reading(p, "1895453", "UK 10")
    assert r.state == Stock.UNKNOWN and "expected" in r.detail


async def test_nykaa_fashion_uses_the_products_own_object(catalog):
    from tracd.samples import SampleItem, SampleSize

    item = SampleItem("nykaafashion", "https://www.nykaafashion.com/levis-501/p/7654321", "Levi's 501 Jeans", "Levi's", 2999, 3999,
                      [SampleSize("S1", "28", False), SampleSize("S2", "30", True, 2), SampleSize("S3", "32", False)], item_id="7654321")
    f = SampleFetcher([item])
    p = await NykaaFashion().load(f, item.url + "?adsource=x")
    assert p.title == "Levi's 501 Jeans"
    assert [(s.label, s.available) for s in p.sizes] == [("28", False), ("30", True), ("32", False)]


async def test_myntra(fetcher):
    url = "https://www.myntra.com/casual-shoes/new-balance/new-balance-men-530-sneakers/24681357/buy"
    p = await Myntra().load(fetcher, url)
    assert p.title == "New Balance Men 530 Sneakers" and p.brand == "New Balance"
    assert p.price == 9999 and p.mrp == 10999
    assert [s.label for s in p.sizes][:2] == ["UK 6", "UK 7"]
    assert p.size("80003").available is False
    assert p.size("80000").qty == 4
    assert p.colour == "Birch"


def test_myntra_page_without_data_is_an_error():
    with pytest.raises(ShopError):
        Myntra().parse("https://www.myntra.com/123456/buy", "<html><title>Myntra</title></html>")


async def test_ajio_api(fetcher):
    p = await Ajio().load(fetcher, "https://www.ajio.com/puma-men-palermo-sneakers/p/469583961_white")
    assert p.title == "Puma Men Palermo Lace-Up Sneakers" and p.brand == "PUMA"
    assert p.price == 7999 and p.mrp == 9999
    assert [s.label for s in p.sizes] == ["UK 6", "UK 7", "UK 8", "UK 9", "UK 10", "UK 11"]
    assert p.size("469583961002").available is True and p.size("469583961002").qty == 1
    assert fetcher.calls == ["https://www.ajio.com/api/p/469583961_white"]


async def test_ajio_falls_back_to_the_page_when_the_api_moves(catalog):
    from tracd.samples import ajio_json

    item = catalog[4]
    page = "<script>window.__PRELOADED_STATE__ = {\"product\": {\"productDetails\": " + ajio_json(item) + "}};</script>"
    f = StaticFetcher({"/api/p/": (404, "gone"), "/p/469583961_white": (200, page)})
    p = await Ajio().load(f, item.url)
    assert len(p.sizes) == 6
    assert len(f.calls) == 2


async def test_ajio_busy_api_is_not_reported_as_a_missing_page():
    f = StaticFetcher({"/api/p/": (503, "busy"), "/p/123_red": (404, "not found")})
    with pytest.raises(ShopError) as e:
        await Ajio().load(f, "https://www.ajio.com/x/p/123_red")
    assert "503" in e.value.detail and "no longer exists" not in e.value.detail


async def test_ajio_blocked_api_is_not_retried_on_the_page():
    f = StaticFetcher({"/api/p/": (403, "Access Denied")})
    with pytest.raises(Blocked):
        await Ajio().load(f, "https://www.ajio.com/x/p/123_red")
    assert len(f.calls) == 1


async def test_amazon(fetcher, catalog):
    shop = Amazon()
    p = await shop.load(fetcher, "https://www.amazon.in/adidas-Samba/dp/B0CSAMBA03/ref=x")
    assert p.title == "adidas Originals Men's Samba OG Sneakers"
    assert p.brand == "adidas" and p.price == 10999
    assert [s.label for s in p.sizes] == ["UK 6", "UK 7", "UK 8", "UK 9", "UK 10", "UK 11"]
    assert p.selected_size_id == "B0CSAMBA03"
    assert shop.reading(p, "B0CSAMBA03", "UK 9").state == Stock.OUT
    catalog[5].size("B0CSAMBA03").available = True
    p = await shop.load(fetcher, shop.poll_url(p.url, "B0CSAMBA03", None))
    assert shop.reading(p, "B0CSAMBA03", "UK 9").state == Stock.IN_STOCK


@pytest.mark.parametrize("box,cart,want", [
    ('<span>In stock</span>', True, True),
    ('<span>Only 2 left in stock.</span>', True, True),
    ('<span>Currently unavailable.</span><span>We don\'t know when or if this item will be back in stock.</span>', False, False),
    ('<span>Temporarily out of stock.</span>', False, False),
    ('<span>Usually dispatched in 2 to 3 days.</span>', True, True),
    ('', True, True),
    ('', False, None),
])
def test_amazon_availability_box(box, cart, want):
    from tracd.shops.amazon import availability

    html = f'<span id="productTitle">X</span><div id="availability" class="a-section">{box}</div>' + ('<input id="add-to-cart-button">' if cart else "")
    assert availability(html)[0] is want


def test_amazon_only_other_sellers_is_buyable_with_a_note():
    html = ('<span id="productTitle">Shoe</span><div id="availability"><span></span></div>'
            '<div id="buybox-see-all-buying-choices"><a>See All Buying Options</a></div>')
    p = Amazon().parse("B0AAAAAAA1", html)
    r = Amazon().reading(p, "B0AAAAAAA1", "This item")
    assert r.state == Stock.IN_STOCK and r.detail == "only from other sellers"


async def test_amazon_captcha_is_blocked():
    f = StaticFetcher({"/dp/": (200, "<html><form action='/errors/validateCaptcha'>Enter the characters you see below</form></html>")})
    with pytest.raises(Blocked):
        await Amazon().load(f, "https://www.amazon.in/dp/B0AAAAAAA1")


def test_amazon_newer_variation_layout(catalog):
    item = catalog[5]
    html = amazon_page(item, "B0CSAMBA03")
    html = html.replace('"dimensionValuesDisplayData"', '"unused"')
    html = html.replace("};", ', "dimensionToAsinMap" : {"0_0":"B0CSAMBA00","1_0":"B0CSAMBA01","3_0":"B0CSAMBA03","3_1":"B0OTHERCLR"}, '
                        '"dimensionValuesData" : [["UK 6","UK 7","UK 8","UK 9"],["Birch","Black"]] };', 1)
    p = Amazon().parse("B0CSAMBA03", html)
    assert [s.label for s in p.sizes] == ["UK 6", "UK 7", "UK 9"]


# --------------------------------------------------------------------------
# blocked and broken answers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status,body,exc_state", [
    (403, "Access Denied", Stock.BLOCKED),
    (429, "slow down", Stock.BLOCKED),
    (200, "<html>Please verify you are human (captcha)</html>", Stock.BLOCKED),
    (500, "oops", Stock.UNKNOWN),
    (0, "timed out", Stock.UNKNOWN),
    (404, "not found", Stock.UNKNOWN),
])
async def test_trouble_maps_to_the_right_state(status, body, exc_state):
    f = StaticFetcher({"flipkart": (status, body)})
    with pytest.raises(ShopError) as e:
        await Flipkart().load(f, "https://www.flipkart.com/a/p/itmabc?pid=SHOAAAAAAAAAAAA1")
    assert e.value.state == exc_state


# --------------------------------------------------------------------------
# any other shop
# --------------------------------------------------------------------------


def test_shopify_variant_feed():
    feed = json.dumps({
        "title": "Comet Lows", "vendor": "Comet", "price": 349900, "featured_image": "//cdn.shopify.com/a.jpg",
        "options": ["Size"],
        "variants": [
            {"id": 11, "title": "UK 8", "option1": "UK 8", "available": True, "price": 349900},
            {"id": 12, "title": "UK 9", "option1": "UK 9", "available": False, "price": 349900},
        ],
    })
    p = parse_shopify("https://comet.in/products/comet-lows", feed)
    assert p.title == "Comet Lows" and p.price == 3499.0
    assert p.image == "https://cdn.shopify.com/a.jpg"
    assert [(s.id, s.label, s.available) for s in p.sizes] == [("11", "UK 8", True), ("12", "UK 9", False)]


async def test_generic_shop_detects_shopify():
    feed = json.dumps({"title": "Tee", "options": [{"name": "Size"}], "variants": [{"id": 1, "title": "M", "option1": "M", "available": True, "price": 99900}]})
    f = StaticFetcher({".js": (200, feed), "/products/tee": (200, "<html><script src='https://cdn.shopify.com/x.js'></script></html>")})
    p = await Generic().load(f, "https://brand.in/products/tee")
    assert p.sizes[0].label == "M" and p.sizes[0].available is True


def test_generic_json_ld_product_group():
    html = """<script type="application/ld+json">{"@context":"https://schema.org","@type":"ProductGroup","name":"Runner",
      "brand":{"name":"Acme"},"image":["https://x/y.jpg"],
      "hasVariant":[{"@type":"Product","sku":"r-8","size":"8","offers":{"price":"1999","availability":"InStock"}},
                    {"@type":"Product","sku":"r-9","size":"9","offers":{"price":"1999","availability":"OutOfStock"}}]}</script>"""
    p = parse_page("other", "https://acme.in/runner", html)
    assert p.title == "Runner" and p.brand == "Acme" and p.image == "https://x/y.jpg"
    assert [(s.id, s.label, s.available) for s in p.sizes] == [("r-8", "8", True), ("r-9", "9", False)]


def test_generic_single_product():
    html = '<script type="application/ld+json">{"@type":"Product","name":"Bag","offers":{"price":"999","availability":"OutOfStock"}}</script>'
    p = parse_page("other", "https://x.in/bag", html)
    assert [(s.id, s.available) for s in p.sizes] == [("item", False)]
    assert GENERIC.reading(p, "item", "One size").state == Stock.OUT


def test_demo_catalog_reads_on_every_shop():
    # Guards the samples themselves, which the demo and the UI test rely on.
    assert {i.shop for i in demo_catalog()} == {"flipkart", "tatacliq", "nykaa", "myntra", "ajio", "amazon"}


async def test_every_demo_example_link_reads():
    """The example links offered in demo mode must all work."""
    from tracd.samples import DemoFetcher
    from tracd.shops import shop_for

    f = DemoFetcher()
    for item in f.items:
        p = await shop_for(item.url).load(f, item.url)
        assert len(p.sizes) == len(item.sizes), item.shop


class _OnePage:
    def __init__(self, html):
        self.html = html

    async def fetch(self, url, *, kind="html", page_url=None):
        return Fetched(200, self.html, url)


async def test_links_from_a_page_are_only_kept_when_safe_to_open():
    offers = [
        {"@type": "Offer", "name": "S", "sku": "s", "availability": "InStock", "url": "javascript:alert(1)"},
        {"@type": "Offer", "name": "M", "sku": "m", "availability": "InStock", "url": "https://evil.example/phish"},
        {"@type": "Offer", "name": "L", "sku": "l", "availability": "InStock", "url": "/p/shirt?size=l"},
        {"@type": "Offer", "name": "XL", "sku": "xl", "availability": "InStock", "url": "https://m.shop.example/p?x"},
    ]
    ld = {"@context": "https://schema.org", "@type": "Product", "name": "Shirt", "image": "javascript:x()",
          "offers": offers}
    html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    p = await GENERIC.read(_OnePage(html), "https://www.shop.example/p/shirt")
    urls = {s.label: s.url for s in p.sizes}
    assert urls["S"] is None and urls["M"] is None
    assert urls["L"] == "https://www.shop.example/p/shirt?size=l"
    assert urls["XL"] == "https://m.shop.example/p?x"
    assert p.image is None
    assert p.url.startswith("https://www.shop.example/")


def test_inline_product_pictures_are_kept_but_scripts_are_not():
    from tracd.shops.base import safe_image

    assert safe_image("data:image/svg+xml;utf8,<svg/>", "https://a.example/").startswith("data:image/svg+xml")
    assert safe_image("data:text/html,<script>x</script>", "https://a.example/") is None
    assert safe_image("//cdn.example/x.jpg", "https://a.example/p") == "https://cdn.example/x.jpg"


async def test_demo_products_keep_their_pictures(fetcher, catalog):
    for item in catalog:
        from tracd.shops import BY_KEY

        p = await BY_KEY[item.shop].read(fetcher, item.url)
        assert p.image, item.shop


def test_a_stored_link_that_is_not_a_web_link_is_never_opened():
    from tracd.store import Watch

    w = Watch(id=1, shop="other", product_url="https://a.example/p", poll_url="https://a.example/p", title="t",
              size_id="s", size_label="S", size_url="javascript:alert(1)")
    assert w.open_url == "https://a.example/p"
