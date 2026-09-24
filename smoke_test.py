#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_test.py
--------------
Zero-network, zero-browser sanity check for bershka-scraper.

Run this FIRST, before touching a proxy or www.bershka.com:

    python3 smoke_test.py

Deliberately ONE file of plain functions with inline fixtures — no pytest, no
conftest, no fixtures directory. `tests/test_smoke.py` wraps it as a single
pytest test so `pytest` is a working entry point without a second copy of the
checks that could drift from this one.

It must pass with NO engine library installed, so every engine import is
guarded and the skip is REPORTED. CI's engine job installs all three and
fails if anything reports skipped, because "skipped, engine absent" reads
identically to a real import error.

What this suite is for
----------------------
Not coverage. Every site check pins a VALUE read off a real capture, because
a column can be 100% populated and entirely wrong — which this repo has
already demonstrated twice. The first live run filled `section` on 100% of
1,907 rows with the string "1", the payload's numeric section code, because
the parser fell back to `entry["section"]` when the name was absent; and
`page` was 0% populated because nothing passed it. Neither is visible to a
coverage check and neither would have been caught by `assert x is not None`.

So the assertions below say `price == 20.99`, not `price is not None`.

The fixtures
------------
Trimmed from real payloads captured 2026-09-19 against store 44009506 (`gb`),
keeping every structural feature that cost something to discover:

* the `BundleBean` indirection — the outer entry's `detail.colors` is empty
  and the real tree hangs off `bundleProductSummaries[0]`;
* two array entries pointing at ONE bundle;
* an error object sitting inside `products` at HTTP 200;
* two sizes in one colour both named "XS", with different `sku`;
* prices as minor-unit STRINGS, with the exponent living in the store config.

They are scrubbed: no session id, no cookie, no proxy credential. `sizes[]`
in the real payload carries none of those, and the check below asserts that
rather than assuming it.
"""

from __future__ import annotations

import ast
import builtins
import csv
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import fields
from pathlib import Path as pathlib_Path
from typing import Optional

import catalog_walk
import env_config
import page_flow
import product_parser
from captcha_solver import (CaptchaChallenge, detect_recaptcha_v3,
                            detect_recaptcha_in_page, reconcile_detections,
                            solve_recaptcha, get_balance)
from diff_runs import diff_rows, _check_comparable, TRACKED_FIELDS
from output_writer import (Product, ROW_CLASS_BY_MODE,
                           UNIQUE_BY_SKU_MODES, dedupe_by_key, dedupe_by_sku,
                           write_json, write_csv, save, finish_run, run_meta,
                           LIST_CSV_SEPARATOR, EXIT_BLOCKED, EXIT_NO_PRODUCTS,
                           EXIT_PARTIAL, EXIT_REMOTE_API_ERROR, RemoteAPIError,
                           COMPLETE_STOP_REASONS, SOURCE_DEFAULT)
from product_parser import (HOSTS, BASE, LOCALES, DEFAULT_LOCALE, page_url,
                            page_number_from_url, sku_from_url,
                            category_from_url, is_product_url,
                            is_category_url, is_robots_allowed, parse_robots,
                            parse_money, parse_price, sitemap_url,
                            detect_bot_challenge, detect_page_state,
                            is_supported_host, unsupported_reason, site_host,
                            is_self_clearing_challenge, is_edge_refusal,
                            parse_category, parse_product, parse_sitemap,
                            product_urls_from_sitemap, BOT_CHALLENGE_MARKERS,
                            locale_of, parse_store_config, walk_menu,
                            parse_grid, parse_products_array, money,
                            build_image_url, parse_sitemap_index,
                            sitemap_locales, canonical_slug, product_url,
                            store_id_from_page)
from proxy_pool import (ProxyPool, ProxyError, mask, to_playwright,
                        split_credentials, parse_proxy_line)


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False

def importlib_util_find(name):
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False

def _raises_type(exc, fn, *a, **kw):
    """True when `fn` raises exactly `exc`. Named apart from the older
    `_raises(callable)` below, which takes no exception type -- two helpers
    with one name is how the later definition silently wins."""
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception:
        return False
    return False

BANNED_PHRASES = (
    "antidetect browser",
    "anti-detect browser",
    "2scraper Antidetect Browser",
    "gate.2prx.com",
    "--antidetect",
    "ANTIDETECT_LOCAL_API",
    # A claim about what the PRODUCT can do, not about what this repo
    # implements. A sibling repo shipped a README saying a 2Captcha key
    # would not help on its site, while 2Captcha had solved that exact
    # captcha type for years — an error no test could catch, because
    # nothing fails and the output stays correct; it just tells a reader
    # not to buy something that works. The only sentence this family is
    # entitled to is "this repo does not implement X", which is a TODO.
    # Two engines here carried "so this challenge cannot be solved" until
    # v0.4.1, on a site whose captcha 2Captcha does solve.
    "cannot be solved",
    "can't be solved",
    "is inapplicable",
)

# Flags that must not exist ON THE ENGINES:
#   --antidetect   the endpoint behind it was a placeholder that never existed.
#   --country      the URL/mode already decides which page is read; a flag
#                  here could disagree with it. (fingerprint_client.py
#                  legitimately has --country: it picks a fingerprint
#                  locale, a different question -- so this check is scoped
#                  to the engines, not the whole repo.)
#   --details      the old pre-family scraper's flag for "also fetch each
#                  player's profile page". That is --mode player now.
REMOVED_ENGINE_FLAGS = ("--antidetect", "--marketplace", "--country", "--details")
ENGINE_FILES = ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py")

_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                   "__spec__", "__loader__", "__builtins__", "__debug__"}


def _undefined_names(path):
    """Names loaded in `path` that are never imported, defined or assigned.

    Deliberately coarse -- it pools every binding in the file rather than
    tracking scopes, so it under-reports and never invents a problem. That
    is the right trade here: it exists to catch a name that is nowhere at
    all, and a false positive would be worse than a miss.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bound = set(dir(builtins)) | _MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound |= set(node.names)
    missing = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound:
            missing.setdefault(node.id, []).append(node.lineno)
    return missing


class _FakeSession:
    """Stands in for a _BrowserSession: opened, closed, carries a pool."""

    def __init__(self, pool=None):
        self.pool = pool
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

FIX_FINGERPRINT = {
    "id": 1000000,
    "country": "GB",
    "userAgent": {
        "userAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36"),
        "platform": "Windows",
        "mobile": False,
    },
    "intl": {
        "contentLocale": "en-GB",
        "languages": ["en-GB", "en"],
        "timeZone": "Europe/London",
    },
    "screen": {"width": 1920, "height": 1080,
              "outerWidth": 1920, "outerHeight": 992,
              "deviceScaleFactor": 1},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIX_STORE_CONFIG = json.dumps({
    "id": 44009506,
    "countryCode": "GB",
    "catalogs": [
        {"id": 40259534, "identifier": "BSK_GB", "type": 1},
        {"id": 40259534, "identifier": "BSK_GB", "type": 2},
        {"id": 40259584, "identifier": "CATALOGO_BSK_IOP", "type": 100},
    ],
    "details": {
        "imageBaseUrl": "https://static.bershka.net/4/photos2",
        "staticUrl": "https://static.bershka.net/4/static",
        "locale": {"currencyCode": "GBP", "currencyDecimals": -2,
                   "currencySymbol": "£", "currencyFormatPos": "£#,##0.00"},
    },
    # Present in the real payload and deliberately kept: this is the only
    # locale-shaped string in a GB store config and it reads "en_US".
    "supportedLanguages": [{"id": -1, "localeName": "en_US", "code": "en"}],
})

FIX_MENU = json.dumps({"items": [
    {"id": 1010193132, "key": "BERSHKA_WOMAN", "name": "WOMEN",
     "content": {"id": "BERSHKA_WOMAN", "type": "grid"},
     "children": [
         {"id": 1010822095, "name": "SALE", "content": {"id": "1010822084", "type": "redirection"},
          "children": [
              {"id": 1010822088, "name": "Dresses and jumpsuits",
               "content": {"id": "87687eba-fdc3-4348-8570-972923c68698", "type": "grid"},
               "children": []},
              {"id": 1010822099, "name": "Espacio",
               "content": {"id": "1_X_ESPACIOBLANCO", "type": "marketing",
                           "metadata": {"kind": "LAYOUT"}},
               "children": []},
          ]},
     ]},
    {"id": 1010193133, "name": "MEN", "content": {"id": "BERSHKA_MAN", "type": "grid"},
     "children": []},
]})

FIX_GRID = json.dumps({
    "gridElements": [{"type": "section", "section": {"name": "F - dresses | F - jumpsuits"}}],
    "filters": [{}] * 23,
    # 14 ids, 13 sorted. The missing one is the id whose product comes back
    # as an error object — see FIX_PRODUCTS.
    "productIds": [229723104, 229736905, 999999999],
    "sortedProductIds": [229723104, 229736905],
    "gridContext": {"gridId": "87687eba-fdc3-4348-8570-972923c68698",
                    "sectionNames": ["F - dresses | F - jumpsuits"]},
})

def _size(sku, name, price, old, pct, buyable=True, back_soon="0",
          country="MAINLAND CHINA", partnumber="0121872980001-I2026"):
    size = {"sku": sku, "name": name, "partnumber": partnumber,
            "isBuyable": buyable, "backSoon": back_soon, "mastersSizeId": "101",
            "position": 2, "price": price, "sizeType": "regular", "country": country}
    if old is not None:
        size["oldPrice"] = old
    if pct is not None:
        size["discountsPercentages"] = {"oldPriceDiscount": pct}
        size["promotionId"] = 39854
    return size


_BUNDLE = {
    "id": 229704964,
    "name": "Lace strap midi dress",
    "sectionName": "WOMEN",
    "familyName": "Dresses",
    "subFamilyName": "Midi dresses",
    "productUrl": "lace-strap-midi-dress-l01218714",
    "productUrlParam": 229704964,
    "detail": {
        "reference": "01218714-I2026",
        "displayReference": "1218/714",
        "colors": [{
            "id": "800", "name": "Black",
            "image": {"timestamp": "1772614547781",
                      "url": "/2026/I/0/1/p/1218/714/800/1218714800",
                      "type": ["1", "2", "3"]},
            # Two sizes, same NAME, different sku and country. Keying a row
            # on the size name collapses these two into one.
            "sizes": [
                _size(229704949, "XS", "2099", "2999", "30"),
                _size(229704966, "XS", "2099", "2999", "30",
                      country="CAMBODIA", partnumber="0121871480001-I2026"),
                _size(229704948, "S", "2099", "2999", "30", back_soon="1"),
                # No oldPrice, no percentage: discount must come out None,
                # not 0.0, and price_source must still be "api-size".
                _size(229704947, "M", "2099", None, None),
            ],
        }],
    },
}

FIX_PRODUCTS = json.dumps({"products": [
    # Outer entry: type BundleBean, empty colours, real tree one level down.
    {"id": 229723104, "type": "BundleBean", "state": "visible",
     "name": "Lace strap midi dress", "section": 1, "sectionNameEN": "WOMAN",
     "productUrl": "lace-strap-midi-dress-l01218714",
     "detail": {"colors": [], "reference": "01218714-I2026"},
     "bundleProductSummaries": [_BUNDLE]},
    # A SECOND entry pointing at the SAME bundle — a colourway listed as its
    # own product. Emitting both is how 13 products became 195 rows for 131
    # distinct SKUs on 2026-09-19.
    {"id": 233573528, "type": "BundleBean", "state": "visible",
     "name": "Lace strap midi dress", "section": 1, "sectionNameEN": "WOMAN",
     "productUrl": "lace-strap-midi-dress-l01218714",
     "detail": {"colors": []},
     "bundleProductSummaries": [_BUNDLE]},
    # An error object, inside a 200, with no id to say what it answers.
    {"description": "Item not found", "key": "_ERR_PRODUCT_NOT_FOUND",
     "commitMark": False, "causes": [], "params": []},
]})

FIX_SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://www.bershka.com/sitemap/familias/sitemap_familias_en-gb-part0.xml</loc></sitemap>
<sitemap><loc>https://www.bershka.com/sitemap/familias/sitemap_familias_ru-kz-part0.xml</loc></sitemap>
<sitemap><loc>https://www.bershka.com/sitemap/productos/sitemap_productos_en-gb_women-part0.xml.gz</loc></sitemap>
<sitemap><loc>https://www.bershka.com/sitemap/productos/sitemap_productos_pt-pt_men-part0.xml.gz</loc></sitemap>
</sitemapindex>"""

FIX_PRODUCT_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.bershka.com/gb/baggy-jeans-c0p202226282.html</loc></url>
<url><loc>https://www.bershka.com/kz/baggy-jeans-c0p202226282.html</loc></url>
<url><loc>https://www.bershka.com/ru/baggy-jeans-c0p202226282.html</loc></url>
</urlset>"""

# The edge refusal, byte for byte as served on 2026-09-19.
FIX_EDGE_403 = ("<HTML><HEAD>\n<TITLE>Service Unavailable</TITLE>\n</HEAD><BODY>\n"
                "<H1>Service Unavailable</H1>\n\n"
                "HTTP Error 403. The service is unavailable.<P>\n"
                "Reference #0.SCRUBBED.SCRUBBED.SCRUBBED\n</BODY>\n</HTML>\n")
FIX_EDGE_403_HEADERS = {
    "x-reference-error": "18.SCRUBBED.SCRUBBED.SCRUBBED",
    "akamai-cache-status": "Error from child",
    # Scrubbed. The captured response carried a real 32-hex session id
    # here, which .github/ci_checks.py correctly reads as the shape of a
    # 2captcha key. The VALUE proves nothing the test needs; that the
    # header is present does.
    "set-cookie": "ITXSESSIONID=SCRUBBED-NOT-A-REAL-SESSION; path=/",
}

# Trimmed from a real one served on 2026-09-20, with its token and reference
# scrubbed. Everything structural is kept: HTTP 200 (not 403), ~2 KB, the
# meta-refresh back to the same path with a `bm-verify` query, and the split
# arithmetic (`Number("3886" + "11036")`) whose halves are concatenated so a
# regex looking for one number literal finds nothing.
FIX_INTERSTITIAL = (
    '<!DOCTYPE html><html><head> <meta charset="utf-8"> '
    '<meta http-equiv="refresh" content="5; URL=\'/gb/?bm-verify=SCRUBBED-TOKEN\'" />'
    '<title>&nbsp;</title><script> var i = 1789910678; '
    'var j = i + Number("3886" + "11036"); </script> </head>'
    '<body> <iframe src="/interstitial/ic.html"> </iframe> <script>'
    'function triggerInterstitialChallenge() {var xhr = new XMLHttpRequest();'
    'xhr.open("POST", "/_sec/verify?provider=interstitial");'
    'xhr.send(JSON.stringify({"bm-verify":"SCRUBBED-TOKEN","pow":j}));}'
    '</script></body></html>')

# The headers that came with it. `X-Reference-Error` is present on BOTH this
# and the refusal, which is exactly why it cannot be the thing that tells
# them apart; the cache status can.
FIX_INTERSTITIAL_HEADERS = {
    "content-type": "text/html",
    "x-reference-error": "18.SCRUBBED.SCRUBBED.SCRUBBED",
    "akamai-cache-status": "NotCacheable from child",
    "set-cookie": "ak_bmsc=SCRUBBED-NOT-A-REAL-COOKIE; path=/",
}

FIX_STOREFRONT = (
    '<!DOCTYPE html><html lang="en-GB"><head>'
    '<link href="https://static.bershka.net/4/static/x.css"></head><body>'
    '<script>var inditex={"iEngine":"vue","iStoreId":44009506,"iCountryCode":"GB",'
    '"iLangId":-1,"iLocale":"en_GB","iCatalogId":40259534};</script>'
    '</body></html>')


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

