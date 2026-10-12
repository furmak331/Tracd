# Contributing to Tracd

Thanks for helping. The most useful thing you can do is tell us when a shop stops reading, or add a shop that isn't supported yet.

## A shop isn't reading

1. Run `tracd check <the product link> --size "UK 9" --save dump/`
   (inside the folder: `.venv\Scripts\python -m tracd ...` on Windows, `.venv/bin/python -m tracd ...` elsewhere).
2. Open a **Shop not reading** issue, paste what it printed and attach the `dump/` folder as a zip.

That dump is exactly what the shop sent, which is all that's needed to fix its reader. It contains no cookies or personal data.

## Set up for development

```sh
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[test]"
python -m playwright install chromium
pytest
```

The tests run offline: shops are stood in for by sample pages and request interception.

## Adding or fixing a shop reader

- Readers live in `tracd/shops/`. `base.py` describes what a reader returns; `myntra.py` and `tatacliq.py` are short examples.
- Read the data behind the page (JSON-LD, the page's boot state, or the shop's own API), not the layout.
- If you can't tell whether a size is in stock, return "unknown". A false "in stock" is the worst bug Tracd can have.
- Add a sample page to `tracd/samples.py` built from a real `--save` dump (strip anything personal) and a test in `tests/test_shops.py`.

## Pull requests

- One change per PR, with a test.
- `pytest` must pass. CI runs on Windows, macOS and Linux.
- Say which real page you checked it against, if any.

## Ground rules

Tracd never logs in, never adds to cart, never buys, and never tries to get around a shop's verification page. PRs that change that won't be merged.
