import json

from tracd.extract import (
    assigned_json,
    find_sizes,
    first_url,
    is_size_label,
    json_ld,
    looks_blocked,
    norm_label,
    raw_json_at,
    schema_availability,
    stock_of,
    to_float,
)


def test_first_url_from_share_text():
    assert first_url("Check out this on Flipkart! https://dl.flipkart.com/s/abcd12 hurry") == "https://dl.flipkart.com/s/abcd12"
    assert first_url("(https://www.myntra.com/123/buy).") == "https://www.myntra.com/123/buy"
    assert first_url("www.ajio.com/x/p/123_red") == "https://www.ajio.com/x/p/123_red"
    assert first_url("no link here") is None
    assert first_url("") is None


def test_raw_json_at_handles_strings_with_braces_and_quotes():
    text = 'x = {"a": "}{ \\" \'", "b": [1, {"c": "Levi\'s"}]}; y = 1'
    raw = raw_json_at(text, text.index("{"))
    assert json.loads(raw) == {"a": "}{ \" '", "b": [1, {"c": "Levi's"}]}
    assert raw_json_at("{ never closes", 0) is None
    assert raw_json_at("abc", 0) is None


def test_assigned_json_forms():
    assert assigned_json('<script>window.__myx = {"a": 1};</script>', "__myx") == {"a": 1}
    assert assigned_json('<script>window["__STATE__"]={"b":2}</script>', "__STATE__") == {"b": 2}
    assert assigned_json('window.__PRELOADED_STATE__ = {"x": undefined, "y": NaN}', "__PRELOADED_STATE__") == {"x": None, "y": None}
    enc = json.dumps(json.dumps({"q": "it's \"ok\""}))
    assert assigned_json(f"window.__S__ = JSON.parse({enc});", "__S__") == {"q": 'it\'s "ok"'}
    assert assigned_json("window.__S__ = JSON.parse('{\\\"k\\\":\\u0022v\\u0022}');", "__S__") == {"k": "v"}
    assert assigned_json("nothing here", "__myx") is None


def test_json_ld_flattens_graph_and_arrays_and_skips_bad_blocks():
    html = (
        '<script type="application/ld+json">{"@graph":[{"@type":"BreadcrumbList"},{"@type":"Product","name":"A"}]}</script>'
        '<script type="application/ld+json">[{"@type":"Product","name":"B"}]</script>'
        '<script type="application/ld+json">{not json</script>'
    )
    names = [d.get("name") for d in json_ld(html)]
    assert names == [None, "A", "B"]


def test_schema_availability():
    assert schema_availability("http://schema.org/InStock") is True
    assert schema_availability("https://schema.org/LimitedAvailability") is True
    assert schema_availability("OutOfStock") is False
    assert schema_availability("SoldOut") is False
    assert schema_availability("") is None
    assert schema_availability("Whatever") is None


def test_size_labels():
    for ok in ["UK 9", "9", "UK/IND-9", "9 UK", "EU 42", "8.5", "M", "XL", "XXL", "3XL", "Free Size", "28", "30W", "2-3Y", "UK 9 (EU 43)"]:
        assert is_size_label(ok), ok
    for bad in ["ASICS GEL-NYC Sneakers For Men", "Black", "Birch/Driftwood", "", "Similar products"]:
        assert not is_size_label(bad), bad


def test_norm_label_matches_shop_spellings():
    assert norm_label("UK/IND-9") == norm_label("UK 9") == norm_label("9 UK")
    assert norm_label("UK 9") != norm_label("UK 9.5")
    assert norm_label("India 9") == norm_label("9")


def test_stock_of_reads_many_spellings():
    assert stock_of({"available": True}) is True
    assert stock_of({"inStock": False}) is False
    assert stock_of({"isOutOfStock": True}) is False
    assert stock_of({"stock": {"stockLevelStatus": "inStock"}}) is True
    assert stock_of({"stock": {"stockLevelStatus": "outOfStock"}}) is False
    assert stock_of({"stockLevelStatus": "lowStock"}) is True
    assert stock_of({"sizeSellerData": [{"availableCount": 0}, {"availableCount": 2}]}) is True
    assert stock_of({"quantity": 0}) is False
    assert stock_of({"label": "9"}) is None


def test_find_sizes_prefers_the_size_list_over_other_lists():
    state = {
        "nav": [{"name": "Men", "id": 1}, {"name": "Women", "id": 2}],
        "similar": [{"title": "Other shoe", "price": 100}],
        "product": {"variants": [
            {"sku": "a", "size": "UK 7", "inStock": True, "quantity": 3},
            {"sku": "b", "size": "UK 8", "inStock": False, "quantity": 0},
            {"sku": "c", "size": "UK 9", "inStock": False, "quantity": 0},
        ]},
    }
    sizes = find_sizes(state)
    assert [(s.id, s.label, s.available) for s in sizes] == [("a", "UK 7", True), ("b", "UK 8", False), ("c", "UK 9", False)]
    assert sizes[0].qty == 3


def test_to_float():
    assert to_float("₹11,999.00") == 11999.0
    assert to_float({"doubleValue": 999}) == 999.0
    assert to_float({"value": "1,299"}) == 1299.0
    assert to_float(0) is None
    assert to_float(None) is None
    assert to_float(True) is None


def test_looks_blocked():
    assert looks_blocked("anything", 403)
    assert looks_blocked("<html>Please verify you are human. captcha</html>")
    assert looks_blocked("<form action='/errors/validateCaptcha'>Enter the characters you see below</form>")
    assert not looks_blocked("<html>normal product page</html>")
    # A large real page that mentions captcha in a script is not a wall.
    assert not looks_blocked("<script>var captchaEnabled=false;</script>" + "x" * 300000)