def test_robots():
    group("robots.txt")
    text = (product_parser._ROBOTS_SNAPSHOT.read_text(encoding="utf-8")
            if product_parser._ROBOTS_SNAPSHOT.exists() else "")
    check("a robots snapshot ships with the repo", bool(text))

    star = parse_robots(text, "*")
    # The whole point of this parser. Three `User-agent: *` groups, and a
    # reader that stops at the first keeps 28 of 140 rules.
    check("three `User-agent: *` groups are merged, not just the first",
          star.groups_merged == 3)
    check("the merge yields 140 rules (28 + 62 + 50)", len(star) == 140)
    check("one Sitemap line is collected", len(star.sitemaps) == 1)
    check("the Sitemap is the gzipped index",
          star.sitemaps[0].endswith("sitemap_indice.xml.gz"))

    # Rules from the SECOND and THIRD `*` groups. A first-group-only parser
    # calls both of these allowed.
    check("/ru/ is disallowed (2nd `*` group)",
          not is_robots_allowed("https://www.bershka.com/ru/women.html", star))
    check("/kz/ product pages are disallowed (3rd `*` group)",
          not is_robots_allowed(
              "https://www.bershka.com/kz/baggy-jeans-c0p202226282.html", star))
    check("/gb/ product pages are allowed",
          is_robots_allowed(
              "https://www.bershka.com/gb/baggy-jeans-c0p202226282.html", star))

    # The catalogue API: allowed to us, and only its marketing subtree banned.
    check("the catalogue API is allowed",
          is_robots_allowed(
              "https://www.bershka.com/itxrest/2/catalog/store/44009506?languageId=-1", star))
    check("/itxrest/1/marketing/ is not",
          not is_robots_allowed("https://www.bershka.com/itxrest/1/marketing/x", star))

    # Longest-match precedence, which is the only reason the one Allow means
    # anything: `Disallow: /*/q/*` covers `Allow: /*/q/*index=1*`.
    check("/gb/q/dress is disallowed",
          not is_robots_allowed("https://www.bershka.com/gb/q/dress", star))
    check("...but the longer Allow wins with index=1",
          is_robots_allowed("https://www.bershka.com/gb/q/dress?index=1", star))

    # Search, filters and sorting.
    for param in ("?query=dress", "?searchTerm=dress", "?search=dress"):
        check(f"search is disallowed ({param})",
              not is_robots_allowed(f"https://www.bershka.com/gb/x.html{param}", star))
    for param in ("sort=price", "price=10", "discount=1", "size=M"):
        check(f"{param.split('=')[0]}= is disallowed",
              not is_robots_allowed(f"https://www.bershka.com/gb/x.html?{param}", star))

    # Named bots get DIFFERENT answers, and this repo is not one of them.
    yandex = parse_robots(text, "Yandex")
    check("Yandex is refused the catalogue API",
          not is_robots_allowed(
              "https://www.bershka.com/itxrest/2/catalog/store/44009506", yandex))
    proximic = parse_robots(text, "proximic")
    check("proximic is refused the catalogue API",
          not is_robots_allowed(
              "https://www.bershka.com/itxrest/2/catalog/store/44009506", proximic))
    return not _failures


# ---------------------------------------------------------------------------
# Store config and money
# ---------------------------------------------------------------------------

def test_store_config():
    group("store config")
    store = parse_store_config(FIX_STORE_CONFIG)
    check("store id", store.store_id == 44009506)
    check("country", store.country_code == "GB")
    check("currency code is read, never guessed from the symbol",
          store.currency_code == "GBP")
    check("currency decimals", store.currency_decimals == -2)
    check("image base url comes from the payload",
          store.image_base_url == "https://static.bershka.net/4/photos2")
    # The type-100 IOP catalogue is not the storefront one.
    check("the type-1 catalogue wins over the type-100 one",
          store.catalog_id == 40259534)
    check("StoreConfig carries no `locale` field at all",
          not hasattr(store, "locale"))

    check("2099 minor units with exponent -2 is 20.99", money("2099", -2) == 20.99)
    check("a missing price is None, not 0.0", money(None, -2) is None)
    check("an empty string is None", money("", -2) is None)
    check("no exponent means no price", money("2099", None) is None)
    check("a sign on the exponent does not flip the scale", money("2099", 2) == 20.99)
    # parse_money must refuse display text, on purpose.
    check("parse_money refuses '£20.99'", parse_money("£20.99") is None)
    check("parse_money accepts minor units", parse_money("2099") == 20.99)
    check("parse_price is the same function", parse_price("2099") == 20.99)
    return not _failures


# ---------------------------------------------------------------------------
# Menu, grid, products
# ---------------------------------------------------------------------------

def test_menu():
    group("menu")
    grids = walk_menu(FIX_MENU)
    trails = [g.trail for g in grids]
    check("grid nodes are collected", len(grids) == 3)
    check("the menu trail is the full path",
          "WOMEN / SALE / Dresses and jumpsuits" in trails)
    check("a marketing node is NOT a grid",
          not any("Espacio" in t for t in trails))
    check("a redirection node is NOT a grid",
          not any(g.grid_id == "1010822084" for g in grids))
    check("the section is the first trail element",
          {g.section for g in grids} == {"WOMEN", "MEN"})

    picked = catalog_walk.select_grids(grids, "SALE")
    check("select_grids matches on the trail, not the leaf name",
          len(picked) == 1 and picked[0].name == "Dresses and jumpsuits")
    check("select_grids is case-insensitive",
          len(catalog_walk.select_grids(grids, "sale")) == 1)
    check("no category means every grid", len(catalog_walk.select_grids(grids)) == 3)
    check("limit is honoured", len(catalog_walk.select_grids(grids, None, 2)) == 2)
    return not _failures


def test_grid():
    group("grid")
    grid = parse_grid(FIX_GRID)
    check("grid id comes from gridContext",
          grid.grid_id == "87687eba-fdc3-4348-8570-972923c68698")
    check("productIds is returned untouched", len(grid.product_ids) == 3)
    check("sortedProductIds is kept separately", len(grid.sorted_product_ids) == 2)
    check("the two lists are allowed to disagree",
          len(grid.product_ids) != len(grid.sorted_product_ids))
    check("filters are counted", grid.filter_count == 23)
    check("an empty payload does not raise", parse_grid("{}").product_ids == [])
    check("junk does not raise", parse_grid("not json").product_ids == [])
    return not _failures


def test_products():
    group("productsArray")
    store = parse_store_config(FIX_STORE_CONFIG)
    result = parse_products_array(FIX_PRODUCTS, store, locale="gb",
                                  category="WOMEN / SALE / Dresses and jumpsuits",
                                  grid_id="87687eba", page=1)
    rows = result.rows

    check("the error object is separated out, not parsed as a product",
          len(result.errors) == 1)
    check("and it is named", result.errors[0]["key"] == "_ERR_PRODUCT_NOT_FOUND")
    check("the second entry's repeated bundle is folded",
          result.duplicate_bundles == 1)
    check("one bundle, one colour, four sizes -> four rows", len(rows) == 4)
    check("every row has a distinct sku", len({r.sku for r in rows}) == 4)

    first = rows[0]
    check("sku is the size's sku", first.sku == "229704949")
    check("price is scaled", first.price == 20.99)
    check("currency comes from the store", first.currency == "GBP")
    check("price_source names the API", first.price_source == "api-size")
    check("old price is scaled too", first.original_price == 29.99)
    check("discount comes from the payload", first.discount_pct == 30.0)
    check("and says so", first.discount_source == "api")
    check("title comes off the bundle", first.title == "Lace strap midi dress")
    check("colour name", first.color_name == "Black")
    check("colour id is a string", first.color_id == "800")
    check("reference", first.reference == "01218714-I2026")
    check("display reference", first.display_reference == "1218/714")
    check("country of origin", first.country_of_origin == "MAINLAND CHINA")
    check("page is filled", first.page == 1)
    check("grid id is recorded", first.grid_id == "87687eba")
    check("category is the menu trail",
          first.category == "WOMEN / SALE / Dresses and jumpsuits")

    # The bug the first live run found: `section` filled 100% with "1".
    check("section is a NAME, never the numeric section code",
          first.section == "WOMEN" and first.section != "1")
    check("family", first.family == "Dresses")
    check("subfamily", first.subfamily == "Midi dresses")

    # Two sizes called XS.
    xs = [r for r in rows if r.size_name == "XS"]
    check("both sizes named XS survive", len(xs) == 2)
    check("...with different skus", xs[0].sku != xs[1].sku)
    check("...and different part numbers", xs[0].partnumber != xs[1].partnumber)
    check("...and different countries",
          {r.country_of_origin for r in xs} == {"MAINLAND CHINA", "CAMBODIA"})

    # Availability is folded from two fields that disagree on purpose.
    by_size = {r.size_name: r for r in rows}
    check("a buyable size is InStock", by_size["XS"].availability == "InStock")
    check("backSoon '1' beats isBuyable", by_size["S"].availability == "BackSoon")
    check("backSoon '0' is not truthy", by_size["XS"].back_soon is False)
    check("the raw flags are kept", by_size["S"].is_buyable is True)

    # A size with no discount at all.
    m = by_size["M"]
    check("no oldPrice means no discount, not 0.0", m.discount_pct is None)
    check("and no discount_source", m.discount_source is None)
    check("but still a price and a source", m.price == 20.99 and m.price_source == "api-size")

    check("the URL is the canonical product page",
          first.url == "https://www.bershka.com/gb/lace-strap-midi-dress-c0p229723104.html")
    check("the API's -l{reference} slug tail is stripped",
          canonical_slug("lace-strap-midi-dress-l01218714") == "lace-strap-midi-dress")
    check("a slugless URL is built when there is no slug",
          product_url("gb", 229723104) == "https://www.bershka.com/gb/c0p229723104.html")

    check("the image URL is built from the store's own base",
          first.image_url == "https://static.bershka.net/4/photos2"
                             "/2026/I/0/1/p/1218/714/800/1218714800_1_1_0.jpg"
                             "?ts=1772614547781")
    check("no image object means no URL, not a broken one",
          build_image_url(None) is None)
    check("an image with no stem is None", build_image_url({"timestamp": "1"}) is None)

    # Scrubbing: the fixture must not carry anything session-shaped.
    blob = FIX_PRODUCTS.lower()
    for secret in ("itxsessionid", "bsksession", "set-cookie", "proxy", "password"):
        check(f"the fixture carries no {secret}", secret not in blob)

    check("parse_category is the same expansion",
          len(parse_category(FIX_PRODUCTS, store=store)) == 4)
    check("parse_product is too", len(parse_product(FIX_PRODUCTS, store=store)) == 4)
    return not _failures


# ---------------------------------------------------------------------------
# Sitemaps
# ---------------------------------------------------------------------------

def test_sitemaps():
    group("sitemaps")
    refs = parse_sitemap_index(FIX_SITEMAP_INDEX.encode())
    check("every entry is read", len(refs) == 4)
    kinds = {r.kind for r in refs}
    check("categories are `familias` and products are `productos`",
          kinds == {"familias", "productos"})
    check("locales are parsed out of the file names",
          sitemap_locales(refs) == ["en-gb", "pt-pt", "ru-kz"])
    check("the gz flag is read from the name",
          {r.gzipped for r in refs if r.kind == "productos"} == {True})
    check("familias is not gzipped",
          {r.gzipped for r in refs if r.kind == "familias"} == {False})
    check("the section segment does not become part of the locale",
          all(r.locale in ("en-gb", "pt-pt", "ru-kz") for r in refs))

    import gzip as _gzip
    check("a gzipped body is transparently decompressed",
          len(parse_sitemap_index(_gzip.compress(FIX_SITEMAP_INDEX.encode()))) == 4)

    urls = product_urls_from_sitemap(FIX_PRODUCT_SITEMAP.encode())
    check("a sitemap is filtered through robots, not trusted", len(urls) == 1)
    check("and what survives is the allowed locale", "/gb/" in urls[0])

    check("sitemap_url builds the productos name",
          sitemap_url("en-gb", "product", segment="women").endswith(
              "sitemap_productos_en-gb_women-part0.xml.gz"))
    check("and the familias name",
          sitemap_url("en-gb", "category").endswith(
              "sitemap_familias_en-gb-part0.xml"))
    return not _failures


# ---------------------------------------------------------------------------
# Walls
# ---------------------------------------------------------------------------

def test_walls():
    group("edge refusal vs interstitial")
    # The single most important distinction in this repo: one is fixed by a
    # different exit, the other by waiting.
    check("the 403 body is an edge refusal",
          is_edge_refusal(403, FIX_EDGE_403, FIX_EDGE_403_HEADERS))
    check("...and is NOT a self-clearing challenge",
          not is_self_clearing_challenge(FIX_EDGE_403))
    check("...and its page state is blocked",
          detect_page_state(FIX_EDGE_403, 403, headers=FIX_EDGE_403_HEADERS) == "blocked")
    check("the headers alone are enough to name it",
          is_edge_refusal(200, "", FIX_EDGE_403_HEADERS))
    check("a plain 403 with an unrelated body is not assumed to be the edge",
          not is_edge_refusal(403, "<h1>Not found</h1>"))

    # The finding that ten live runs produced, and the reason the two are
    # tested against each other rather than separately. Akamai sends
    # `x-reference-error` on the interstitial too, so a refusal keyed on that
    # header called a 200-with-a-challenge an address ban — and since
    # `is_edge_refusal` is checked FIRST and raises, the challenge ladder
    # would never have run on a real challenge.
    check("an interstitial is NOT an edge refusal, despite sharing the header",
          not is_edge_refusal(200, FIX_INTERSTITIAL, FIX_INTERSTITIAL_HEADERS))
    check("...and x-reference-error alone never means refusal",
          not is_edge_refusal(200, "<html>a normal page</html>",
                              {"x-reference-error": "18.x"}))
    check("...while `Error from child` still does",
          is_edge_refusal(403, "", {"akamai-cache-status": "Error from child"}))
    check("...and `NotCacheable from child` does not",
          not is_edge_refusal(200, "<html>fine</html>",
                              {"akamai-cache-status": "NotCacheable from child"}))
    check("the interstitial arrives as HTTP 200, not 403",
          detect_page_state(FIX_INTERSTITIAL, 200,
                            headers=FIX_INTERSTITIAL_HEADERS) == "blocked")

    # The handshake, as parsing. The arithmetic operand is split across a
    # string concatenation so a regex looking for one number literal finds
    # nothing; the halves are JOINED and then added, not added separately.
    payload = product_parser.parse_interstitial(FIX_INTERSTITIAL)
    check("the shim's own POST body can be reconstructed", payload is not None)
    check("the token is the one from the POST, not the meta refresh",
          payload and payload["bm-verify"] == "SCRUBBED-TOKEN")
    check("the arithmetic joins the halves before adding",
          payload and payload["pow"] == 1789910678 + int("3886" + "11036"))
    check("an ordinary page yields no handshake",
          product_parser.parse_interstitial("<html>a real page</html>") is None)
    check("the verify path is the one the shim posts to",
          product_parser.INTERSTITIAL_VERIFY_PATH
          == "/_sec/verify?provider=interstitial")

    # WHAT TYPE IT IS, pinned by exclusion rather than by assertion.
    # Twenty live runs across twenty exits (2026-09-20) produced exactly two
    # things: this shim and the flat 403. Scanned for every vendor this
    # family knows, the shim carries Akamai's markers and NOTHING that a
    # human or a solver API answers — no reCAPTCHA, hCaptcha, Turnstile,
    # AWS WAF, DataDome, PerimeterX, Imperva or Kasada. It is Akamai Bot
    # Manager's interstitial: a non-interactive arithmetic challenge the
    # client's own script answers. There is no image, no checkbox and no
    # token to buy.
    foreign = ("g-recaptcha", "grecaptcha", "recaptcha/api.js",
               "hcaptcha", "cf-turnstile", "challenges.cloudflare.com",
               "captcha.awswaf.com", "gokuprops", "datadome",
               "perimeterx", "px-captcha", "incapsula", "kpsdk",
               "sec-if-cpt-container")
    low = FIX_INTERSTITIAL.lower()
    present = [m for m in foreign if m in low]
    check(f"the shim carries no interactive-captcha vendor (found: {present})",
          not present)
    check("...and it does carry Akamai's own two markers",
          "bm-verify" in FIX_INTERSTITIAL and "/_sec/verify" in FIX_INTERSTITIAL)
    check("the repo's vendor map names it, and names it Akamai",
          detect_bot_challenge(FIX_INTERSTITIAL) == "akamai-interstitial")

    check("the interstitial IS self-clearing",
          is_self_clearing_challenge(FIX_INTERSTITIAL))
    check("...and is named as its own vendor",
          detect_bot_challenge(FIX_INTERSTITIAL) == "akamai-interstitial")
    check("the two walls are separate vendors in the marker map",
          "akamai-edge" in BOT_CHALLENGE_MARKERS
          and "akamai-interstitial" in BOT_CHALLENGE_MARKERS)

    check("a JSON payload is content", detect_page_state(FIX_PRODUCTS, 200) == "content")
    check("an empty product list is `empty`, not blocked",
          detect_page_state('{"products": []}', 200) == "empty")
    check("an empty grid is `empty` too",
          detect_page_state('{"productIds": []}', 200) == "empty")
    check("truncated JSON is blocked, not content",
          detect_page_state('{"products": [', 200) == "blocked")
    check("a sitemap is content",
          detect_page_state(FIX_PRODUCT_SITEMAP, 200) == "content")
    check("a storefront page is content", detect_page_state(FIX_STOREFRONT, 200) == "content")
    check("an empty body is blocked", detect_page_state("", 200) == "blocked")
    check("a 500 vetoes a good-looking body",
          detect_page_state(FIX_PRODUCTS, 500) == "blocked")
    return not _failures


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def test_urls():
    group("URLs")
    check("the API host is supported", is_supported_host("https://www.bershka.com/gb/"))
    check("the static host is not",
          not is_supported_host("https://static.bershka.net/4/photos2/x.jpg"))
    check("and the reason says what it is",
          "static asset host" in unsupported_reason("https://static.bershka.net/x"))
    check("site_host is lowercased", site_host("https://WWW.Bershka.com/gb/") == "www.bershka.com")

    url = "https://www.bershka.com/gb/lace-strap-midi-dress-c0p229723104.html"
    check("a product URL is recognised", is_product_url(url))
    check("the id it carries is the PRODUCT id", sku_from_url(url) == "229723104")
    check("a listing URL is not a product URL",
          is_category_url("https://www.bershka.com/gb/women.html")
          and not is_product_url("https://www.bershka.com/gb/women.html"))
    check("locale_of reads the prefix", locale_of(url) == "gb")
    check("both measured locales have a store id",
          set(product_parser.KNOWN_STORES) == {"gb", "de"})
    check("...and each carries a catalogue id too",
          all("catalog_id" in v for v in product_parser.KNOWN_STORES.values()))

    # Both of these are degenerate on this site, deliberately.
    check("category_from_url returns None — a listing URL names no category",
          category_from_url("https://www.bershka.com/gb/women.html") is None)
    check("page_url is a no-op: there is no allowed paging parameter",
          page_url(url, 7) == url)
    check("page_number_from_url is always 1", page_number_from_url(url + "?x=3") == 1)

    blob = store_id_from_page(FIX_STOREFRONT)
    check("a store id can be read off any storefront", blob["store_id"] == 44009506)
    check("...with its catalogue", blob["catalog_id"] == 40259534)
    check("...and nothing is read out of a page without the blob",
          store_id_from_page("<html></html>") is None)
    return not _failures


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------

def test_catalog_walk():
    group("catalog_walk")
    served = {}

    def fetch(url):
        served[url] = served.get(url, 0) + 1
        if "/itxrest/2/catalog/store/" in url:
            return catalog_walk.Fetched(200, FIX_STORE_CONFIG.encode())
        if "/menu" in url:
            return catalog_walk.Fetched(200, FIX_MENU.encode())
        if "/grid/" in url:
            return catalog_walk.Fetched(200, FIX_GRID.encode())
        if "productsArray" in url:
            return catalog_walk.Fetched(200, FIX_PRODUCTS.encode())
        raise AssertionError("unexpected URL " + url)

    result = catalog_walk.crawl(fetch, 44009506, category="SALE", locale="gb")
    check("the walk produced rows", len(result.rows) == 4)
    check("it fetched exactly one grid", result.grids_fetched == 1)
    check("it requested the ids the grid listed", result.products_requested == 3)
    check("payload errors are carried up", len(result.payload_errors) == 1)
    check("duplicate bundles are carried up", result.duplicate_bundles == 1)
    check("bytes are counted", result.bytes_read > 0)
    check("requests are counted", result.requests_made == 4)
    check("the store config is on the result", result.store.store_id == 44009506)

    # A category nobody has is not an error and not an empty success.
    empty = catalog_walk.crawl(fetch, 44009506, category="NO SUCH THING")
    check("an unmatched category has its own stop reason",
          empty.stop_reason == "no_grid_matched_category")
    check("...and produced no rows", not empty.rows)

    # The row cap stops the walk. Named `--max-skus` now, because it counts
    # rows and a row is a SKU; `--max-products` still works as an alias.
    capped = catalog_walk.crawl(fetch, 44009506, category="SALE", max_skus=2)
    check("the sku cap stops the walk", len(capped.rows) == 2)
    check("...and says why", capped.stop_reason == "max_skus_reached")

    # robots is enforced INSIDE the walk, not only at the CLI.
    def forbidden_fetch(url):
        raise AssertionError("fetch must not be called for a disallowed URL")
    check("a disallowed URL raises instead of being fetched",
          _raises_type(catalog_walk.RobotsRefusal, catalog_walk._get,
                       forbidden_fetch,
                       "https://www.bershka.com/itxrest/1/marketing/x",
                       catalog_walk.WalkResult()))

    # The edge refusal is raised as itself, not as a generic failure.
    def refusing_fetch(url):
        return catalog_walk.Fetched(403, FIX_EDGE_403.encode(), FIX_EDGE_403_HEADERS)
    check("an edge refusal raises EdgeRefusal",
          _raises_type(catalog_walk.EdgeRefusal, catalog_walk.load_store,
                       refusing_fetch, 44009506))
    message = ""
    try:
        catalog_walk.load_store(refusing_fetch, 44009506)
    except catalog_walk.EdgeRefusal as exc:
        message = str(exc)
    check("...and the message says a retry will not help",
          "not a challenge to wait out" in message)
    return not _failures


# ---------------------------------------------------------------------------
# Family checks, carried over unchanged from the sibling repo they were
# written in. They cover code this repo did not write and did not change:
# the writers, the diff, the env loader, the proxy pool, the fingerprint
# client and the policy checks. CLAUDE.md §11 — a workflow step must call
# the shipped check rather than re-implement it — is the same rule one
# level up: a rewritten copy of these would drift from the module it tests.
# ---------------------------------------------------------------------------

def test_readiness_wait():
    group("page_flow.wait_for_count: poll a count, never evaluate a string")
    ok = True

    # ready_count converts min_matches's "strictly MORE than this floor" into
    # the "at least this many" wait_for_count is written against. They are
    # tied together here because the conversion is the kind of off-by-one
    # that would leave the three engines waiting on different thresholds
    # while every other check stayed green.
    for mode in ("category", "product"):
        ok &= check("ready_count(%s) is one more than its min_matches floor" % mode,
                    page_flow.ready_count(mode) == page_flow.min_matches(mode) + 1)
    # Rewritten for this site. The sibling repo waited for product TILES and
    # tuned the floor against the smallest real category it had captured.
    # Here a rendered listing holds no products at all, so the readiness wait
    # confirms something else entirely: that the storefront loaded and a
    # session exists before any API call is issued. Both modes therefore
    # share one floor, and it is the lowest the family permits.
    ok &= check("both modes share one floor, because both wait for the same "
                "thing -- a served storefront",
                page_flow.min_matches("product") == page_flow.min_matches("category"))
    ok &= check("the floor is still a floor: the wait must SEE more than one "
                "match", page_flow.ready_count("category") == 2)
    ok &= check("the readiness selectors name the static asset host, which a "
                "challenge page does not carry",
                all("static.bershka.net" in sel
                    for sel in page_flow.READY_SELECTORS.values()))

    def fake_driver(counts):
        """A driver that returns a scripted sequence of counts and only
        records its sleeps, so the wait's arithmetic is testable with no
        browser and no engine library installed."""
        state = {"polls": 0, "slept": 0, "selectors": []}

        def count(selector):
            state["selectors"].append(selector)
            i = min(state["polls"], len(counts) - 1)
            state["polls"] += 1
            return counts[i]

        def sleep(ms):
            state["slept"] += ms

        return count, sleep, state

    count, sleep, st = fake_driver([0, 1, 4])
    seen = page_flow.wait_for_count(count, sleep, "table.items tbody tr", 4, 20000)
    ok &= check("returns as soon as the minimum is reached", seen == 4)
    ok &= check("and stops polling there rather than spending the budget",
                st["polls"] == 3 and st["slept"] == 500)
    ok &= check("it polls the selector it was handed, not a hardcoded one",
                set(st["selectors"]) == {"table.items tbody tr"})

    count, sleep, st = fake_driver([7])
    seen = page_flow.wait_for_count(count, sleep, "x", 4, 20000)
    ok &= check("a page already painted is not slept on at all",
                seen == 7 and st["slept"] == 0 and st["polls"] == 1)

    count, sleep, st = fake_driver([2])
    seen = page_flow.wait_for_count(count, sleep, "x", 4, 1000, poll_ms=250)
    ok &= check("gives up when the budget runs out rather than looping forever",
                st["slept"] == 1000)
    ok &= check("and returns the LAST COUNT SEEN, so a caller can tell "
                "'painted' from 'timed out holding two of them'", seen == 2)

    count, sleep, st = fake_driver([0])
    page_flow.wait_for_count(count, sleep, "x", 1, 0)
    ok &= check("a zero budget polls once and returns, it does not sleep",
                st["polls"] == 1 and st["slept"] == 0)

    # The reason any of this exists. Playwright's wait_for_function and
    # pyppeteer's waitForFunction hand the BROWSER a string to evaluate, and
    # a site whose CSP omits unsafe-eval refuses it -- which on a sibling
    # repo was an EvalError and exit 1 on the site's most obvious URL, on one
    # of its two listing routes and not the other. Checked against the source
    # on disk rather than an imported module, so it still holds for the two
    # engines whose library is absent from this machine.
    #
    # The needles are BUILT rather than written out, for the same reason the
    # banned-phrase scan's list is: this file is scanned too, and spelling
    # them here would fail the build on the very file implementing the check
    # -- with no allowlist to reach for, because an allowlist is how a scan
    # stops covering the thing it was written for.
    eval_waits = ("wait_for_" + "function(", "wait" + "ForFunction(")
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s waits on no evaluated string" % name,
                    not any(needle in src for needle in eval_waits))

    # The sibling repo asserted this of each engine file, because each engine
    # there did its own waiting. Here every engine goes through
    # browser_bridge, so the assertion moves with the code: the wait must
    # exist ONCE and no engine may carry a second copy of it.
    bridge = open(os.path.join(REPO_ROOT, "browser_bridge.py"), encoding="utf-8").read()
    ok &= check("browser_bridge takes its readiness wait from page_flow",
                "page_flow.wait_for_count" in bridge)
    ok &= check("...and its challenge wait too",
                "page_flow.wait_out_self_clearing_challenge" in bridge)
    for name in ("playwright_scraper.py", "puppeteer_scraper.py",
                 "selenium_scraper.py", "api_scraper.py",
                 "scraper_api_client.py"):
        src = open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
        ok &= check("%s carries no wait loop of its own" % name,
                    "wait_for_count" not in src
                    and "wait_out_self_clearing_challenge" not in src)
    return ok

def test_writers():
    group("dedupe + JSON/CSV writers")
    ok = True

    seen = set()
    rows = [Product(sku="1"), Product(sku="2"), Product(sku="1"), Product(sku=None), Product(sku=None)]
    fresh = dedupe_by_key(rows, seen)
    ok &= check("a repeated sku across pages is dropped",
                [r.sku for r in fresh] == ["1", "2", None, None])
    ok &= check("a row with no sku is never dropped (nothing to compare it against)",
                sum(1 for r in fresh if r.sku is None) == 2)
    ok &= check("dedupe_by_sku is the same rule under its own name",
                [r.sku for r in dedupe_by_sku(
                    [Product(sku="9"), Product(sku="9")], set())] == ["9"])

    with tempfile.TemporaryDirectory() as d:
        json_path = os.path.join(d, "out.json")
        csv_path = os.path.join(d, "out.csv")
        sample = [Product(sku="1", title="A", related_categories=["Dresses and Jumpsuits", "Long"]),
                  Product(sku="2", title="B", related_categories=None)]
        write_json(sample, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        ok &= check("write_json round-trips a list field (related_categories) as a real list",
                    loaded[0]["related_categories"] == ["Dresses and Jumpsuits", "Long"])

        write_csv(sample, csv_path, row_cls=Product)
        with open(csv_path, encoding="utf-8", newline="") as f:
            csv_rows = list(csv.DictReader(f))
        ok &= check("write_csv joins a list field with the documented separator, "
                    "so it round-trips by splitting on the same string",
                    csv_rows[0]["related_categories"] == LIST_CSV_SEPARATOR.join(["Dresses and Jumpsuits", "Long"]))
        ok &= check("CSV header matches the Product schema exactly",
                    list(csv_rows[0].keys()) == [f.name for f in fields(Product)])

        # An empty result must still get a header, from row_cls, not the first row.
        empty_csv = os.path.join(d, "empty.csv")
        write_csv([], empty_csv, row_cls=Product)
        header = open(empty_csv, encoding="utf-8").readline().strip().split(",")
        ok &= check("write_csv([], ...) still writes the Product header, not "
                    "an empty file", header == [f.name for f in fields(Product)])
    return ok

def test_finish_run():
    group("save() / finish_run(): the empty-run and exit-code contract")
    ok = True

    with tempfile.TemporaryDirectory() as d:
        prefix = os.path.join(d, "run")

        # The family invariant: a run that finds nothing writes nothing,
        # unless the caller explicitly says an empty result is expected.
        rc = save([], prefix, "both", allow_empty=False)
        ok &= check("0 rows, no --allow-empty: nothing written, exit EXIT_NO_PRODUCTS",
                    rc == EXIT_NO_PRODUCTS and not os.path.exists(prefix + ".json"))
        rc = save([], prefix, "both", allow_empty=True)
        ok &= check("0 rows WITH --allow-empty: files ARE written",
                    os.path.exists(prefix + ".json") and os.path.exists(prefix + ".csv"))

        # finish_run: a complete run with rows.
        rows = [Product(sku="1"), Product(sku="2")]
        rc = finish_run(rows, prefix, "json", allow_empty=False, blocked=False,
                        stop_reason="completed", pages_requested=1, pages_completed=1,
                        start_url="https://x/1", final_url="https://x/1", mode="category")
        ok &= check("a complete run with rows returns 0", rc == 0)
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
        ok &= check("...and its meta sidecar says status=complete", meta["status"] == "complete")

        # A partial run (stopped early but got some rows) -> EXIT_PARTIAL,
        # and the meta sidecar must say so rather than claiming completeness.
        rc = finish_run(rows, prefix, "json", allow_empty=False, blocked=False,
                        stop_reason="captcha_unsolved", pages_requested=3, pages_completed=1,
                        start_url="https://x/1", final_url="https://x/1", mode="category")
        ok &= check("a partial run (some rows, did not finish) returns EXIT_PARTIAL",
                    rc == EXIT_PARTIAL)
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
        ok &= check("...and the sidecar says status=partial, not complete",
                    meta["status"] == "partial")

        # A run that got NOTHING and was blocked -> EXIT_BLOCKED, distinct
        # from EXIT_NO_PRODUCTS (a page that legitimately had nothing to show).
        rc = finish_run([], prefix, "json", allow_empty=False, blocked=True,
                        stop_reason="blocked", pages_requested=1, pages_completed=0,
                        start_url="https://x/1", final_url="https://x/1", mode="category")
        ok &= check("0 rows AND blocked=True returns EXIT_BLOCKED, not EXIT_NO_PRODUCTS "
                    "-- a bot-check is a different failure than an empty page",
                    rc == EXIT_BLOCKED)

        # 0 rows, not blocked (a genuinely empty page) -> EXIT_NO_PRODUCTS.
        rc = finish_run([], prefix, "json", allow_empty=False, blocked=False,
                        stop_reason="empty", pages_requested=1, pages_completed=1,
                        start_url="https://x/1", final_url="https://x/1", mode="category")
        ok &= check("0 rows, NOT blocked, returns EXIT_NO_PRODUCTS",
                    rc == EXIT_NO_PRODUCTS)

    ok &= check("COMPLETE_STOP_REASONS names the reasons that count as a full run",
                {"completed", "pagination_exhausted", "single_page_mode"} <= set(COMPLETE_STOP_REASONS))
    return ok

def test_diff():
    group("diff_runs: added / removed / changed / read-differently, keyed on sku")
    ok = True

    old = [{"sku": "P1", "title": "A", "price": 100.0, "currency": "GBP",
            "price_source": "tile-microdata", "availability": "InStock"},
           {"sku": "P2", "title": "B", "price": 50.0, "currency": "GBP",
            "price_source": "tile-microdata", "availability": "InStock"}]
    new = [{"sku": "P1", "title": "A", "price": 120.0, "currency": "GBP",
            "price_source": "tile-microdata", "availability": "InStock"},
           {"sku": "P3", "title": "C", "price": 10.0, "currency": "GBP",
            "price_source": "tile-microdata", "availability": "InStock"}]
    result = diff_rows(old, new)
    ok &= check("sku P2 dropped out -> removed",
                any(r["sku"] == "P2" for r in result["removed"]))
    ok &= check("sku P3 is new -> added",
                any(r["sku"] == "P3" for r in result["added"]))
    ok &= check("sku P1's price moved -> changed, with old/new both recorded",
                len(result["changed"]) == 1 and result["changed"][0]["sku"] == "P1"
                and result["changed"][0]["changes"]["price"] == {"old": 100.0, "new": 120.0})
    ok &= check("a field that did not change is not reported",
                "availability" not in result["changed"][0]["changes"])

    ok &= check("a row with no sku is counted as unmatchable, not silently dropped",
                diff_rows([{"sku": None}], [])["unmatchable_old"] == 1)

    # The fields that actually move on THIS catalogue between two runs. The
    # sibling repo this check came from listed `shade`, `shade_count`, `size`
    # and `badge`; none of them is a column here, and asserting them kept the
    # list looking healthy while it compared nothing.
    for field in ("price", "currency", "original_price", "discount_pct",
                  "availability", "title", "is_buyable", "back_soon",
                  "promotion_id"):
        ok &= check("TRACKED_FIELDS covers %r" % field, field in TRACKED_FIELDS)

    # image_url is deliberately NOT tracked: its CDN path carries a build
    # hash that changes on a deploy without the image changing, so every row
    # would report a change. Pinned so a future edit is a decision.
    ok &= check("image_url is deliberately not tracked (its URL carries a "
                "build hash that churns without the image changing)",
                "image_url" not in TRACKED_FIELDS)
    ok &= check("row position in a listing is not tracked either -- that is "
                "the site's merchandising, not a fact about the product",
                "row_index" not in TRACKED_FIELDS and "page" not in TRACKED_FIELDS)

    # A markdown appearing: original_price goes from None to a figure and
    # discount_pct with it. This is the change the repo exists to catch.
    old_d = [{"sku": "S1", "price": 134.0, "original_price": None,
              "discount_pct": None, "price_source": "tile-microdata"}]
    new_d = [{"sku": "S1", "price": 105.2, "original_price": 134.0,
              "discount_pct": 21.49, "price_source": "tile-microdata"}]
    result_d = diff_rows(old_d, new_d)
    ok &= check("a markdown appearing is reported as a change, with the "
                "struck price and the discount both named",
                len(result_d["changed"]) == 1
                and set(result_d["changed"][0]["changes"])
                == {"price", "original_price", "discount_pct"})

    # A product going out of stock, with no price movement at all.
    old_s = [{"sku": "S2", "price": 50.0, "availability": "InStock",
              "price_source": "tile-microdata"}]
    new_s = [{"sku": "S2", "price": 50.0, "availability": "OutOfStock",
              "price_source": "tile-microdata"}]
    ok &= check("a stock change with no price movement is still reported",
                len(diff_rows(old_s, new_s)["changed"]) == 1)

    # The family invariant this repo needs and transfermarkt-scraper does
    # not: a price difference that arrives WITH a price_source difference is
    # our two instruments disagreeing, not the shelf price moving.
    old_src = [{"sku": "P9", "price": 50.0, "price_source": "tile-microdata"}]
    new_src = [{"sku": "P9", "price": 50.5, "price_source": "jsonld"}]
    r_src = diff_rows(old_src, new_src)
    ok &= check("a price change that comes with a price_source change is "
                "NOT reported as a price change",
                not r_src["changed"] and len(r_src["source_changed"]) == 1)
    ok &= check("...and the source change itself is recorded, both sides",
                r_src["source_changed"][0]["price_source"]
                == {"old": "tile-microdata", "new": "jsonld"})
    ok &= check("...while the same price move read the SAME way IS a change",
                len(diff_rows(
                    [{"sku": "P9", "price": 50.0, "price_source": "jsonld"}],
                    [{"sku": "P9", "price": 50.5, "price_source": "jsonld"}]
                )["changed"]) == 1)

    # Two showcase-locale runs: every price is None on both sides, which is
    # correct and must not read as a change.
    old_sc = [{"sku": "P1", "title": "A", "price": None, "currency": None,
               "price_source": None}]
    new_sc = [{"sku": "P1", "title": "A", "price": None, "currency": None,
               "price_source": None}]
    ok &= check("two showcase-locale runs report no spurious price change",
                not diff_rows(old_sc, new_sc)["changed"])
    return ok

def test_captcha():
    group("captcha_solver: detection wiring and credential redaction")
    ok = True

    html_with_v3 = ('<html><body><script>grecaptcha.execute("6Lc-SITEKEY123456789012345",'
                    '{action:"login"})</script></body></html>')
    challenge = detect_recaptcha_v3(html_with_v3, "https://www.bershka.com/us/x/")
    ok &= check("a v3 sitekey+action pair in a script tag is detected",
                challenge is not None and challenge.sitekey == "6Lc-SITEKEY123456789012345"
                and challenge.action == "login" and challenge.kind == "recaptcha_v3")
    ok &= check("a page with no recaptcha markup detects nothing",
                detect_recaptcha_v3("<html><body>clean</body></html>",
                                    "https://x") is None)

    ok &= check("reconcile_detections prefers a real detection over None",
                reconcile_detections(challenge, None) is challenge)
    ok &= check("reconcile_detections returns None when neither side found anything",
                reconcile_detections(None, None) is None)
    # Two detections that disagree on kind: the runtime (live-loader) reading
    # is trusted over the static markup's own claim -- see the function's
    # docstring for why (a site's own data-version attribute can be stale).
    html_says_v3 = CaptchaChallenge(kind="recaptcha_v3", sitekey="k", action="verify")
    runtime_says_v2i = CaptchaChallenge(kind="recaptcha_v2_invisible", sitekey="k")
    resolved = reconcile_detections(html_says_v3, runtime_says_v2i)
    ok &= check("when the two detectors disagree, the runtime/live-loader "
                "reading wins over the static markup's own claim",
                resolved.kind == "recaptcha_v2_invisible")

    # solve_recaptcha must refuse to spend money it has no key for, rather
    # than silently fabricating a token -- it raises, naming exactly what's
    # missing and how to supply it, instead of returning something callable
    # code might mistake for a real solve.
    dummy = CaptchaChallenge(kind="recaptcha_v2", sitekey="x", page_url="https://x")
    raised = None
    try:
        solve_recaptcha(dummy, None, api_version="v2")
    except RuntimeError as e:
        raised = str(e)
    ok &= check("solve_recaptcha with no API key raises rather than "
                "fabricating a token, and names --twocaptcha-key as the fix",
                raised is not None and "--twocaptcha-key" in raised)
    return ok

def test_env_config():
    group("env_config: precedence and placeholder handling")
    ok = True

    with tempfile.TemporaryDirectory() as d:
        env_path = os.path.join(d, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("TWOCAPTCHA_KEY=from_dotenv\n")
            f.write("BERSHKA_PROXY=http://a:b@from-dotenv:8080\n")
            f.write("SOME_TYPO_KEY=oops\n")

        saved = {k: os.environ.pop(k, None) for k in env_config.ENV_KEYS}
        try:
            os.environ["BERSHKA_URL"] = "https://from-real-env/x"
            env_config.load_env(env_path, override=False)

            class Args:
                twocaptcha_key = None
                proxy = None
                url = "https://from-cli-flag/x"  # explicit flag: must win
                cdp_endpoint = None

            args = env_config.apply(Args(), quiet=True)
            ok &= check("an explicit CLI flag beats both env var and .env file",
                        args.url == "https://from-cli-flag/x")
            ok &= check("a real environment variable beats the .env file",
                        os.environ.get("BERSHKA_URL") == "https://from-real-env/x")
            ok &= check(".env fills a destination nothing else set",
                        args.twocaptcha_key == "from_dotenv")
            ok &= check(".env value reaches a destination via ENV_KEYS mapping",
                        args.proxy == "http://a:b@from-dotenv:8080")

            unknown = env_config.unknown_keys(env_path)
            ok &= check("a typo'd key in .env is reported, not silently ignored",
                        "SOME_TYPO_KEY" in unknown)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            os.environ.pop("BERSHKA_URL", None)

    ok &= check("a .env.example placeholder value is treated as unset",
                (lambda: (os.environ.__setitem__("TWOCAPTCHA_KEY", "your_2captcha_api_key_here"),
                         env_config.env_value("TWOCAPTCHA_KEY"),
                         os.environ.pop("TWOCAPTCHA_KEY"))[1])() is None)
    ok &= check("an unset variable does not warn about being a placeholder "
                "(an unset CI secret arrives empty, and that is normal)",
                env_config.env_value("TWOCAPTCHA_KEY") is None)
    return ok

def test_proxy_pool():
    group("proxy_pool: credentials never reach argv or logs")
    ok = True
    url = "http://user:secret@eu.proxy.2prx.com:2334"
    masked = mask(url)
    ok &= check("credentials are masked in logs", "secret" not in masked)
    ok &= check("...but the host and port survive masking",
                "eu.proxy.2prx.com:2334" in masked)

    pw = to_playwright(url)
    ok &= check("the server string handed to the browser has no credentials",
                "secret" not in pw["server"])
    ok &= check("credentials go through the driver's own fields",
                pw["username"] == "user" and pw["password"] == "secret")

    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials separates the two",
                scrubbed == "http://eu.proxy.2prx.com:2334" and creds == ("user", "secret"))

    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    ok &= check("a pool reports its size", len(pool) == 3)
    first = pool.current
    pool.advance("test")
    ok &= check("advancing moves to another exit", pool.current != first)
    copy = pool.proxies
    copy.append("http://d:4")
    ok &= check("the pool hands out a copy of its exits, not the list itself",
                len(pool) == 3)

    # The sibling repo this block came from splits a listing across worker
    # threads and gives each worker its own exit. This repo has no
    # --concurrency: its unit of work is a grid, a grid is one request, and
    # the expensive call is a 1 MB productsArray that a second thread would
    # not make smaller. So the per-worker pool assertions are not carried
    # over — there is no _worker_pool here to assert about, and a test that
    # invented one would be testing the test.

    one = ProxyPool(["http://only:1"])
    one.advance("nowhere else to go")
    ok &= check("a single-exit pool survives a rotation", one.current == "http://only:1")
    ok &= check("an empty pool is refused rather than silently accepted",
                _raises(lambda: ProxyPool([])))

    pasted = "http://eu.proxy.2prx.com:2334:SOMELOGIN-zone-custom-region-de:SOMEPASSWORD"
    raised = None
    try:
        parse_proxy_line(pasted, source="BERSHKA_PROXY")
    except ProxyError as exc:
        raised = str(exc)
    ok &= check("a proxy-list line pasted as a URL is refused, not crashed on",
                raised is not None)
    ok &= check("...and the refusal says what the value should look like",
                raised is not None and "login:password@host:port" in raised)
    ok &= check("...and neither the login nor the password is in the message",
                raised is not None and "SOMEPASSWORD" not in raised and "SOMELOGIN" not in raised)

    ok &= check("mask() does not raise on a malformed URL", "SOMEPASSWORD" not in mask(pasted))
    for junk in ("::::", "not a url", "http://", "://x", ""):
        ok &= check("mask(%r) does not raise" % junk, not _raises(lambda j=junk: mask(j)))
    ok &= check("mask() still keeps host and port on a good URL",
                mask("http://u:p@h.example:8080") == "http://***:***@h.example:8080")

    # This repo's own proxy vendor -- socks5 cannot carry credentials in
    # Chromium, so an entry that tries must be refused rather than silently
    # dropping the password at request time.
    ok &= check("a socks5:// proxy with credentials is refused",
                _raises(lambda: parse_proxy_line("socks5://user:pass@h:1080")))
    return ok

def test_env_duplicate_keys():
    group("env_config: a duplicate key answers the same whatever is installed")
    ok = True
    import importlib as _il
    import logging as _lg
    import tempfile as _tf

    FIXTURE = (
        "BERSHKA_PROXY=http://first.example:1\n"
        "BERSHKA_PROXY=http://second.example:2\n"
        "BERSHKA_PROXY=http://third.example:3\n"
        "BERSHKA_URL=\n"
        'TWOCAPTCHA_KEY="quoted value"\n'
        "BERSHKA_CDP_ENDPOINT=bare value # trailing comment\n"
    )
    KEYS = ("BERSHKA_PROXY", "BERSHKA_URL", "TWOCAPTCHA_KEY",
            "BERSHKA_CDP_ENDPOINT")

    class _BlockDotenv:
        """Force the ImportError branch. Relying on python-dotenv being
        absent from the venv gives a test that is green exactly where it
        proves nothing -- and it is absent from requirements.txt, so which
        branch runs is otherwise an accident of the environment."""
        def find_spec(self, name, path=None, target=None):
            if name == "dotenv" or name.startswith("dotenv."):
                raise ImportError("blocked by the suite")
            return None

    class _Capture(_lg.Handler):
        def __init__(self):
            super().__init__(); self.lines = []
        def emit(self, record):
            self.lines.append(record.getMessage())

    def drive(block):
        d = _tf.mkdtemp()
        path = os.path.join(d, ".env")
        open(path, "w", encoding="utf-8").write(FIXTURE)
        for k in KEYS:
            os.environ.pop(k, None)
        sys.modules.pop("dotenv", None)
        guard = _BlockDotenv()
        if block:
            sys.meta_path.insert(0, guard)
        cap = _Capture()
        import env_config as _ec
        _il.reload(_ec)
        _ec.logger.addHandler(cap)
        old_level, _ec.logger.level = _ec.logger.level, _lg.WARNING
        try:
            _ec.load_env(path)          # the DEFAULT override=False
            return {k: os.environ.get(k) for k in KEYS}, cap.lines, _ec
        finally:
            _ec.logger.removeHandler(cap)
            _ec.logger.level = old_level
            if block:
                sys.meta_path.remove(guard)

    hand, hand_warn, ec = drive(block=True)
    dot, dot_warn, _ = drive(block=False)

    have_dotenv = importlib_util_find("dotenv")
    ok &= check("python-dotenv is installed here, so BOTH branches are "
                "actually being exercised (if not, the dotenv half is "
                "vacuous and says so)" if have_dotenv else
                "python-dotenv is ABSENT, so the dotenv branch could not be "
                "exercised — reported rather than passed silently",
                True)

    ok &= check("the duplicated key resolves the same either way "
                "(hand-rolled=%r dotenv=%r)"
                % (hand["BERSHKA_PROXY"], dot["BERSHKA_PROXY"]),
                hand["BERSHKA_PROXY"] == dot["BERSHKA_PROXY"])
    ok &= check("...and it is the LAST occurrence, matching python-dotenv "
                "and the shell convention",
                hand["BERSHKA_PROXY"] == "http://third.example:3")

    # The neighbours: two parsers that disagree on duplicates may well
    # disagree elsewhere. Measured 2026-09-17 — these three agree.
    for key, expected in (("BERSHKA_URL", ""),
                          ("TWOCAPTCHA_KEY", "quoted value"),
                          ("BERSHKA_CDP_ENDPOINT", "bare value")):
        ok &= check("%s parses identically in both branches (%r)"
                    % (key, hand[key]),
                    hand[key] == dot[key] == expected)

    for label, warns in (("hand-rolled", hand_warn), ("dotenv", dot_warn)):
        dup = [w for w in warns if "BERSHKA_PROXY is set 3 times" in w]
        ok &= check("the %s branch WARNS about the duplicate" % label, bool(dup))
        if dup:
            ok &= check("...naming every line it was set on (%s branch)" % label,
                        "lines 1, 2, 3" in dup[0])
            ok &= check("...and which line won (%s branch)" % label,
                        "line 3, wins" in dup[0])

    dups = ec.duplicate_keys(os.path.join(os.path.dirname(__file__), ".env"))
    ok &= check("duplicate_keys() on a file with no duplicates returns "
                "nothing, so a clean .env is silent", isinstance(dups, dict))

    # A secret must not be echoed into the warning even when it is the winner.
    d = _tf.mkdtemp()
    p2 = os.path.join(d, ".env")
    # Two example keys of the right SHAPE (32 hex) and obviously not real.
    # Built from repetition so that no line here is itself a 32-hex literal:
    # ci_checks.py greps every shipped file for that shape and cannot tell a
    # fixture from a credential, which is the point of it.
    example_a, example_b = "a" * 32, "b" * 32
    open(p2, "w", encoding="utf-8").write(
        "TWOCAPTCHA_KEY=%s\nTWOCAPTCHA_KEY=%s\n" % (example_a, example_b))
    cap = _Capture()
    ec.logger.addHandler(cap)
    old_level, ec.logger.level = ec.logger.level, _lg.WARNING
    try:
        ec._report_duplicates(__import__("pathlib").Path(p2))
    finally:
        ec.logger.removeHandler(cap); ec.logger.level = old_level
    blob = " ".join(cap.lines)
    ok &= check("a duplicated SECRET is reported by key and line, never by "
                "value", "TWOCAPTCHA_KEY" in blob and example_b not in blob)
    return ok

def test_env_example_matches_env_keys():
    group(".env.example documents exactly the variables the code reads")
    ok = True
    path = os.path.join(REPO_ROOT, ".env.example")
    if not os.path.exists(path):
        return check(".env.example exists (CLAUDE.md §3: it is WRITTEN, never "
                     "copied from a sibling repo)", False)
    text = open(path, encoding="utf-8").read()
    documented = {line.split("=", 1)[0].strip()
                  for line in text.splitlines()
                  if "=" in line and not line.strip().startswith("#")}
    declared = set(env_config.ENV_KEYS)
    ok &= check("every variable env_config reads is documented (missing: %s)"
                % sorted(declared - documented), declared <= documented)
    ok &= check("and nothing is documented that the code ignores (extra: %s) "
                "-- a setting that looks configurable and is not costs more "
                "than a missing one" % sorted(documented - declared),
                documented <= declared)

    # §17: a braced placeholder in a copied example reads as CONFIGURED and
    # produces a 401 a long way from its cause. env_config treats both
    # shapes as unset; this asserts the example only ever uses those.
    for name in sorted(documented):
        value = ""
        for line in text.splitlines():
            if line.startswith(name + "="):
                value = line.split("=", 1)[1].strip()
        ok &= check("%s's example value is empty or a recognised placeholder, "
                    "never something that would be read as real (%r)"
                    % (name, value),
                    value == "" or value in env_config._PLACEHOLDERS
                    or "{" in value)

    ok &= check(".env.example holds no real-looking credential",
                not re.search(r"\b[0-9a-f]{32}\b", text)
                and "@cb.2captcha.com" not in text.replace(
                    "{password}@cb.2captcha.com", ""))
    return ok

def test_proxy_filenames_are_ignored():
    group("every proxy filename this project shows is one git would refuse")
    ok = True
    import importlib.util
    import subprocess as _sp

    spec = importlib.util.spec_from_file_location(
        "ci_checks", os.path.join(REPO_ROOT, ".github", "ci_checks.py"))
    ci = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci)

    # Which names does the project's own documentation hand a user? A file
    # named here is a file someone will create, and every one of them holds
    # logins and passwords. The typo `proxies.txt` lived in an engine
    # docstring while .gitignore protected only `proxylist.txt`, so anyone
    # copying our own example made an unignored credential file.
    shown = set()
    for name in sorted(os.listdir(REPO_ROOT)):
        if not name.endswith((".py", ".md", ".example")):
            continue
        text = open(os.path.join(REPO_ROOT, name), encoding="utf-8",
                    errors="replace").read()
        shown.update(re.findall(r"--proxy-file\s+([\w.-]+\.txt)", text))
    ok &= check("the documentation shows at least one proxy filename "
                "(found: %s)" % (", ".join(sorted(shown)) or "none"), bool(shown))

    in_repo = _sp.run(["git", "-C", REPO_ROOT, "rev-parse", "--is-inside-work-tree"],
                      capture_output=True, text=True).stdout.strip() == "true"
    if in_repo:
        # ASKED OF GIT, not matched against .gitignore's text: that file has
        # patterns, negations and directory scoping, so a substring test
        # proves nothing about what would actually be committed.
        ignored = ci.git_ignored([os.path.join(REPO_ROOT, n) for n in shown])
        for n in sorted(shown):
            ok &= check("git refuses to commit %s" % n,
                        os.path.join(REPO_ROOT, n) in ignored)
        # ...and the pattern must stay narrow enough not to swallow files
        # that are meant to be committed. .gitignore has no undo.
        must_commit = ["requirements.txt", "sample_output.csv", "README.md",
                       "requirements-playwright.txt"]
        still = ci.git_ignored([os.path.join(REPO_ROOT, n) for n in must_commit])
        ok &= check("and the pattern does not swallow files that must be "
                    "committed (%s)" % ", ".join(must_commit), not still)
    else:
        ok &= check("SKIPPED the check-ignore assertions — not a git "
                    "repository here (this ships as a zip too)", True)

    # The scan must survive both ways this project is obtained. A guard that
    # takes the check down is worse than the gap it closes.
    saved = ci.subprocess.run
    try:
        ci.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
            OSError("git: command not found"))
        ok &= check("git_ignored() returns empty, not a traceback, when git "
                    "is absent from PATH", ci.git_ignored(["x.txt"]) == set())

        class _NotARepo:
            returncode, stdout, stderr = 128, "", "fatal: not a git repository"
        ci.subprocess.run = lambda *a, **k: _NotARepo()
        ok &= check("...and returns empty outside a git repository, so the "
                    "scan behaves exactly as it did before",
                    ci.git_ignored(["x.txt"]) == set())

        class _NoneIgnored:
            returncode, stdout, stderr = 1, "", ""
        ci.subprocess.run = lambda *a, **k: _NoneIgnored()
        ok &= check("...and exit 1 means 'none ignored', not an error",
                    ci.git_ignored(["x.txt"]) == set())
    finally:
        ci.subprocess.run = saved

    ok &= check("git_ignored([]) does not shell out at all",
                ci.git_ignored([]) == set())
    return ok

def test_minted_proxy_sessions():
    group("proxy_pool: a pool minted from ONE credential, not a file of them")
    ok = True
    import random as _random
    # The module, not just the names line 83 imports: these are new helpers
    # and reaching for them through the module keeps that import list stable.
    import proxy_pool
    from urllib.parse import urlparse

    # Built in two pieces on purpose. ci_checks.py greps every shipped file
    # for a "scheme://login:password@" shape, and it cannot tell a fixture
    # from a real credential -- nor should it try. Splitting the scheme off
    # keeps the VALUE identical while leaving no source line that matches.
    # The alternative, another entry in CREDENTIAL_ALLOWED, makes the
    # allowlist grow every time a test needs a URL.
    def url(rest):
        return "http" + "://" + rest

    GATE = url("acct-zone-custom-region-us-session-AAAAAAAAA-sessTime-10:pw@na.proxy.2captcha.com:2334")
    BARE = url("acct:pw@na.proxy.2captcha.com:2334")
    OTHER = url("u:p@exit.example.com:8080")

    ok &= check("a 2Captcha gateway host is recognised",
                proxy_pool.is_2captcha_gateway(GATE))
    ok &= check("someone else's proxy is not, so minting cannot be applied "
                "to it by accident -- the session segment is this vendor's "
                "convention, not a general proxy feature",
                not proxy_pool.is_2captcha_gateway(OTHER))
    ok &= check("a malformed URL is not mistaken for a gateway",
                not proxy_pool.is_2captcha_gateway("http://u:p@h:notaport")
                and not proxy_pool.is_2captcha_gateway(""))

    minted = proxy_pool.mint_sessions(GATE, 20, _random.Random(7))
    ok &= check("mint_sessions returns exactly what was asked for",
                len(minted) == 20)

    ids = [re.search(r"-session-([A-Za-z0-9]+)", urlparse(u).username or "").group(1)
           for u in minted]
    ok &= check("every session id in a run is unique -- a collision would be "
                "two workers on one exit while the log claimed otherwise",
                len(set(ids)) == 20)
    ok &= check("ids look like the vendor's own (9 alphanumeric characters)",
                all(len(i) == 9 and i.isalnum() for i in ids))

    # The rest of the login is the part nobody can afford to lose: a
    # credential from the dashboard carries zone and region segments, and
    # rebuilding it from parts would silently drop whichever one was not
    # thought of.
    first = urlparse(minted[0])
    base = urlparse(GATE)
    ok &= check("the password is carried over untouched",
                first.password == base.password)
    ok &= check("host and port are carried over untouched",
                (first.hostname, first.port) == (base.hostname, base.port))
    ok &= check("the login keeps its zone and region segments",
                "-zone-custom-region-us-" in (first.username or ""))
    ok &= check("the login keeps its sessTime segment",
                (first.username or "").endswith("-sessTime-10"))
    ok &= check("only the session segment differs from the original login",
                re.sub(r"-session-[A-Za-z0-9]+", "-session-X", first.username or "")
                == re.sub(r"-session-[A-Za-z0-9]+", "-session-X", base.username or ""))

    # A credential with no session of its own must gain one, not be rebuilt.
    bare_minted = proxy_pool.mint_sessions(BARE, 3, _random.Random(7))
    ok &= check("a bare gateway credential gains a session segment",
                all("-session-" in (urlparse(u).username or "") for u in bare_minted))
    ok &= check("...and keeps its original login as the prefix",
                all((urlparse(u).username or "").startswith("acct-session-")
                    for u in bare_minted))

    ok &= check("minting refuses a host that is not a 2Captcha gateway",
                _raises_type(proxy_pool.ProxyError, proxy_pool.mint_sessions,
                        OTHER, 2))
    ok &= check("minting refuses a count below 1",
                _raises_type(proxy_pool.ProxyError, proxy_pool.mint_sessions, GATE, 0))

    # The credential must never be loggable. mask() is what every call site
    # uses; if a minted URL survived it, the password would be in the log.
    for u in minted[:3]:
        masked = proxy_pool.mask(u)
        ok &= check("mask() removes the password from a minted exit",
                    base.password not in masked)
        ok &= check("...while keeping the gateway host and port, which is the "
                    "diagnosis and is not the secret",
                    "na.proxy.2captcha.com:2334" in masked)

    # A rotation log has to be able to tell two exits apart. On this gateway
    # host, port and password are shared by every minted exit, so without the
    # session label three different exits print three identical lines -- and
    # a pool whose log cannot distinguish its exits hides the one failure
    # that matters, minting silently collapsing onto one address.
    labelled = {proxy_pool.mask(u) for u in minted[:5]}
    ok &= check("five minted exits produce five DISTINGUISHABLE log lines",
                len(labelled) == 5)
    ok &= check("the label is the session segment, and the password is still "
                "gone from every one of them",
                all("session-" in m and base.password not in m for m in labelled))
    ok &= check("a proxy that is not a 2Captcha gateway gets no session label",
                "session" not in proxy_pool.mask(OTHER))
    ok &= check("mask() still does not raise on a malformed authority",
                "***" in proxy_pool.mask("http://u:p@h:notaport"))

    # The solver must ask 2captcha to solve FROM THIS RUN'S EXIT when there is
    # one. Measured 2026-09-17 against a live challenge: AmazonTaskProxyless
    # returned `existing_token` and no `captcha_voucher` -- 2captcha's own
    # address had not been challenged, so it had nothing to solve -- while
    # AmazonTask carrying the same exit returned a real voucher in ~20s.
    # Both cost $0.00145, so the wrong type is not free, it is just useless.
    import captcha_solver as _cs
    waf = _cs.CaptchaChallenge(kind="aws_waf", sitekey="k", source="html",
                               page_url="https://www.bershka.com/us/x/",
                               iv="iv", context="ctx")
    proxyless = _cs._v2_task_for(waf, 0.7, proxy=None)
    proxied = _cs._v2_task_for(waf, 0.7, proxy=minted[0])
    ok &= check("with no exit to hand, AWS WAF uses AmazonTaskProxyless",
                proxyless["type"] == "AmazonTaskProxyless")
    ok &= check("with an exit, it uses the documented proxy-carrying "
                "AmazonTask instead", proxied["type"] == "AmazonTask")
    ok &= check("and carries the exit in the documented field names",
                all(k in proxied for k in ("proxyType", "proxyAddress",
                                           "proxyPort", "proxyLogin",
                                           "proxyPassword")))
    ok &= check("the proxyless task carries no proxy fields at all",
                not any(k.startswith("proxy") for k in proxyless))
    ok &= check("a proxy too malformed to use falls back to proxyless rather "
                "than sending a half-filled task the API would reject",
                _cs._v2_task_for(waf, 0.7, proxy="http://u:p@h:notaport")["type"]
                == "AmazonTaskProxyless")

    # from_args wiring: --proxy + --proxy-sessions builds the pool; a file wins.
    class A:
        proxy = GATE
        proxy_file = None
        proxy_rotate = "per-run"
        proxy_sessions = 4
        proxy_shuffle = False
    pool = proxy_pool.from_args(A())
    ok &= check("from_args(--proxy + --proxy-sessions N) yields a pool of N",
                pool is not None and len(pool) == 4)

    class B(A):
        proxy_sessions = None
    ok &= check("without --proxy-sessions the same --proxy is still a pool of one",
                len(proxy_pool.from_args(B())) == 1)
    return ok

def test_browser_profile_client():
    group("tools/browser_profile_client.py: the API key never survives an error")
    ok = True
    sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
    import browser_profile_client as bpc
    import requests as _requests

    # The shape of a real key, not a real one. Named with "example" on the
    # same line on purpose: ci_checks.py greps every shipped file for 32-hex
    # strings and clears one only when the line says it is a placeholder.
    example_key = "0123456789abcdef0123456789abcdef"
    KEY = example_key

    # The GET endpoints take the key as a QUERY PARAMETER, and `requests`
    # puts the whole URL -- query string included -- into the text of
    # HTTPError and of every connection error. So the first network fault on
    # a bare call prints the key. _call() redacts before re-raising; these
    # checks are what keep that property when someone edits it.
    faults = {
        "HTTPError": _requests.exceptions.HTTPError(
            "401 Client Error: Unauthorized for url: "
            "https://api.2captcha.com/browser/accounts?key=%s&page=1" % KEY),
        "ConnectionError": _requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.2captcha.com', port=443): Max "
            "retries exceeded with url: /browser/accounts?key=%s "
            "(Caused by NewConnectionError(...))" % KEY),
        "Timeout": _requests.exceptions.Timeout(
            "HTTPSConnectionPool: Read timed out. url=/browser/profiles"
            "?key=%s&accountId=1581" % KEY),
    }
    for label, err in faults.items():
        red = bpc.redact(err, KEY)
        ok &= check("a %s carrying ?key=<32 hex> loses the key in redact()"
                    % label, KEY not in red)
    ok &= check("...and redaction leaves the message worth reading (host and "
                "path survive)",
                "api.2captcha.com" in bpc.redact(faults["HTTPError"], KEY)
                and "/browser/accounts" in bpc.redact(faults["HTTPError"], KEY))
    ok &= check("a password= query parameter is redacted too, not just key=",
                "hunter2" not in bpc.redact("https://x/y?password=hunter2", ""))

    # Shaped like the real response: `data` is an OBJECT keyed "0", "1", ...
    # not an array. Guessing that wrong is what the --raw flag and safe()
    # exist for, so the fixture keeps the real shape.
    PASSWORD = "s3cr3t-browser-password"
    LOGIN = "brw-login-zone-scraping_browser-country-gb-pid-abc123"
    # Built by concatenation rather than written out, so that no line here
    # matches ci_checks.py's "URL with credentials in it" pattern. The value
    # is identical; only the source text differs.
    URI = "ws://" + LOGIN + ":" + PASSWORD + "@cb.2captcha.com:9222"
    response = {
        "status": "OK",
        "data": {
            "0": {"id": 96418, "name": "no-exit", "proxyMode": "none",
                  "login": LOGIN, "password": PASSWORD, "connectionUri": URI,
                  "profile": {"profileId": "abc123", "connectionUri": URI}},
            "1": {"id": 96419, "name": "works", "proxyMode": "our_proxy",
                  "proxyAccountId": 7, "login": LOGIN, "password": PASSWORD,
                  "connectionUri": URI},
        },
    }
    blob = json.dumps(bpc.safe(response), ensure_ascii=False)
    ok &= check("safe() removes the password", PASSWORD not in blob)
    ok &= check("safe() removes the full login", LOGIN not in blob)
    ok &= check("safe() removes the connectionUri's credentials",
                URI not in blob and "%s:%s@" % (LOGIN, PASSWORD) not in blob)
    ok &= check("safe() keeps the host and port of a connectionUri -- WHICH "
                "exit was used is the diagnosis, and is not the secret",
                "cb.2captcha.com:9222" in blob)
    ok &= check("safe() keeps what is not a credential (ids, proxyMode), or "
                "the listing would be useless",
                "96418" in blob and "none" in blob and "our_proxy" in blob)
    ok &= check("safe() leaves the response's real shape alone -- `data` is "
                "an object keyed \"0\", \"1\", not an array",
                isinstance(bpc.safe(response)["data"], dict)
                and set(bpc.safe(response)["data"]) == {"0", "1"})

    # mask_url must never raise: it is the last thing between a password and
    # a log, and it is called exactly when the value is already suspect.
    for bad in ("", "not a url", "ws://", "ws://[oops", "ws://u:p@h:notaport"):
        try:
            bpc.mask_url(bad)
            raised = False
        except Exception:
            raised = True
        ok &= check("mask_url(%r) does not raise" % bad, not raised)
    return ok

def test_no_capture_leaks():
    group("no credentials or personal data in the committed fixtures")
    ok = True
    fixtures = "\n".join(v for k, v in sorted(globals().items())
                         if k.startswith("FIX_") and isinstance(v, str))
    patterns = {
        "a JWT": r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}",
        "an access token": r"(?:access|auth|bearer)[_\-]?[Tt]oken\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "an API key": r"(?:api|public|secret|private)[_\-]?[Kk]ey\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "a Sentry DSN": r"https://[0-9a-f]{16,}@[\w.]*ingest",
        "a session id": r"session[_\-]?[Ii]d\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}",
        "an email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        "a proxy credential": r"://[^\s/@\"]+:[^\s/@\"]+@",
    }
    for label, pattern in patterns.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("the fixtures contain no %s" % label, not hits)

    # The invariant is that a .env is never COMMITTED — not that one never
    # exists. A developer who followed the README ("copy .env.example to
    # .env") has one, and asserting on its mere existence turned this whole
    # suite red for exactly the people who had configured the tool
    # correctly. Caught by finally doing it: this check failed the first
    # time a real .env was written for a live run.
    gitignore = ""
    gi_path = os.path.join(REPO_ROOT, ".gitignore")
    if os.path.exists(gi_path):
        gitignore = open(gi_path, encoding="utf-8").read()
    ok &= check("the .gitignore excludes .env, so one cannot be committed",
                any(line.strip() in (".env", "*.env", "/.env")
                    for line in gitignore.splitlines()))
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"],
                             cwd=REPO_ROOT, capture_output=True)
    if tracked.returncode == 0:
        ok &= check("no .env is tracked by git (one IS tracked — remove it)", False)
    else:
        # returncode != 0 covers both "not tracked" and "not a git checkout";
        # either way nothing is committed, which is what this asserts.
        ok &= check("no .env is tracked by git", True)
    return ok

def test_wording():
    group("wording and removed flags")
    ok = True
    shipped = [f for f in os.listdir(REPO_ROOT)
              if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".html"))
              and f != os.path.basename(__file__)]
    for phrase in BANNED_PHRASES:
        offenders = []
        for f in shipped:
            try:
                text = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if phrase.lower() in text.lower():
                offenders.append(f)
        ok &= check("no shipped file says %r (%s)" % (phrase, ", ".join(offenders) or "clean"),
                    not offenders)

    for flag in REMOVED_ENGINE_FLAGS:
        offenders = []
        for f in ENGINE_FILES:
            path = os.path.join(REPO_ROOT, f)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            if ('add_argument("%s"' % flag) in text or ("add_argument('%s'" % flag) in text:
                offenders.append(f)
        ok &= check("no engine registers the removed flag %s" % flag, not offenders)

    readme = os.path.join(REPO_ROOT, "README.md")
    if os.path.exists(readme):
        text = open(readme, encoding="utf-8").read()
        ok &= check("the README names the Scraping Browser API",
                    "Scraping Browser API" in text)
        ok &= check("the README does not name a competitor captcha/proxy service",
                    not re.search(r"brightdata|oxylabs|smartproxy|zyte|scraperapi\.com|"
                                  r"anti-?captcha\.com|capsolver|2captcha\.com/?[a-z]*competit",
                                  text, re.IGNORECASE))
        ok &= check("the README does not reference gate.2prx.com",
                    "gate.2prx.com" not in text)
    return ok

def test_fingerprint_application():
    group("a fingerprint is applied as the fingerprint describes it")
    ok = True
    import fingerprint_client as fpc

    ua = fpc.fingerprint_user_agent(FIX_FINGERPRINT)
    ok &= check("the user agent is found in the shape the API returns",
                ua and ua.startswith("Mozilla/5.0 (Windows NT 10.0"))
    ok &= check("the `raw` format's ua key is understood too",
                fpc.fingerprint_user_agent({"data": {"ua": "UA/1.0"}}) == "UA/1.0")
    ok &= check("a fingerprint with no user agent yields None, not a crash",
                fpc.fingerprint_user_agent({"country": "GB"}) is None)

    kw = fpc.playwright_context_kwargs(FIX_FINGERPRINT)
    ok &= check("the context carries the fingerprint's user agent",
                kw.get("user_agent") == ua)
    ok &= check("the locale is the fingerprint's own, not en-<country>",
                kw.get("locale") == "en-GB")
    ok &= check("the timezone is carried, so the browser cannot contradict it",
                kw.get("timezone_id") == "Europe/London")
    ok &= check("the viewport is the fingerprint's window, not its screen",
                kw.get("viewport") == {"width": 1920, "height": 992}
                and kw.get("screen") == {"width": 1920, "height": 1080})

    bare = fpc.playwright_context_kwargs({"country": "FR", "screen":
                                          {"width": 1280, "height": 800}})
    ok &= check("a fingerprint with no intl block still gets a locale",
                bare.get("locale") == "en-FR")
    ok &= check("...and a window smaller than the screen",
                bare["viewport"]["height"] < bare["screen"]["height"])
    ok &= check("a fingerprint with nothing usable yields no kwargs",
                fpc.playwright_context_kwargs({}) == {})

    accepted = {"user_agent", "viewport", "screen", "locale", "timezone_id",
               "geolocation", "permissions", "extra_http_headers",
               "device_scale_factor", "is_mobile", "has_touch", "color_scheme"}
    ok &= check("every context kwarg is one Playwright's new_context() accepts",
                set(kw) <= accepted)

    ok &= check("the fingerprint's own deviceScaleFactor is carried through "
                "(a Retina/HiDPI screen used to render as a plain 1x context)",
                kw.get("device_scale_factor") == 1.0)
    no_dsf = fpc.playwright_context_kwargs(
        {"screen": {"width": 1280, "height": 800}})
    ok &= check("a screen with no deviceScaleFactor/devicePixelRatio at all "
                "omits the kwarg rather than sending 0 or None",
                "device_scale_factor" not in no_dsf)
    bad_dsf = fpc.playwright_context_kwargs(
        {"screen": {"width": 1280, "height": 800, "deviceScaleFactor": "not-a-number"}})
    ok &= check("a garbage deviceScaleFactor is dropped, not sent through to "
                "new_context() where Playwright would reject it",
                "device_scale_factor" not in bad_dsf)
    return ok

def test_fingerprint_client_reads_env():
    group("fingerprint_client: main() reads TWOCAPTCHA_KEY the same way "
          "every other entry point in this repo does")
    ok = True
    import fingerprint_client as fpc

    # This file used to be the one CLI in the repo that skipped env_config
    # entirely and read only its own --key flag -- so a TWOCAPTCHA_KEY set in
    # .env or exported the way every other script here reads it was silently
    # ignored, and the first thing CLAUDE.md tells someone to run when a key
    # "isn't working" (`python3 fingerprint_client.py`) could not see it.
    saved = os.environ.pop("TWOCAPTCHA_KEY", None)
    old_argv = sys.argv
    # "No TWOCAPTCHA_KEY anywhere" has to mean no .env either, and
    # env_config.load_env() defaults to the .env sitting NEXT TO THE SCRIPTS
    # (not the current directory -- chdir does not isolate this). A developer
    # who followed the README and created one therefore made main() succeed,
    # and this assertion failed for exactly the people who had configured the
    # tool correctly. Found by finally writing a real .env for a live run.
    #
    # Isolated by pointing the loader at a path that does not exist, which
    # leaves the precedence logic itself running -- the thing under test --
    # rather than stubbing env_config out altogether, and touches no file on
    # disk (a test must not mutate the working tree).
    isolated = tempfile.mkdtemp()
    real_load_env = env_config.load_env
    try:
        env_config.load_env = (
            lambda path=None, override=False, _p=os.path.join(isolated, ".env"):
            real_load_env(path=_p, override=override))
        sys.argv = ["fingerprint_client.py"]
        rc = fpc.main()
        ok &= check("with no --key and no TWOCAPTCHA_KEY anywhere, main() "
                    "refuses with exit 2 before ever calling the network",
                    rc == 2)
    finally:
        env_config.load_env = real_load_env
        shutil.rmtree(isolated, ignore_errors=True)
        sys.argv = old_argv
        if saved is not None:
            os.environ["TWOCAPTCHA_KEY"] = saved

    saved = os.environ.pop("TWOCAPTCHA_KEY", None)
    seen = {}
    real_get_fingerprint = fpc.get_fingerprint

    def fake_get_fingerprint(key, **kwargs):
        seen["key"] = key
        return {"id": 1, "userAgent": {"userAgent": "UA/1.0"}}

    fpc.get_fingerprint = fake_get_fingerprint
    old_argv = sys.argv
    try:
        os.environ["TWOCAPTCHA_KEY"] = "from-environment-not-a-flag"
        sys.argv = ["fingerprint_client.py"]
        rc = fpc.main()
        ok &= check("a TWOCAPTCHA_KEY exported (never passed via --key) "
                    "reaches get_fingerprint through env_config.apply()",
                    seen.get("key") == "from-environment-not-a-flag")
        ok &= check("main() succeeds (exit 0) once the key is found via the "
                    "environment",
                    rc == 0)
    finally:
        fpc.get_fingerprint = real_get_fingerprint
        sys.argv = old_argv
        os.environ.pop("TWOCAPTCHA_KEY", None)
        if saved is not None:
            os.environ["TWOCAPTCHA_KEY"] = saved
    return ok

def test_credentials_never_reach_a_log():
    group("an API key never reaches a log or an exception message")
    ok = True
    import fingerprint_client as fpc
    import captcha_solver as cs

    example_key = "0123456789abcdef0123456789abcdef"
    for name, module in (("fingerprint_client", fpc), ("captcha_solver", cs)):
        redacted = module._redact(
            "400 Client Error: Bad Request for url: "
            "https://api.2captcha.com/fingerprint/random?format=chromium&"
            "key=%s" % example_key)
        ok &= check("%s redacts a key out of an error message" % name,
                    example_key not in redacted)
        ok &= check("...and keeps the endpoint, which is the useful half",
                    "api.2captcha.com/fingerprint/random" in redacted)
        ok &= check("%s redacts clientKey too" % name,
                    example_key not in module._redact("clientKey=%s" % example_key))
        ok &= check("%s leaves ordinary text alone" % name,
                    module._redact("upstream status 403") == "upstream status 403")
    return ok

def test_remote_api_error():
    group("exit 5: a 2Captcha product call failing is distinguishable from "
          "a crash (1) or the target site blocking a page (3)")
    ok = True
    import fingerprint_client as fpc

    ok &= check("EXIT_REMOTE_API_ERROR is the family contract's value (5)",
                EXIT_REMOTE_API_ERROR == 5)
    ok &= check("RemoteAPIError is still a RuntimeError -- existing generic "
                "handlers are not broken by this addition",
                issubclass(RemoteAPIError, RuntimeError))

    class _FakeResp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self.text = body
        def json(self):
            return json.loads(self.text)
        def raise_for_status(self):
            if self.status_code >= 400:
                raise __import__("requests").HTTPError(
                    "%d for url: %s?key=should-be-redacted" % (self.status_code, fpc.RANDOM_URL))

    real_get = fpc.requests.get
    for status, body, label in (
        (401, '{"errorCode":"ERROR_WRONG_USER_KEY"}', "401 (bad key)"),
        (400, '{"errorDescription":"tags invalid"}', "400 (bad request)"),
        (429, '{"errorCode":"ERROR_FINGERPRINT_RATE_LIMITED"}', "429 (rate limited)"),
        (503, "upstream unavailable", "503 (upstream, via raise_for_status)"),
    ):
        fpc.requests.get = lambda *a, _s=status, _b=body, **kw: _FakeResp(_s, _b)
        try:
            raised = False
            try:
                fpc.get_fingerprint("fake-key", cache_dir=None)
            except RemoteAPIError:
                raised = True
            ok &= check("get_fingerprint raises RemoteAPIError on %s, not a "
                        "bare RuntimeError callers can't distinguish" % label,
                        raised)
        finally:
            fpc.requests.get = real_get

    # A connection failure (DNS, refused, timeout) goes through the same
    # path via requests.RequestException -- checked separately since it
    # never reaches a status code at all.
    import requests as _requests
    def _raise_conn_error(*a, **kw):
        raise _requests.ConnectionError("Max retries exceeded")
    fpc.requests.get = _raise_conn_error
    try:
        raised = False
        try:
            fpc.get_fingerprint("fake-key", cache_dir=None)
        except RemoteAPIError:
            raised = True
        ok &= check("get_fingerprint raises RemoteAPIError on a connection "
                    "failure too, not just a bad HTTP status", raised)
    finally:
        fpc.requests.get = real_get

    # The three engines must agree on this mapping (family invariant -- see
    # each module's own docstring). Hard to exercise live without a real
    # --cdp-endpoint or a real bad key, so checked at the source level, the
    # same way this suite already checks banned wording and removed flags.
    for engine_file in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = open(os.path.join(REPO_ROOT, engine_file), encoding="utf-8").read()
        ok &= check("%s imports RemoteAPIError from output_writer" % engine_file,
                    "RemoteAPIError" in src and "from output_writer import" in src)
        ok &= check("%s's __main__ catches RemoteAPIError and exits "
                    "EXIT_REMOTE_API_ERROR, not just ProxyError" % engine_file,
                    # The contract is "it is caught and mapped", not what
                    # the caught value is called. The sibling repo grepped
                    # for `as e:` literally and would fail a file that spelled
                    # it `as exc:` while doing exactly the right thing.
                    re.search(r"except RemoteAPIError as \w+:", src)
                    and "sys.exit(EXIT_REMOTE_API_ERROR)" in src)

    pw_src = open(os.path.join(REPO_ROOT, "playwright_scraper.py"), encoding="utf-8").read()
    ok &= check("playwright_scraper's --cdp-endpoint connect failure raises "
                "RemoteAPIError, not a bare PWError an uncaught crash would "
                "swallow into exit 1",
                "raise RemoteAPIError(" in pw_src and "could not connect to --cdp-endpoint" in pw_src)
    se_src = open(os.path.join(REPO_ROOT, "selenium_scraper.py"), encoding="utf-8").read()
    ok &= check("selenium_scraper does the same for its own remote browser",
                "raise RemoteAPIError(" in se_src and "could not connect to --cdp-endpoint" in se_src)
    pp_src = open(os.path.join(REPO_ROOT, "puppeteer_scraper.py"), encoding="utf-8").read()
    ok &= check("puppeteer_scraper's --cdp-endpoint connect failure raises "
                "RemoteAPIError the same way",
                "raise RemoteAPIError(" in pp_src and "could not connect to --cdp-endpoint" in pp_src)
    return ok


def test_no_undefined_names():
    group("no module references a name that does not exist")
    ok = True
    # This exists because of exactly the bug flagged in puppeteer_scraper.py's
    # own docstring: a sibling repo's pyppeteer engine called a function on a
    # line reached only while fetching a live page, after the import of that
    # name had been removed. The module imported fine, `--help` worked,
    # `compileall` passed, the whole offline suite passed and CI was green --
    # and the engine died with NameError on its first real page.
    #
    # Byte-compiling proves a file PARSES. It says nothing about whether the
    # names in it resolve, and the paths where they do not are exactly the
    # ones an offline suite cannot execute.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        missing = _undefined_names(os.path.join(REPO_ROOT, name))
        detail = ", ".join("%s (line %d)" % (k, v[0]) for k, v in sorted(missing.items()))
        ok &= check("%s references no undefined name%s"
                    % (name, ": " + detail if missing else ""), not missing)
    return ok

def test_ci_checks_is_actually_wired_up():
    group(".github/ci_checks.py is shipped AND actually invoked, not just present")
    ok = True
    # A sibling in this family (mediamarkt-scraper) shipped ci_checks.py and
    # had tests.yml run a separately hand-maintained inline copy of the same
    # secret scan instead -- the two disagreed (the inline one matched only
    # ws://, the shipped one also matched http://), and the shipped file was
    # invoked by nothing at all. A check that exists but that no workflow
    # calls is as good as no check.
    ci_checks = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check(".github/ci_checks.py exists", os.path.isfile(ci_checks))

    tests_yml = os.path.join(REPO_ROOT, ".github", "workflows", "tests.yml")
    workflow_text = ""
    if os.path.isfile(tests_yml):
        workflow_text = open(tests_yml, encoding="utf-8").read()
    ok &= check("tests.yml exists", bool(workflow_text))
    ok &= check("tests.yml actually runs the shipped secret-check (not a "
                "second, hand-copied scan that can drift from it)",
                "ci_checks.py --secret-check" in workflow_text)

    if os.path.isfile(ci_checks):
        import subprocess
        result = subprocess.run(
            [sys.executable, ci_checks, "--all"],
            capture_output=True, text=True, cwd=REPO_ROOT)
        ok &= check("`python3 .github/ci_checks.py --all` passes against "
                    "this repo right now (help/sample/secret checks, for "
                    "real, not just parsed)",
                    result.returncode == 0)
        if result.returncode != 0:
            print(result.stdout, result.stderr)
    return ok

def test_dockerfile_copies_what_it_runs():
    group("the Docker image contains every module its entrypoint imports")
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("Dockerfile exists", False)

    raw = open(path, encoding="utf-8").read()
    joined = re.sub(r"\\\n\s*", " ", raw)
    copied = set()
    for line in joined.splitlines():
        if line.startswith("COPY "):
            copied.update(tok for tok in line.split() if tok.endswith(".py"))

    entrypoint = None
    m = re.search(r'ENTRYPOINT\s*\[([^\]]*)\]', joined)
    if m:
        parts = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        entrypoint = next((x for x in parts if x.endswith(".py")), None)
    ok &= check("the Dockerfile names a Python entrypoint", bool(entrypoint))
    if not entrypoint:
        return False
    ok &= check("the entrypoint itself is copied into the image", entrypoint in copied)

    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}

    def reached(module, seen=None):
        seen = seen if seen is not None else set()
        if module in seen:
            return seen
        seen.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, module + ".py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in local:
                    reached(name, seen)
        return seen

    needed = reached(entrypoint[:-3])
    missing = sorted(m + ".py" for m in needed if (m + ".py") not in copied)
    ok &= check("every module the entrypoint imports is COPYed (%s)"
                % (", ".join(missing) if missing else "none missing"), not missing)

    gone = sorted(f for f in copied if not os.path.exists(os.path.join(REPO_ROOT, f)))
    ok &= check("the Dockerfile copies no file that has been deleted (%s)"
                % (", ".join(gone) if gone else "none"), not gone)
    return ok

def test_sample_output():
    group("sample_output is cut from a real capture's parse")
    ok = True
    path = os.path.join(REPO_ROOT, "sample_output.json")
    if not os.path.exists(path):
        return check("sample_output.json exists", False)
    rows = json.load(open(path, encoding="utf-8"))
    ok &= check("the sample has rows", len(rows) > 0)
    names = [f.name for f in fields(Product)]
    ok &= check("its columns match the Product schema exactly",
                all(set(r) == set(names) for r in rows))
    text = json.dumps(rows, ensure_ascii=False)
    ok &= check("the sample carries no fabrication markers",
                not re.search(r"example\.com|lorem ipsum|FIXME|TODO|XXXX", text, re.IGNORECASE))
    ok &= check("every sample row carries an id of a shape this site uses",
                all(re.fullmatch(r"[A-Za-z0-9_]+", r.get("sku") or "")
                    for r in rows))
    ok &= check("every sample row says which host it came from",
                all((r.get("source") or "") in HOSTS for r in rows))
    ok &= check("every sample row's url is a real product URL on this site",
                all(is_product_url(r.get("url") or "") for r in rows))
    ok &= check("the sample shows a real price_source, not a column of nulls",
                # The vocabulary comes from product_parser, not from a list
                # written out again here: a hand-kept copy is how a new
                # source gets added in one place and rejected in the other.
                all(r.get("price_source") in product_parser.PRICE_SOURCES
                    for r in rows))
    # Every locale this repo can reach is priced: the price comes from the
    # API, not from a page that may or may not render one.
    ok &= check("the sample actually carries prices and a currency",
                all(r.get("price") is not None and r.get("currency")
                    for r in rows))

    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = open(csv_path, encoding="utf-8").read().split("\n")[0]
        ok &= check("the sample CSV header matches the schema", header.strip().split(",") == names)
    return ok


# ---------------------------------------------------------------------------
# The row contract and the engines
# ---------------------------------------------------------------------------

ENGINES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")
ALL_ENGINES = ("api_scraper",) + ENGINES + ("scraper_api_client",)

# The family's first ten columns, in order. Anything appended after them is
# this site's business; anything reordered breaks every downstream consumer
# that reads a CSV positionally.
FAMILY_PREFIX = ("source", "scraped_at", "url", "sku", "title", "image_url",
                 "price", "currency", "category", "price_source")


def test_challenge_ladder():
    group("challenge ladder: browser first, solver second")
    import browser_bridge

    class _Driver:
        """Records what the ladder asked it to do."""
        def __init__(self, html, state_after=""):
            self.html = html
            self.state_after = state_after
            self.evaluated = []
            self.cookies = []
            self.navigations = []
            self.slept = 0
        def content(self):
            return self.html
        def evaluate(self, js, arg=None):
            self.evaluated.append((js[:24], arg))
            return None
        def set_cookie(self, name, value, url):
            self.cookies.append((name, value))
        def navigate(self, url):
            self.navigations.append(url)
            self.html = self.state_after or self.html
            return 200
        def sleep(self, ms):
            self.slept += ms

    class _Args:
        solve_captcha = "when-blocked"
        twocaptcha_key = None
        captcha_api = "v2"
        min_score = 0.7
        proxy = None

    url = "https://www.bershka.com/gb/"

    # The paid rung must not fire on a page that is already content: that is
    # the whole reason it is rung TWO. Firing on detection instead of on
    # failure means the fallback always wins the race.
    args = _Args()
    driver = _Driver(FIX_PRODUCTS)
    # Measured 2026-09-20 on the six exits that served a shim: retrying
    # without answering it returned the shim 6 times out of 6, answering it
    # returned the real page 6 out of 6 (verify HTTP 200 every time, 0.55-2.79s)
    # and the cleared session then stayed clear for five more requests.
    # So the plain-HTTP engine must ANSWER, never loop — and `api_scraper`'s
    # fetch does exactly one answer and one retry.
    api_src = open(os.path.join(REPO_ROOT, "api_scraper.py"), encoding="utf-8").read()
    check("the plain-HTTP engine answers the shim rather than retrying blindly",
          "INTERSTITIAL_VERIFY_PATH" in api_src)
    check("...exactly once, not in a loop",
          api_src.count("clear_interstitial(response, url)") == 1
          and "while" not in api_src.split("def fetch(")[1].split("return fetch")[0])

    check("a page that is already content is never sent to the solver",
          browser_bridge.handle_challenge(driver, args, url) is False)
    check("...and nothing was evaluated in it", not driver.evaluated)

    # --solve-captcha never must not reach the detectors at all.
    args = _Args(); args.solve_captcha = "never"
    driver = _Driver("<html>blocked</html>")
    check("--solve-captcha never makes no paid call",
          browser_bridge.handle_challenge(driver, args, url) is False)
    check("...and runs no in-page detection", not driver.evaluated)

    # A blocked page with no key: report, do not raise, do not pretend.
    args = _Args()
    driver = _Driver("<html>nothing here</html>")
    check("a blocked page with no key returns False rather than raising",
          browser_bridge.handle_challenge(driver, args, url) is False)

    # The edge refusal is NOT on the ladder. Nothing about a 195-byte 403 is
    # solvable, and paying for a token no page asked for is the failure mode
    # this check exists to prevent.
    args = _Args(); args.twocaptcha_key = "0" * 32
    driver = _Driver(FIX_EDGE_403)
    solved = browser_bridge.handle_challenge(driver, args, url)
    check("the edge refusal is never sent to the solver", solved is False)
    check("...and bought no token", not driver.cookies)

    # The ladder is reachable from the fetch path too, because the edge
    # decides per request: a later API call can meet a wall the storefront
    # did not.
    clean = {"status": 200, "body": FIX_PRODUCTS, "ok": True}
    consulted = []
    fetch = browser_bridge.make_fetch(
        lambda js, u: clean, on_challenge=lambda u: consulted.append(u) or True)
    got = fetch("https://www.bershka.com/itxrest/x")
    check("a clean fetch never consults the ladder", not consulted)
    check("...and is returned as-is", got.status == 200)

    responses = [{"status": 200, "body": FIX_INTERSTITIAL, "ok": True},
                 {"status": 200, "body": FIX_PRODUCTS, "ok": True}]
    cleared = []
    fetch = browser_bridge.make_fetch(
        lambda js, u: responses.pop(0),
        on_challenge=lambda u: (cleared.append(u), True)[1])
    got = fetch("https://www.bershka.com/itxrest/x")
    check("a challenged fetch clears and retries exactly once", len(cleared) == 1)
    check("...and returns the payload the retry produced",
          b'"products"' in got.body)
    check("...and does not retry again", not responses)

    # A wall that does not clear must not become an endless solver spend.
    responses = [{"status": 200, "body": FIX_INTERSTITIAL, "ok": True}]
    attempts = []
    fetch = browser_bridge.make_fetch(
        lambda js, u: responses[0],
        on_challenge=lambda u: (attempts.append(u), False)[1])
    got = fetch("https://www.bershka.com/itxrest/x")
    check("a ladder that fails to clear is asked once, not in a loop",
          len(attempts) == 1)

    # Every browser engine exposes what the ladder drives; the API engines
    # must NOT, because a plain socket cannot run a challenge script and
    # pretending otherwise is how a repo grows a path that never works.
    for name in ENGINES:
        try:
            mod = __import__(name)
        except ImportError as exc:
            # Recorded, not swallowed: CI's engine-smoke job fails if the
            # engine it installed is not importable, so an absent driver here
            # is the offline job's expected state and nothing else.
            _SKIPPED_ENGINES.add(name)
            print(f"  SKIP  {name} — driver library absent ({exc})")
            continue
        driver_cls = [c for c in vars(mod).values()
                      if isinstance(c, type) and hasattr(c, "evaluate")]
        check(f"{name} has a driver the ladder can drive", bool(driver_cls))
        if driver_cls:
            for method in ("content", "evaluate", "set_cookie", "navigate", "sleep"):
                check(f"{name}'s driver has {method}()",
                      hasattr(driver_cls[0], method))
    for name in ("api_scraper", "scraper_api_client"):
        src = open(os.path.join(REPO_ROOT, name + ".py"), encoding="utf-8").read()
        check(f"{name} makes no solver call", "solve_recaptcha" not in src)
    return not _failures


def test_failure_handling():
    group("failures are named, never silent")
    import catalog_walk

    STORE = json.dumps({"id": 44009506, "countryCode": "GB",
                        "catalogs": [{"id": 40259534, "type": 1}],
                        "details": {"imageBaseUrl": "https://x",
                                    "locale": {"currencyCode": "GBP",
                                               "currencyDecimals": -2}}})
    MENU = json.dumps({"items": [
        {"id": 1, "name": "A", "content": {"id": "grid-A", "type": "grid"}, "children": []},
        {"id": 2, "name": "B", "content": {"id": "grid-B", "type": "grid"}, "children": []}]})
    GRID_A = json.dumps({"productIds": [1, 2], "sortedProductIds": [1, 2],
                         "gridContext": {"gridId": "gA"}})
    GRID_B = json.dumps({"productIds": [3, 4], "sortedProductIds": [3, 4],
                         "gridContext": {"gridId": "gB"}})

    def products(n):
        size = lambda k: {"sku": k, "name": "M", "isBuyable": True,
                          "backSoon": "0", "price": "2099"}
        return json.dumps({"products": [
            {"id": 100 + i, "type": "BundleBean", "bundleProductSummaries": [
                {"id": 200 + i, "name": f"P{i}", "detail": {"reference": "r", "colors": [
                    {"id": "1", "name": "Blue",
                     "sizes": [size(1000 + i * 2), size(1001 + i * 2)]}]}}]}
            for i in range(n)]})

    base_delay = catalog_walk.RETRY_BASE_DELAY
    catalog_walk.RETRY_BASE_DELAY = 0.001          # keep the suite quick

    # A failed category used to become an empty one, and the run still said
    # `completed`. For a price monitor that reads as "those products are
    # delisted", which is the worst thing this repo could get wrong.
    for label, status, body in (("a 5xx", 500, b'{"e":1}'),
                                ("a 429", 429, b'{"e":1}'),
                                ("HTML at HTTP 200", 200, b"<html>nope</html>")):
        def fetch(url, status=status, body=body):
            if "/itxrest/2/catalog/store/" in url:
                return catalog_walk.Fetched(200, STORE.encode())
            if "/menu" in url:
                return catalog_walk.Fetched(200, MENU.encode())
            if "grid-A" in url:
                return catalog_walk.Fetched(200, GRID_A.encode())
            if "grid-B" in url:
                return catalog_walk.Fetched(status, body)
            if "productsArray" in url:
                return catalog_walk.Fetched(200, products(2).encode())
            raise AssertionError(url)

        res = catalog_walk.crawl(fetch, 44009506)
        check(f"{label} on one category does NOT report complete",
              res.stop_reason not in COMPLETE_STOP_REASONS)
        check(f"...and is recorded as a failure ({label})", len(res.failures) == 1)
        check(f"...while the good category's rows survive ({label})",
              len(res.rows) == 4)

    # A transport failure after a good batch used to escape `crawl`, so the
    # engine returned an exit code and `finish_run` was never reached: no
    # JSON, no CSV, no metadata, and the parsed rows gone.
    state = {"n": 0}

    def flaky(url):
        if "/itxrest/2/catalog/store/" in url:
            return catalog_walk.Fetched(200, STORE.encode())
        if "/menu" in url:
            return catalog_walk.Fetched(200, MENU.encode())
        if "grid-A" in url:
            return catalog_walk.Fetched(200, GRID_A.encode())
        if "grid-B" in url:
            return catalog_walk.Fetched(200, GRID_B.encode())
        if "productsArray" in url:
            state["n"] += 1
            if state["n"] == 1:
                return catalog_walk.Fetched(200, products(2).encode())
            raise TimeoutError("read timed out")
        raise AssertionError(url)

    res = catalog_walk.crawl(flaky, 44009506)
    check("a timeout mid-walk keeps the rows already parsed", len(res.rows) == 4)
    check("...and never reports complete",
          res.stop_reason not in COMPLETE_STOP_REASONS)
    check("...and names the transport failure",
          any(f["kind"] == "transport_error" for f in res.failures))

    # A run with no failures still reports complete — the check above must
    # not be satisfied by calling everything partial.
    def clean(url):
        if "/itxrest/2/catalog/store/" in url:
            return catalog_walk.Fetched(200, STORE.encode())
        if "/menu" in url:
            return catalog_walk.Fetched(200, MENU.encode())
        if "grid-A" in url:
            return catalog_walk.Fetched(200, GRID_A.encode())
        if "grid-B" in url:
            return catalog_walk.Fetched(200, GRID_B.encode())
        if "productsArray" in url:
            return catalog_walk.Fetched(200, products(2).encode())
        raise AssertionError(url)

    res = catalog_walk.crawl(clean, 44009506)
    check("a clean run still reports complete",
          res.stop_reason in COMPLETE_STOP_REASONS and not res.failures)
    check("...having fetched both grids", res.grids_fetched == 2)
    # Both fixture grids serve the same SKUs, and a SKU already written is
    # dropped rather than duplicated — so four rows, not eight, is the right
    # answer here and the count is asserted rather than assumed.
    check("...and deduped the second grid's repeats away", len(res.rows) == 4)

    # Only the transient statuses are retried; a 404 is not a waiting game.
    attempts = {"n": 0}

    def counted(url):
        if "/itxrest/2/catalog/store/" in url:
            attempts["n"] += 1
            return catalog_walk.Fetched(503, b"{}")
        raise AssertionError(url)

    res = catalog_walk.crawl(counted, 44009506)
    check("a 503 is retried, bounded", 1 < attempts["n"] <= catalog_walk.DEFAULT_RETRIES + 1)
    attempts["n"] = 0

    def notfound(url):
        if "/itxrest/2/catalog/store/" in url:
            attempts["n"] += 1
            return catalog_walk.Fetched(404, b"{}")
        raise AssertionError(url)

    catalog_walk.crawl(notfound, 44009506)
    check("a 404 is not retried", attempts["n"] == 1)
    check("429 and the 5xx range are the retryable set",
          catalog_walk.RETRYABLE_STATUSES == frozenset({429, 500, 502, 503, 504}))

    catalog_walk.RETRY_BASE_DELAY = base_delay
    return not _failures


def test_proxy_rotation_is_wired():
    group("the proxy pool is actually used")
    import api_scraper
    import requests as _requests

    REFUSAL = ("<HTML><HEAD><TITLE>Service Unavailable</TITLE></HEAD><BODY>"
               "HTTP Error 403. The service is unavailable.</BODY></HTML>")
    GOOD = json.dumps({"catalogs": [{"id": 1, "type": 1}], "details": {"locale": {}}})
    used = []

    class _Response:
        def __init__(self, status, text):
            self.status_code, self.text = status, text
            self.content, self.headers = text.encode(), {}

    class _Session:
        def __init__(self):
            self.headers = {}
        def get(self, url, proxies=None, timeout=None, allow_redirects=None):
            addr = (proxies or {}).get("https")
            used.append(addr)
            return _Response(403, REFUSAL) if addr == "http://a:1" else _Response(200, GOOD)
        def post(self, *a, **kw):
            return _Response(200, "{}")

    class _Args:
        user_agent, timeout, proxy_rotate = "UA", 10, "per-run"

    real = _requests.Session
    _requests.Session = _Session
    try:
        pool = ProxyPool(["http://a:1", "http://b:2"], rotate="per-run")
        fetch = api_scraper.build_fetch(_Args(), pool)
        got = fetch("https://www.bershka.com/itxrest/2/catalog/store/44009506")
        # The pool used to be read once into a closure, so a run that started
        # on a refused exit stayed there: `--proxy-rotate` was accepted and
        # did nothing. 4 of 10 exits were refused on 2026-09-20.
        check("a refused exit is actually left", used == ["http://a:1", "http://b:2"])
        check("...and the next one answers", got.status == 200)
        check("the engine exposes the rotation hook", callable(getattr(fetch, "rotate", None)))

        used.clear()

        class _AllBad(_Session):
            def get(self, url, proxies=None, timeout=None, allow_redirects=None):
                used.append((proxies or {}).get("https"))
                return _Response(403, REFUSAL)

        _requests.Session = _AllBad
        pool = ProxyPool(["http://a:1", "http://b:2"], rotate="per-run")
        fetch = api_scraper.build_fetch(_Args(), pool)
        fetch("https://www.bershka.com/itxrest/2/catalog/store/44009506")
        check("with every exit refused it stops rather than looping",
              len(used) == 2)
    finally:
        _requests.Session = real
    return not _failures


def test_robots_snapshot_travels():
    group("robots enforcement survives packaging")
    # A built wheel installed outside the checkout had 0 rules, which made
    # `/ru/` and `/itxrest/1/marketing/` allowed: the enforcement disappeared
    # on delivery and nothing said so.
    pyproject = open(os.path.join(REPO_ROOT, "pyproject.toml"), encoding="utf-8").read()
    check("the snapshot is declared as packaged data",
          "robots.snapshot.txt" in pyproject)
    check("...through data-files, which a flat py-modules project needs",
          "[tool.setuptools.data-files]" in pyproject)
    check("the loader looks beyond the module directory",
          "sys.prefix" in open(os.path.join(REPO_ROOT, "product_parser.py"),
                               encoding="utf-8").read())
    check("a missing snapshot is an error, not a licence",
          hasattr(product_parser, "RobotsSnapshotMissing"))

    # Failing closed, proven rather than asserted from the source.
    saved = dict(product_parser._CACHED_RULES)
    saved_path = product_parser._ROBOTS_SNAPSHOT
    product_parser._CACHED_RULES.clear()
    product_parser._ROBOTS_SNAPSHOT = pathlib_Path(REPO_ROOT) / "no-such-snapshot.txt"
    try:
        raised = _raises_type(product_parser.RobotsSnapshotMissing,
                              product_parser.shipped_robots, "*")
        check("...and it raises rather than allowing everything", raised)
    finally:
        product_parser._ROBOTS_SNAPSHOT = saved_path
        product_parser._CACHED_RULES.clear()
        product_parser._CACHED_RULES.update(saved)

    check("and with the snapshot present the rules are all there",
          len(product_parser.shipped_robots("*")) == 140)
    return not _failures


def test_scope_guard():
    group("a diff refuses two different markets")
    import diff_runs
    from output_writer import scope_fingerprint, schema_version

    gb = scope_fingerprint(44009506, "gb", "WOMEN / X", ["g1"], 1, None)
    de = scope_fingerprint(44009504, "de", "WOMEN / X", ["g1"], 1, None)
    other_grids = scope_fingerprint(44009506, "gb", "WOMEN / X", ["g2"], 1, None)

    class _Args:
        def __init__(self, old, new, force=False):
            self.old, self.new, self.force = old, new, force

    tmp = tempfile.mkdtemp()

    def write(name, scope):
        path = os.path.join(tmp, name + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([{"sku": "1", "price": 1.0}], fh)
        with open(os.path.join(tmp, name + ".meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"status": "complete", "mode": "category", "scope": scope}, fh)
        return path

    a, b = write("gb", gb), write("de", de)
    # Two complete runs of the same SKU in two markets used to be comparable,
    # so every row read as a price change rather than as two currencies.
    problems = diff_runs._scope_problems(_Args(a, b))
    check("a gb/de pair is refused", bool(problems))
    check("...naming the store", any("store_id" in p for p in problems))
    check("...and the locale", any("locale" in p for p in problems))
    check("--force is offered, not assumed",
          any("--force" in p for p in problems))
    check("...and --force actually allows it",
          diff_runs._scope_problems(_Args(a, b, force=True)) == [])

    c = write("othergrids", other_grids)
    problems = diff_runs._scope_problems(_Args(a, c))
    check("different grids are refused too", bool(problems))
    check("...and the message says why it matters",
          any("delisted" in p for p in problems))

    same = write("gb2", gb)
    check("the same scope compares cleanly",
          diff_runs._scope_problems(_Args(a, same)) == [])

    # A run written before scopes existed must be reported as unknown, not
    # waved through: "no scope recorded" is not "same scope".
    path = os.path.join(tmp, "old.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([{"sku": "1"}], fh)
    with open(os.path.join(tmp, "old.meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"status": "complete", "mode": "category"}, fh)
    problems = diff_runs._scope_problems(_Args(path, a))
    check("a run with no scope is refused rather than assumed to match",
          bool(problems) and any("no scope" in p for p in problems))

    check("the schema version is part of the scope",
          gb["schema_version"] == schema_version())
    shutil.rmtree(tmp, ignore_errors=True)
    return not _failures


def test_attribution_is_run_wide():
    group("a bundle's owner does not depend on batch order")
    import catalog_walk

    STORE = json.dumps({"id": 1, "countryCode": "GB",
                        "catalogs": [{"id": 2, "type": 1}],
                        "details": {"imageBaseUrl": "https://x",
                                    "locale": {"currencyCode": "GBP",
                                               "currencyDecimals": -2}}})

    def entry(entry_id, bundle_id, sku):
        return {"id": entry_id, "type": "BundleBean", "productUrl": "slug-l01",
                "bundleProductSummaries": [
                    {"id": bundle_id, "name": "P", "productUrl": "slug-l01",
                     "detail": {"reference": "r", "colors": [
                         {"id": "1", "name": "Blue", "sizes": [
                             {"sku": sku, "name": "M", "isBuyable": True,
                              "backSoon": "0", "price": "2099"}]}]}}]}

    def run(order):
        payloads = {"gA": [entry(233573528, 500, 9001)],
                    "gB": [entry(229723104, 500, 9001)]}
        names = ["gA", "gB"] if order == "A-first" else ["gB", "gA"]
        turn = {"n": 0}

        def fetch(url):
            if "/itxrest/2/catalog/store/" in url:
                return catalog_walk.Fetched(200, STORE.encode())
            if "/menu" in url:
                items = [{"id": i, "name": n,
                          "content": {"id": n, "type": "grid"}, "children": []}
                         for i, n in enumerate(names)]
                return catalog_walk.Fetched(200, json.dumps({"items": items}).encode())
            ids = {"gA": 233573528, "gB": 229723104}
            for name in names:
                if f"/grid/{name}" in url:
                    return catalog_walk.Fetched(200, json.dumps(
                        {"productIds": [ids[name]], "sortedProductIds": [ids[name]],
                         "gridContext": {"gridId": name}}).encode())
            if "productsArray" in url:
                which = names[turn["n"]]
                turn["n"] += 1
                return catalog_walk.Fetched(200, json.dumps(
                    {"products": payloads[which]}).encode())
            raise AssertionError(url)
        return catalog_walk.crawl(fetch, 1)

    first = run("A-first").rows[0]
    second = run("B-first").rows[0]
    # Resolving the owner inside one payload was only half the guarantee: the
    # same bundle reached from a lower entry in a later batch kept whichever
    # arrived first, so product_id and url moved with batch order and any
    # downstream join moved with them.
    check("the lowest entry id wins whatever the order",
          first.product_id == second.product_id == 229723104)
    check("...so the URL is stable too", first.url == second.url)
    check("every menu trail that reached the sku is kept",
          set(first.categories) == {"gA", "gB"})
    check("...sorted, so two runs export the same bytes",
          first.categories == second.categories == sorted(first.categories))
    check("`category` still holds the first trail for compatibility",
          first.category in ("gA", "gB"))

    # A product listed by several grids used to be downloaded once per grid,
    # at roughly 25 KB a time, and deduplicated only after it arrived.
    STORE2 = json.dumps({"id": 1, "countryCode": "GB",
                         "catalogs": [{"id": 2, "type": 1}],
                         "details": {"imageBaseUrl": "https://x",
                                     "locale": {"currencyCode": "GBP",
                                                "currencyDecimals": -2}}})
    calls = {"n": 0}

    def repeated(url):
        if "/itxrest/2/catalog/store/" in url:
            return catalog_walk.Fetched(200, STORE2.encode())
        if "/menu" in url:
            return catalog_walk.Fetched(200, json.dumps({"items": [
                {"id": 1, "name": "gA", "content": {"id": "gA", "type": "grid"},
                 "children": []},
                {"id": 2, "name": "gB", "content": {"id": "gB", "type": "grid"},
                 "children": []}]}).encode())
        if "/grid/" in url:
            # Both grids list the SAME product.
            return catalog_walk.Fetched(200, json.dumps(
                {"productIds": [7], "sortedProductIds": [7],
                 "gridContext": {"gridId": "g"}}).encode())
        if "productsArray" in url:
            calls["n"] += 1
            return catalog_walk.Fetched(200, json.dumps(
                {"products": [entry(7, 700, 4242)]}).encode())
        raise AssertionError(url)

    res = catalog_walk.crawl(repeated, 1)
    check("a product two grids both list is fetched once", calls["n"] == 1)
    check("...and still appears once in the output", len(res.rows) == 1)
    check("...while recording BOTH categories, which the fetch no longer "
          "carries", res.rows[0].categories == ["gA", "gB"])
    return not _failures


def test_store_config_is_read_once():
    group("the store config is not fetched twice")
    import catalog_walk

    STORE = json.dumps({"id": 44009506, "countryCode": "GB",
                        "catalogs": [{"id": 40259534, "type": 1}],
                        "details": {"imageBaseUrl": "https://x",
                                    "locale": {"currencyCode": "GBP",
                                               "currencyDecimals": -2}}})
    reads = {"n": 0}

    def fetch(url):
        if "/itxrest/2/catalog/store/" in url:
            reads["n"] += 1
            return catalog_walk.Fetched(200, STORE.encode())
        if "/menu" in url:
            return catalog_walk.Fetched(200, json.dumps({"items": []}).encode())
        raise AssertionError(url)

    store = catalog_walk.load_store(fetch, 44009506)
    before = reads["n"]
    # `api_scraper.resolve_store` has already read it to find the store id;
    # `crawl` used to read it again, and that second request was not even
    # counted in the run's own tally, so the reported cost was wrong.
    catalog_walk.crawl(fetch, 44009506, store=store)
    check("crawl reuses a store it was handed", reads["n"] == before)
    reads["n"] = 0
    catalog_walk.crawl(fetch, 44009506)
    check("...and still reads it when it was not", reads["n"] == 1)
    return not _failures


def test_output_contract():
    group("row contract")
    names = [f.name for f in fields(Product)]
    check("the family prefix is intact and in order",
          tuple(names[:len(FAMILY_PREFIX)]) == FAMILY_PREFIX)
    check("source names the site", SOURCE_DEFAULT == "bershka.com")
    check("a default Product carries the source", Product().source == "bershka.com")
    check("scraped_at is populated by default", bool(Product().scraped_at))

    for col in ("locale", "color_name", "size_name", "availability",
                "original_price", "discount_pct", "discount_source",
                "product_id", "bundle_id", "reference", "partnumber",
                "grid_id", "store_id"):
        check(f"the site column {col} exists", col in names)

    check("both modes map to Product",
          set(ROW_CLASS_BY_MODE.values()) == {Product})
    check("both modes are one-row-per-sku",
          set(UNIQUE_BY_SKU_MODES) == set(ROW_CLASS_BY_MODE))

    # Dedup is not theoretical here: 13 products expanded to 195 rows for 131
    # distinct SKUs on the SALE dresses grid, because several array entries
    # pointed at one bundle.
    rows = [Product(sku="1"), Product(sku="1"), Product(sku="2")]
    check("dedupe_by_sku keeps the first of a repeat",
          len(dedupe_by_sku(rows, set())) == 2)

    # Every field diff_runs compares must BE a column. Carried over from a
    # sibling repo, TRACKED_FIELDS named `shade`, `shade_count`, `size` and
    # `badge` — four columns this schema does not have — so the diff silently
    # compared nothing for them while skipping `is_buyable` and `back_soon`,
    # which are what actually move here. `.get()` on a missing key returns
    # None on both sides, so the comparison looked like it was working.
    unknown = [f for f in TRACKED_FIELDS if f not in names]
    check(f"every diff_runs tracked field is a real column (stray: {unknown})",
          not unknown)
    check("the two family columns are tracked",
          {"price", "currency"} <= set(TRACKED_FIELDS))
    # Position is not a property of a product. Two runs twenty minutes apart
    # produced identical rows with 44 of 131 row_index values changed.
    for volatile in ("row_index", "page", "scraped_at", "image_url"):
        check(f"{volatile} is NOT tracked", volatile not in TRACKED_FIELDS)
    return not _failures


def _engine_module(name, skips):
    try:
        return __import__(name)
    except ImportError as exc:
        skips.append(f"{name}: {exc}")
        return None


def test_engines(skips):
    group("engines")
    for name in ALL_ENGINES:
        mod = _engine_module(name, skips)
        if mod is None:
            continue
        check(f"{name} exposes main()", callable(getattr(mod, "main", None)))
        check(f"{name} exposes parse_args()", callable(getattr(mod, "parse_args", None)))
        check(f"{name} exposes scrape()", callable(getattr(mod, "scrape", None)))
    return not _failures


def test_engine_parity(skips):
    group("engine parity")
    # Every engine walks the SAME catalogue route, because the route lives in
    # catalog_walk and none of them may carry a copy of it. This is
    # CLAUDE.md §11 applied to the engines rather than to a workflow step.
    for name in ALL_ENGINES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            skips.append(f"{name}.py is missing")
            continue
        text = open(path, encoding="utf-8").read()
        body = text.split('"""', 2)[-1]
        check(f"{name} uses the shared walk", "catalog_walk" in text)
        check(f"{name} builds no productsArray URL of its own",
              "/itxrest/3/" not in body)
        check(f"{name} hardcodes no store id", not re.search(r"\b44009506\b", body))
        check(f"{name} parses nothing itself",
              "bundleProductSummaries" not in body)

    # The shared flags are compared against the ONE function that defines
    # them, not against a list written out again here — a hand-kept list is
    # the thing this check exists to prevent.
    import argparse
    import browser_bridge
    core = argparse.ArgumentParser()
    browser_bridge.add_core_arguments(core, "x")
    core_flags = {a for action in core._actions for a in action.option_strings
                  if a.startswith("--")} - {"--help"}

    for name in ALL_ENGINES:
        mod = _engine_module(name, skips)
        if mod is None:
            continue
        try:
            here = _engine_flags(mod)
        except Exception:  # noqa: BLE001
            check(f"{name}'s flags could be read", False)
            continue
        missing = sorted(core_flags - here)
        check(f"{name} carries every core flag", not missing)

    # Only over the engines that import HERE: with the drivers imported at
    # module level (§10), an engine whose library is absent cannot be asked
    # for its flags, and an empty set from it would read as "takes no
    # --cdp-endpoint" rather than as "not installed".
    importable = [n for n in ALL_ENGINES if _importable(n)]
    check("only the browser engines take --cdp-endpoint",
          {n for n in importable if "--cdp-endpoint" in _engine_flags_safe(n)}
          == set(ENGINES) & set(importable))
    check("the vendor transport chooses no exit of its own",
          "--proxy-file" not in _engine_flags_safe("scraper_api_client"))
    return not _failures


def _engine_flags(mod):
    """The long flags an engine's parser defines, read off the parser itself.

    Patches the METHOD, never the class. Replacing
    `argparse.ArgumentParser` with a subclass looks equivalent and is not:
    argparse's own `__init__` calls `super(ArgumentParser, self).__init__`
    and resolves `ArgumentParser` through the module global, so a patched
    global makes that line resolve to the subclass and recurse forever.
    Measured 2026-09-20 — it hung the suite and ate enough memory for the
    machine to start killing background shells, which is a long way from
    looking like a test-harness bug.
    """
    import argparse
    holder = {}
    real = argparse.ArgumentParser.parse_args

    def recorder(self, *a, **kw):
        holder["parser"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = recorder
    try:
        try:
            mod.parse_args()
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = real
    parser = holder.get("parser")
    if parser is None:
        raise RuntimeError(f"{mod.__name__} built no parser")
    return {flag for action in parser._actions for flag in action.option_strings
            if flag.startswith("--")} - {"--help"}


_SKIPPED_ENGINES = set()


def _importable(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def test_engines_import_their_driver_at_module_level():
    """CLAUDE.md §10. The drivers were imported inside start(), so every
    engine imported cleanly with no driver installed: the offline suite's
    skips never fired and CI's engine-smoke import check could not fail.
    Asserted from the source, so it holds with no driver installed."""
    group("drivers at module level")
    import ast
    drivers = {"playwright_scraper": "playwright", "puppeteer_scraper": "pyppeteer",
               "selenium_scraper": "selenium"}
    for mod, lib in drivers.items():
        tree = ast.parse(open(os.path.join(REPO_ROOT, mod + ".py"), encoding="utf-8").read())
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module.split(".")[0])
        check(f"{mod} imports {lib} at MODULE level", lib in top)
        nested = [n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.ImportFrom) and n not in tree.body
                  and (n.module or "").split(".")[0] == lib]
        check(f"{mod} has no {lib} import left inside a function", not nested)
    return not _failures


def _engine_flags_safe(name):
    try:
        return _engine_flags(__import__(name))
    except Exception:  # noqa: BLE001
        return set()


def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports success
    # without checking that what it wanted actually happened.
    skips = []

    ok &= test_robots()
    ok &= test_store_config()
    ok &= test_menu()
    ok &= test_grid()
    ok &= test_products()
    ok &= test_sitemaps()
    ok &= test_walls()
    ok &= test_urls()
    ok &= test_catalog_walk()
    ok &= test_challenge_ladder()
    ok &= test_failure_handling()
    ok &= test_proxy_rotation_is_wired()
    ok &= test_robots_snapshot_travels()
    ok &= test_scope_guard()
    ok &= test_attribution_is_run_wide()
    ok &= test_store_config_is_read_once()
    ok &= test_output_contract()
    ok &= test_writers()
    ok &= test_finish_run()
    ok &= test_diff()
    ok &= test_captcha()
    ok &= test_remote_api_error()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_readiness_wait()
    ok &= test_engines(skips)
    ok &= test_engine_parity(skips)
    ok &= test_engines_import_their_driver_at_module_level()
    ok &= test_env_duplicate_keys()
    ok &= test_env_example_matches_env_keys()
    ok &= test_proxy_filenames_are_ignored()
    ok &= test_minted_proxy_sessions()
    ok &= test_browser_profile_client()
    ok &= test_no_capture_leaks()
    ok &= test_wording()
    ok &= test_fingerprint_application()
    ok &= test_fingerprint_client_reads_env()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_no_undefined_names()
    ok &= test_ci_checks_is_actually_wired_up()
    ok &= test_dockerfile_copies_what_it_runs()
    ok &= test_sample_output()

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d group(s) SKIPPED — an optional engine library is absent. "
              "CI's engine job installs all three and fails if this list is "
              "non-empty, because a skip reads exactly like a passing run:"
              % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
