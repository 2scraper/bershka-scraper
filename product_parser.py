#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
product_parser.py
-----------------
Everything this repo knows about bershka.com, as pure functions over bytes
that somebody else fetched. No network, no browser, no global state — so the
whole of it is covered by `smoke_test.py` offline, and the four engines share
one set of rules instead of four drifting copies.

The one thing to understand before reading further
==================================================
**A rendered Bershka listing page contains no products.** Measured
2026-09-19 on `/gb/women.html`, fetched through a paid exit and fully
rendered, 938,454 bytes:

    application/ld+json blocks        0
    product id attributes             0
    product grid classes              0
    product URL shapes in the markup  0

The grid, the prices and the sizes arrive afterwards as JSON from Inditex's
own catalogue API under `/itxrest/`. So this file has no HTML selectors for
products and no tile parser. It parses JSON payloads, and the engines differ
only in how they get those payloads.

The route through the API
-------------------------
    GET /itxrest/2/catalog/store/{store}          store config: currency, catalogId
    GET /api/storefront/1/stores/{store}/menu     the category tree -> grid ids
    GET /itxrest/4/catalog/store/{store}/grid/{gridId}      -> productIds
    GET /itxrest/3/catalog/store/{store}/{catalog}/productsArray?productIds=...

Measured 2026-09-19 against store 44009506 (`gb`, catalog 40259534): the menu
carries 675 content nodes — 585 of type `grid`, 57 `redirection`, 33
`marketing` — and the four top-level entries are WOMEN, MEN, BY INFLUENCERS
and FOOTER.

What refuses you, and what does not
-----------------------------------
Two different walls, and conflating them sends a run into the wrong retry:

* **The edge refusal.** From a Moscow residential address every URL on this
  host — including `/robots.txt` — answered HTTP 403 with a 195-byte
  `Service Unavailable` body carrying `x-reference-error` and
  `akamai-cache-status: Error from child`, while setting `ITXSESSIONID` and
  `BSKSESSION` even as it refused. No amount of waiting or solving changes
  it; it is about the exit address. `is_edge_refusal()` names it.
* **The behavioural interstitial.** A small self-clearing shim that does
  arithmetic and POSTs to `/_sec/verify?provider=interstitial`. It is NOT
  what the 403 above is: the 403 body contains neither `bm-verify` nor
  `/_sec/verify`, so `is_interstitial()` correctly returns False for it.
  This repo does not implement the headless handshake; the browser engines
  wait it out through `page_flow.wait_out_self_clearing_challenge()`.

Through a proxy exit, plain HTTPS with no browser and no cookies answered
200 on both `/robots.txt` and the catalogue API, twice, once with TLS
impersonation disabled (2026-09-19). That measurement is why `api_scraper.py`
is the primary engine here and the three browser engines are parity.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlsplit

from output_writer import Product

logger = logging.getLogger("product_parser")

BASE = "https://www.bershka.com"

# Store ids are per country and are published by the storefront itself, in a
# `var inditex={...}` blob at the end of the document. 44009506 is `gb`, read
# from `iStoreId` on 2026-09-19 along with `iCatalogId` 40259534. The other 26
# ids the page lists belong to other countries and are NOT mapped here,
# because nobody has fetched a store config for them: a map with 27 entries
# would imply 27 measurements that do not exist. `store_id_from_page()` reads
# the real one out of any storefront HTML.
KNOWN_STORES: Dict[str, Dict[str, int]] = {
    "gb": {"store_id": 44009506, "catalog_id": 40259534},
    # Measured 2026-09-20 by reading the `de` storefront after answering its
    # interstitial: store 44009504, catalogue 40259546, EUR. Added because it
    # was measured, not because the pattern looked safe to extend.
    "de": {"store_id": 44009504, "catalog_id": 40259546},
}
DEFAULT_LOCALE = "gb"

# The trailing `;` is optional: the live page omits it and a minifier may
# not. Anchoring on `</script>` keeps the lazy `.*?` from running off the end
# of a 937 KB document.
_INDITEX_BLOB_RE = re.compile(r"var\s+inditex\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)


# ---------------------------------------------------------------------------
# Bot walls
# ---------------------------------------------------------------------------
# Kept as two separate vocabularies on purpose — see the module docstring.
# `x-reference-error` is NOT in this list, and that omission is the whole
# lesson. Measured 2026-09-20 over ten live runs: Akamai sends that header on
# the self-clearing interstitial too — a 2,142-byte HTTP 200 carrying
# `bm-verify` and the arithmetic shim — so keying the refusal on it made a
# challenge look like an address ban. Since `is_edge_refusal` is checked
# FIRST and raises, the challenge ladder would never have run: every real
# interstitial would have been reported as "change your exit", which is the
# one remedy that does not apply.
#
# What actually separates them is the cache status and the body:
#
#     refusal        HTTP 403, 195 b, "Service Unavailable"
#                    Akamai-Cache-Status: Error from child
#     interstitial   HTTP 200, ~2,142 b, bm-verify + /_sec/verify
#                    Akamai-Cache-Status: NotCacheable from child
EDGE_REFUSAL_MARKERS = (
    "akamai-cache-status: error from child",
    "http error 403. the service is unavailable",
)
# The family contract is {vendor: markers}, and the two Akamai entries are
# kept apart on purpose: `akamai-interstitial` clears itself, `akamai-edge`
# never does. Folding them into one vendor would make a run wait out a wall
# that is not waiting for anything.
BOT_CHALLENGE_MARKERS = {
    "akamai-interstitial": ("bm-verify", "/_sec/verify"),
    "akamai-sec-cpt": ("sec-cpt", "_sec_cpt"),
    "akamai-edge": ("x-reference-error", "akamai-cache-status: error from child"),
}

# Only the first of those reloads itself once passed. `sec-cpt` is included
# because a sibling repo measured one live on this edge vendor and waits it
# out; this repo has not met one on bershka.com, and says so rather than
# implying the wait is tuned for a wall nobody here has seen.
SELF_CLEARING_CHALLENGE_MARKERS = ("bm-verify", "/_sec/verify", "sec-cpt")


def is_edge_refusal(status: Optional[int], body: str = "", headers: Optional[Dict[str, str]] = None) -> bool:
    """True for Akamai's flat refusal of the exit address.

    Deliberately not keyed on the status alone: a 403 from the application is
    a different thing from a 403 served by the edge before the application
    ever saw the request, and only the second one is fixed by changing exit.
    """
    text = body or ""
    # A challenge is never a refusal, whatever the headers say. This test
    # comes first because the two share headers; see EDGE_REFUSAL_MARKERS.
    if is_self_clearing_challenge(text):
        return False

    blob = text.lower()
    for k, v in (headers or {}).items():
        blob += f"\n{k.lower()}: {str(v).lower()}"
    if any(m in blob for m in EDGE_REFUSAL_MARKERS):
        return True
    return status == 403 and "service unavailable" in blob


# The interstitial's own handshake, as pure parsing. Measured on a real one
# served 2026-09-20 (HTTP 200, 2,142 bytes):
#
#   <meta http-equiv="refresh" content="5; URL='/gb/?bm-verify=AAQ…'" />
#   <script> var i = 1789910678; var j = i + Number("3886" + "11036"); </script>
#   xhr.send(JSON.stringify({"bm-verify":"AAQ…","pow":j}))
#
# The operand is split across a string concatenation on purpose, so a regex
# looking for one number literal finds nothing; both halves are captured and
# JOINED (not added) before being added to `i`.
#
# Note the two tokens in the document are NOT the same string: the meta
# refresh carries a base64url form and the POST body a base64 one. The POST
# is what has to be answered, so that is the one read here.
INTERSTITIAL_VERIFY_PATH = "/_sec/verify?provider=interstitial"

_POW_RE = re.compile(
    r'var\s+i\s*=\s*(\d+)\s*;\s*var\s+j\s*=\s*i\s*\+\s*Number\(\s*"(\d+)"\s*\+\s*"(\d+)"\s*\)')
_BM_TOKEN_RE = re.compile(r'"bm-verify"\s*:\s*"([^"]+)"')


def parse_interstitial(html: str) -> Optional[Dict[str, Any]]:
    """`{"bm-verify": token, "pow": n}` — the exact body the shim's own
    script POSTs — or None if this is not one, or its shape changed."""
    if not is_self_clearing_challenge(html or ""):
        return None
    token = _BM_TOKEN_RE.search(html)
    pow_match = _POW_RE.search(html)
    if not token or not pow_match:
        logger.warning("interstitial detected but its token or arithmetic "
                       "could not be read — the shim's shape changed")
        return None
    base, hi, lo = pow_match.groups()
    return {"bm-verify": token.group(1), "pow": int(base) + int(hi + lo)}


def is_interstitial(html: str) -> bool:
    """True for the self-clearing arithmetic shim, and not for the 403.

    Narrow by the same reasoning a sibling repo arrived at: `bm-verify`
    alone also appears in a healthy storefront's inline analytics, so the
    match requires the verify endpoint too and a document small enough to be
    a shim rather than a page.
    """
    if not html or len(html) > 12000:
        return False
    return "bm-verify" in html and "/_sec/verify" in html


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------
# This site's robots.txt is the reason CLAUDE.md §7 now opens with "read
# robots before designing pagination", and it does not reward a substring
# check. Measured 2026-09-19 on the 5,927-byte file shipped beside this
# module as `robots.snapshot.txt`:
#
#   * 31 user-agent groups.
#   * THREE separate `User-agent: *` groups, at lines 79, 123 and 192,
#     carrying 28, 62 and 50 rules. A parser that takes the first matching
#     group and stops keeps 28 rules and drops 112 — including
#     `Disallow: /ru/` and fifty per-country product-page bans of the shape
#     `/kz/*-c0*p*.html`. RFC 9309 §2.2.1 says records with the same product
#     of user-agent lines are merged, so this parser merges them.
#   * A bare `Disallow: /itxrest` appears TWICE — but in the `proximic`
#     group (line 76) and the `Yandex` group (line 112, which also carries a
#     blanket `Disallow: /`). For `*` the only `/itxrest` rule is line 106,
#     `Disallow: /itxrest/1/marketing/`. The catalogue API is therefore
#     allowed to us and forbidden to those two names, which is a fact about
#     what this scraper may call ITSELF, not only about what it may fetch.
#   * One `Allow` (`/*/q/*index=1*`) that only means anything if longest-match
#     precedence is implemented, because `Disallow: /*/q/*` covers it.
#
# Search, filters and sorting are disallowed for `*` (`?query=`, `?searchTerm=`,
# `?search=`, `price=`, `discount=`, `size=`, `sort=`). Nothing in this repo
# builds such a URL.

_ROBOTS_SNAPSHOT = Path(__file__).resolve().parent / "robots.snapshot.txt"


@dataclass
class RobotsRules:
    """Merged rules for one user-agent, in the order they were read."""
    agent: str = "*"
    rules: List[Tuple[bool, str]] = field(default_factory=list)   # (allow?, pattern)
    groups_merged: int = 0
    sitemaps: List[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rules)


def _pattern_to_re(pattern: str) -> re.Pattern:
    """`*` is any run of characters, a trailing `$` anchors the end."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    out = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile("^" + out + ("$" if anchored else ""))


def parse_robots(text: str, agent: str = "*") -> RobotsRules:
    """Parse robots.txt, merging every group whose user-agent matches.

    `agent` is matched case-insensitively and exactly (plus `*`), which is
    what RFC 9309 asks for. Sitemap lines are collected regardless of group,
    since they are not part of any.
    """
    out = RobotsRules(agent=agent)
    wanted = agent.lower()
    agents: List[str] = []
    in_group = False           # have we seen a rule since the last agent line?
    matching = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "sitemap":
            if value:
                out.sitemaps.append(value)
            continue
        if key == "user-agent":
            if in_group:           # a new group starts here
                agents = []
                in_group = False
            agents.append(value.lower())
            matching = any(a == wanted or a == "*" for a in agents)
            continue
        if key in ("allow", "disallow"):
            if agents:
                in_group = True
            if matching and value:
                out.rules.append((key == "allow", value))
    # Count how many distinct groups contributed, for the smoke test to pin.
    out.groups_merged = _count_matching_groups(text, wanted)
    return out


def _count_matching_groups(text: str, wanted: str) -> int:
    groups = 0
    agents: List[str] = []
    in_group = False
    counted = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().lower()
        if key == "user-agent":
            if in_group:
                agents = []
                in_group = False
                counted = False
            agents.append(value)
            continue
        if key in ("allow", "disallow") and agents:
            in_group = True
            if not counted and any(a == wanted or a == "*" for a in agents):
                groups += 1
                counted = True
    return groups


_CACHED_RULES: Dict[str, RobotsRules] = {}


def shipped_robots(agent: str = "*") -> RobotsRules:
    """The snapshot that travels with the repo.

    A snapshot is a record of what was true on the day it was taken, not a
    licence: `is_robots_allowed()` takes live rules when an engine has them.
    """
    if agent not in _CACHED_RULES:
        text = _ROBOTS_SNAPSHOT.read_text(encoding="utf-8") if _ROBOTS_SNAPSHOT.exists() else ""
        _CACHED_RULES[agent] = parse_robots(text, agent)
    return _CACHED_RULES[agent]


def is_robots_allowed(url: str, rules: Optional[RobotsRules] = None) -> bool:
    """Whether robots.txt permits fetching `url`.

    Longest matching pattern wins; `Allow` wins a tie. Matched against
    path+query, because robots patterns are path patterns and the host would
    otherwise make `/*sort=*` match nothing.
    """
    rules = rules if rules is not None else shipped_robots()
    parts = urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    best_len, best_allow = -1, True
    for allow, pattern in rules.rules:
        if _pattern_to_re(pattern).match(target):
            plen = len(pattern)
            if plen > best_len or (plen == best_len and allow):
                best_len, best_allow = plen, allow
    return best_allow


def robots_url() -> str:
    return f"{BASE}/robots.txt"


# ---------------------------------------------------------------------------
# Canonical URLs
# ---------------------------------------------------------------------------

def store_config_url(store_id: int, language_id: int = -1) -> str:
    return f"{BASE}/itxrest/2/catalog/store/{store_id}?languageId={language_id}&appId=1"


def menu_url(store_id: int) -> str:
    return f"{BASE}/api/storefront/1/stores/{store_id}/menu"


def grid_url(store_id: int, grid_id: str, language_id: int = -1) -> str:
    return (f"{BASE}/itxrest/4/catalog/store/{store_id}/grid/{quote(str(grid_id))}"
            f"?languageId={language_id}&appId=1")


# 40 ids per call is what the prior generation used and what this repo
# measured against: one call for 40 ids returned 1,031,388 bytes on
# 2026-09-19. The payload is large because every product carries its full
# colour and size tree, so raising this trades one round trip for megabytes.
PRODUCTS_PER_CALL = 40


def products_array_url(store_id: int, catalog_id: int, product_ids: Sequence[int],
                       language_id: int = -1) -> str:
    ids = ",".join(str(int(i)) for i in product_ids)
    return (f"{BASE}/itxrest/3/catalog/store/{store_id}/{catalog_id}/productsArray"
            f"?productIds={ids}&languageId={language_id}&appId=1")


def product_url(locale: str, product_id: int, slug: str = "") -> str:
    """The canonical product page.

    Shape measured 2026-09-19: `/gb/{slug}-c0p{productId}.html`. The API's own
    `productUrl` field carries an extra `-l{reference}` segment
    (`lace-strap-midi-dress-l01218714`); fetched, it 302s to the form without
    it, so this builds the canonical target rather than the redirecting one.
    A missing slug is not invented — `-c0p{id}.html` is served on its own.
    """
    stem = f"{slug}-" if slug else ""
    return f"{BASE}/{locale}/{stem}c0p{int(product_id)}.html"


def canonical_slug(api_product_url: Optional[str]) -> str:
    """Strip the `-l{reference}` tail the API appends to its slug."""
    if not api_product_url:
        return ""
    return re.sub(r"-l\d+$", "", api_product_url.strip("/"))


SITEMAP_INDEX_URL = f"{BASE}/sitemap/sitemap_indice.xml.gz"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# There is nothing to page. A grid endpoint returns its whole product id list
# in one response — 14 ids for the SALE dresses grid, 45 for `WOMEN / Xenia /
# Xeniadown`, both measured 2026-09-19 — and carries no cursor, no offset and
# no total-pages field. The storefront's own paging parameters (`sort=`,
# `price=`, `size=`) are robots-disallowed for `*` anyway, so there is no
# allowed URL to page with even if the API wanted one.
#
# `page_url` therefore returns its input unchanged, exactly as the sibling
# repo's does for a different reason, and an engine asked for `--pages 5`
# stops after one fetch with `pagination_exhausted`. Breadth on this site
# comes from walking the menu's 585 grids, not from paging one of them.

def page_url(url: str, page: int) -> str:
    return url


# ---------------------------------------------------------------------------
# Store config
# ---------------------------------------------------------------------------

@dataclass
class StoreConfig:
    store_id: Optional[int] = None
    catalog_id: Optional[int] = None
    country_code: Optional[str] = None
    currency_code: Optional[str] = None
    currency_symbol: Optional[str] = None
    # `currencyDecimals` is negative in the payload (-2). It is the exponent
    # to apply, not a count to trust the sign of: 2099 -> 20.99.
    currency_decimals: Optional[int] = None
    # `details.imageBaseUrl`, measured as "https://static.bershka.net/4/photos2".
    # Taken from the payload rather than hardcoded because it is the only
    # place the serving host and its `/4/photos2` prefix are stated together.
    image_base_url: Optional[str] = None


def parse_store_config(payload: Any) -> StoreConfig:
    """Read currency and catalog id out of `/itxrest/2/catalog/store/{id}`.

    There is deliberately no `locale` on StoreConfig. `details.locale` sounds
    like it holds one and does not: its twelve keys are all currency
    formatting (`currencyCode`, `currencyDecimals`, `currencySymbol`,
    separators). The only locale-shaped string in the GB payload is
    `supportedLanguages[0].localeName`, and it reads `"en_US"` for store
    44009506 — so reporting it as this store's locale would ship a wrong
    value that looks right.

    The catalogue list holds the same id five times under different `type`
    values (measured: 40259534 four times as types 1/2/5/7, plus 40259584
    `CATALOGO_BSK_IOP` as type 100). Type 1 is the storefront catalogue and
    is what the productsArray endpoint accepts; the IOP one is not.
    """
    data = _as_json(payload)
    if not isinstance(data, dict):
        return StoreConfig()
    loc = ((data.get("details") or {}).get("locale") or {})
    catalog_id = None
    for entry in data.get("catalogs") or []:
        if isinstance(entry, dict) and entry.get("type") == 1:
            catalog_id = entry.get("id")
            break
    decimals = loc.get("currencyDecimals")
    return StoreConfig(
        store_id=data.get("id"),
        catalog_id=catalog_id,
        country_code=data.get("countryCode"),
        currency_code=loc.get("currencyCode"),
        currency_symbol=loc.get("currencySymbol"),
        currency_decimals=int(decimals) if isinstance(decimals, (int, float)) else None,
        image_base_url=(data.get("details") or {}).get("imageBaseUrl"),
    )


def money(raw: Any, decimals: Optional[int]) -> Optional[float]:
    """Scale the API's minor-unit price string.

    Returns None rather than 0.0 for a missing price: a row with no price is
    a fact, and 0.0 would be a claim that the thing is free.
    """
    if raw in (None, "", "null"):
        return None
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    if decimals is None:
        return None
    return round(value * (10 ** -abs(int(decimals))), 2)


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

@dataclass
class MenuGrid:
    grid_id: str
    name: str
    trail: str          # "WOMEN / SALE / Bershka / Dresses and jumpsuits"
    menu_id: Optional[int] = None
    section: Optional[str] = None


def walk_menu(payload: Any) -> List[MenuGrid]:
    """Every node whose content is a grid, depth-first, in menu order.

    `marketing` nodes are dropped rather than followed: `/itxrest/1/marketing/`
    is the one catalogue path robots disallows for `*`, and there were 33 of
    them in the GB menu on 2026-09-19. `redirection` nodes (57) point at other
    menu entries, not at products, so they are dropped too.
    """
    data = _as_json(payload)
    items = (data or {}).get("items") if isinstance(data, dict) else None
    out: List[MenuGrid] = []

    def walk(node: Dict[str, Any], trail: List[str]) -> None:
        name = node.get("name") or ""
        here = trail + [name]
        content = node.get("content") or {}
        if content.get("type") == "grid" and content.get("id"):
            out.append(MenuGrid(
                grid_id=str(content["id"]),
                name=name,
                trail=" / ".join(p for p in here if p),
                menu_id=node.get("id"),
                section=here[0] if here else None,
            ))
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child, here)

    for item in items or []:
        if isinstance(item, dict):
            walk(item, [])
    return out


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------

@dataclass
class Grid:
    grid_id: Optional[str] = None
    product_ids: List[int] = field(default_factory=list)
    sorted_product_ids: List[int] = field(default_factory=list)
    section_names: List[str] = field(default_factory=list)
    filter_count: int = 0


def parse_grid(payload: Any) -> Grid:
    """Product ids for one category.

    `productIds` and `sortedProductIds` do NOT have to agree, and the
    difference is not noise: the SALE dresses grid served 14 of the first and
    13 of the second on 2026-09-19, and the missing id was the one whose
    productsArray entry came back as an error object rather than a product.
    This returns both lists untouched and lets the caller decide; the engines
    request `productIds`, because asking for the id that fails is how the
    failure stays visible.
    """
    data = _as_json(payload)
    if not isinstance(data, dict):
        return Grid()
    ctx = data.get("gridContext") or {}
    return Grid(
        grid_id=ctx.get("gridId"),
        product_ids=[int(i) for i in (data.get("productIds") or []) if _is_int(i)],
        sorted_product_ids=[int(i) for i in (data.get("sortedProductIds") or []) if _is_int(i)],
        section_names=list(ctx.get("sectionNames") or []),
        filter_count=len(data.get("filters") or []),
    )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
# An entry in `products` is not guaranteed to be a product. A request for 14
# ids came back HTTP 200 with 13 products and this, in the array, at the
# position of the fourteenth:
#
#     {"description": "Item not found", "key": "_ERR_PRODUCT_NOT_FOUND",
#      "commitMark": false, "causes": [], "params": []}
#
# So the error arrives INSIDE a success, positionally, with no id on it to
# say which request it answers. `parse_products_array` returns those
# separately instead of letting a `KeyError` end a run or a `.get()` chain
# turn one into a row of Nones.
ERROR_KEYS = ("_ERR_PRODUCT_NOT_FOUND",)

# Every value `Product.price_source` may take. Declared here so the parser,
# the smoke test and .github/ci_checks.py read one list instead of three.
PRICE_SOURCES = ("api-size", "api-bundle")


@dataclass
class ProductsResult:
    rows: List[Product] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    # How many array entries pointed at a bundle another entry had already
    # contributed. See `parse_products_array` — this is a property of the
    # site, not a parser fault, and a run that reports 0 of them on a colour-
    # rich category is the thing worth looking at.
    duplicate_bundles: int = 0


def _availability(is_buyable: Any, back_soon: Any) -> str:
    if _truthy(back_soon):
        return "BackSoon"
    return "InStock" if is_buyable else "OutOfStock"


def parse_products_array(payload: Any, store: Optional[StoreConfig] = None,
                         locale: str = DEFAULT_LOCALE,
                         category: Optional[str] = None,
                         grid_id: Optional[str] = None,
                         page: Optional[int] = None) -> ProductsResult:
    """One `Product` per (product, colour, size), plus whatever was not one.

    The real record is not the array entry. Every visible entry measured on
    2026-09-19 had `type: "BundleBean"` and carried its colours, sizes and
    prices one level down, in `bundleProductSummaries[0].detail`, while its
    own `detail.colors` was an empty list. Reading the outer `detail` gives a
    product with no colours and no price — which looks like a parser that
    works and a site that stopped publishing prices.

    **Several array entries can point at the SAME bundle**, and this is the
    normal case rather than a glitch. The grid lists colourways as separate
    products while the bundle behind them already carries every colour.
    Measured on the SALE dresses grid, 2026-09-19: 13 products resolved to 9
    distinct bundles — one bundle was reached from 3 entries and two more
    from 2 each. Emitting rows per entry produced 195 rows for 131 distinct
    SKUs. So bundles are emitted once per call, first entry wins, and the
    count of entries that were folded away is reported on the result.

    Deduping here rather than only in `output_writer.dedupe_by_sku` is
    deliberate: by the time a duplicate reaches the writer it has already
    cost a full colour-and-size expansion, and the writer cannot say whether
    two identical SKUs are one bundle seen twice or a genuine bug.

    **Which entry a row is attributed to is chosen, not left to arrival
    order.** A bundle reachable from several entries has several candidate
    `product_id`s and therefore several candidate URLs, all of them real. The
    grid does not promise an order: two runs of the same category on
    2026-09-20 attributed all 131 rows to 229723104 and to 229723105
    respectively. Left alone that makes `url` and `product_id` flap between
    runs, and `diff_runs.py` — which compares runs by sku — would report
    every row as changed every time. So the lowest id wins: an arbitrary
    rule, but a stable one, and the alternatives are worse than arbitrary.
    """
    data = _as_json(payload)
    entries = data.get("products") if isinstance(data, dict) else data
    result = ProductsResult()
    store = store or StoreConfig()
    decimals = store.currency_decimals
    row_index = 0
    seen_bundles: set = set()

    # First pass: the lowest entry id each bundle is reachable from. See the
    # docstring — this is what keeps `product_id` and `url` stable between
    # runs of the same category.
    # `owner` maps a bundle to the ENTRY it is attributed to, not just that
    # entry's id: `relatedCategories` lives on the entry and is sparse (8 of
    # 14 entries carried none on 2026-09-19), so reading it off whichever
    # entry happened to be iterated first would make the column flap between
    # runs exactly as `product_id` did.
    owner: Dict[Any, Dict[str, Any]] = {}
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("id") is None:
            continue
        for holder in entry.get("bundleProductSummaries") or []:
            if isinstance(holder, dict) and holder.get("id") is not None:
                current = owner.get(holder["id"])
                if current is None or entry["id"] < current["id"]:
                    owner[holder["id"]] = entry

    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("key") in ERROR_KEYS or (entry.get("id") is None and "description" in entry):
            result.errors.append(entry)
            continue

        product_id = entry.get("id")
        summaries = entry.get("bundleProductSummaries") or []
        holders = summaries if summaries else [entry]

        for holder in holders:
            if not isinstance(holder, dict):
                continue
            bundle_id = holder.get("id")
            if bundle_id is not None and bundle_id in seen_bundles:
                result.duplicate_bundles += 1
                continue
            if bundle_id is not None:
                seen_bundles.add(bundle_id)
            detail = holder.get("detail") or {}
            # The attributed entry, not necessarily the one being iterated.
            attributed = owner.get(bundle_id) or entry
            attributed_id = attributed.get("id", product_id)
            colors = detail.get("colors") or []
            slug = canonical_slug(holder.get("productUrl")
                                  or attributed.get("productUrl"))
            url = product_url(locale, attributed_id, slug) if attributed_id else ""
            title = holder.get("name") or entry.get("name")
            for color in colors:
                if not isinstance(color, dict):
                    continue
                for size in color.get("sizes") or []:
                    if not isinstance(size, dict):
                        continue
                    price = money(size.get("price"), decimals)
                    old = money(size.get("oldPrice"), decimals)
                    pct_raw = (size.get("discountsPercentages") or {}).get("oldPriceDiscount")
                    if pct_raw not in (None, ""):
                        discount, discount_source = _as_float(pct_raw), "api"
                    elif price is not None and old not in (None, 0):
                        discount = round((1 - price / old) * 100, 2)
                        discount_source = "computed"
                    else:
                        discount, discount_source = None, None
                    row_index += 1
                    result.rows.append(Product(
                        url=url,
                        sku=str(size.get("sku")) if size.get("sku") is not None else None,
                        title=title,
                        image_url=_image_url(color, holder, entry, store.image_base_url),
                        price=price,
                        currency=store.currency_code if price is not None else None,
                        category=category,
                        price_source="api-size" if price is not None else None,
                        locale=locale,
                        store_id=store.store_id,
                        product_id=attributed_id,
                        bundle_id=holder.get("id"),
                        reference=detail.get("reference"),
                        display_reference=detail.get("displayReference"),
                        color_id=str(color.get("id")) if color.get("id") is not None else None,
                        color_name=color.get("name"),
                        size_name=size.get("name"),
                        size_id=size.get("mastersSizeId"),
                        partnumber=size.get("partnumber"),
                        availability=_availability(size.get("isBuyable"), size.get("backSoon")),
                        is_buyable=bool(size.get("isBuyable")),
                        back_soon=_truthy(size.get("backSoon")),
                        original_price=old,
                        discount_pct=discount,
                        discount_source=discount_source,
                        promotion_id=size.get("promotionId"),
                        # NOT `entry["section"]`: that field is a numeric
                        # code (it read "1" for WOMEN), and falling back to it
                        # filled the column 100% with a number that looks
                        # like a section and names nothing.
                        section=(holder.get("sectionName")
                                 or holder.get("sectionNameEN")
                                 or attributed.get("sectionNameEN")),
                        family=holder.get("familyName") or entry.get("familyName"),
                        subfamily=holder.get("subFamilyName") or entry.get("subFamilyName"),
                        grid_id=grid_id,
                        related_categories=_related_categories(attributed, holder),
                        country_of_origin=size.get("country"),
                        page=page,
                        row_index=row_index,
                    ))
    return result


# Images live on a different host, and the payload stores the pieces rather
# than a URL. Measured 2026-09-19: `color.image.url` is a stem
# (`/2026/I/0/1/p/1218/714/800/1218714800`), `color.image.type` lists the view
# codes that exist for it (`["1","2","3","4","13"]`), and the serving prefix is
# `store_config.details.imageBaseUrl` — `https://static.bershka.net/4/photos2`.
# The full form `{base}{stem}_{view}_1_0.jpg` answered 200 image/jpeg for views
# 1 (460,944 bytes) and 2 (381,726 bytes). `?ts={timestamp}` is a cache buster
# and is appended when present, but the URL answers 200 without it, so its
# absence is not a broken row.
#
# The prefix is read from the store config rather than hardcoded: the one
# hardcoded guess tried against the live host, `{staticUrl}4/photos2/...`,
# returned 404.
IMAGE_FALLBACK_BASE = "https://static.bershka.net/4/photos2"


def build_image_url(image: Optional[Dict[str, Any]], base: Optional[str] = None) -> Optional[str]:
    """`{imageBaseUrl}{stem}_{view}_1_0.jpg?ts={timestamp}`, or None."""
    if not isinstance(image, dict):
        return None
    stem = image.get("url")
    if not isinstance(stem, str) or not stem:
        return None
    views = [v for v in (image.get("type") or []) if str(v).strip()]
    view = str(views[0]) if views else "1"
    prefix = (base or IMAGE_FALLBACK_BASE).rstrip("/")
    url = f"{prefix}{stem if stem.startswith('/') else '/' + stem}_{view}_1_0.jpg"
    ts = image.get("timestamp")
    return f"{url}?ts={ts}" if ts else url


def _image_url(color: Dict[str, Any], holder: Dict[str, Any], entry: Dict[str, Any],
               base: Optional[str] = None) -> Optional[str]:
    for source in (color, holder, entry):
        built = build_image_url((source or {}).get("image"), base)
        if built:
            return built
    return None


# ---------------------------------------------------------------------------
# Sitemaps
# ---------------------------------------------------------------------------

@dataclass
class SitemapRef:
    url: str
    kind: str                    # "familias" (categories) or "productos"
    locale: Optional[str] = None   # "en-gb"
    part: Optional[int] = None
    gzipped: bool = False


_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAP_NAME_RE = re.compile(
    r"/sitemap/(?P<kind>[^/]+)/sitemap_[^/]*?_(?P<locale>[a-z]{2}(?:-[a-z]{2})?)"
    r"(?:_(?P<segment>[a-z]+))?-part(?P<part>\d+)\.xml(?P<gz>\.gz)?$", re.I)


def maybe_gunzip(blob: bytes) -> str:
    """Decode a sitemap whether or not the transport already un-gzipped it.

    Both happen. `sitemap_indice.xml.gz` arrived still gzipped through a
    proxied `requests` call, while some parts arrive decoded with a
    `utf-8-sig` BOM. Sniffing the magic number is cheaper than being wrong.
    """
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    return blob.decode("utf-8-sig", "replace")


def parse_sitemap_index(blob: bytes) -> List[SitemapRef]:
    """The index, split by kind and locale.

    Measured 2026-09-19: 375 entries — 121 `familias` (categories) and 254
    `productos`. The two do not cover the same ground and neither is the
    locale list on its own: 121 locales have a category sitemap, 85 have a
    product sitemap, and the union is **122**. `pt-pt` publishes products and
    no categories; 37 locales — `ru-kz`, `en-ph`, `es-cr` and 34 more —
    publish categories and no products.

    That is an order of magnitude more than the eleven locales a sibling repo
    reads, and it is why nothing here iterates locales by default. Note also
    that robots forbids a good number of them outright (`/ru/` wholesale, and
    fifty per-country product-page patterns), so the sitemap index is a list
    of what exists, not a list of what may be fetched.
    """
    out: List[SitemapRef] = []
    for url in _LOC_RE.findall(maybe_gunzip(blob)):
        m = _SITEMAP_NAME_RE.search(url)
        if m:
            out.append(SitemapRef(url=url, kind=m.group("kind").lower(),
                                  locale=m.group("locale").lower(),
                                  part=int(m.group("part")),
                                  gzipped=bool(m.group("gz"))))
        else:
            out.append(SitemapRef(url=url, kind="unknown", gzipped=url.endswith(".gz")))
    return out


def sitemap_locales(refs: Iterable[SitemapRef]) -> List[str]:
    return sorted({r.locale for r in refs if r.locale})


_PRODUCT_URL_RE = re.compile(r"/([a-z0-9-]*?)-?c(\d+)p(\d+)\.html$", re.I)


def parse_product_sitemap(blob: bytes, rules: Optional[RobotsRules] = None) -> List[str]:
    """Product URLs from one sitemap part, robots-filtered.

    Filtering here is not belt-and-braces. A sitemap part lists a URL once per
    locale as `xhtml:link` alternates, and robots bans product pages outright
    for a long list of country prefixes (`/kz/*-c0*p*.html` and forty-nine
    more) plus `/ru/` wholesale. A sibling repo had to learn the same lesson
    the same way: the map is not permission.
    """
    urls = _LOC_RE.findall(maybe_gunzip(blob))
    return [u for u in urls if is_robots_allowed(u, rules)]


def product_ids_in_sitemap(blob: bytes) -> List[int]:
    return [int(m.group(3)) for m in
            (_PRODUCT_URL_RE.search(u) for u in _LOC_RE.findall(maybe_gunzip(blob)))
            if m]


# ---------------------------------------------------------------------------
# Storefront HTML — the only thing this repo still reads out of a page
# ---------------------------------------------------------------------------

def store_id_from_page(html: str) -> Optional[Dict[str, Any]]:
    """`{"store_id": 44009506, "catalog_id": 40259534, ...}` from a storefront.

    The blob sits near the end of the document as `var inditex={...}`. This is
    how a locale this repo has never met gets its store id without a hardcoded
    table of 27 guesses.
    """
    m = _INDITEX_BLOB_RE.search(html or "")
    if not m:
        return None
    try:
        blob = json.loads(m.group(1))
    except ValueError:
        return None
    return {
        "store_id": blob.get("iStoreId"),
        "catalog_id": blob.get("iCatalogId"),
        "country_code": blob.get("iCountryCode"),
        "language_id": blob.get("iLangId"),
        "locale": blob.get("iLocale"),
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _related_categories(entry: Dict[str, Any], holder: Dict[str, Any]) -> Optional[List[str]]:
    """Category NAMES from `relatedCategories`, outer entry first.

    The bundle's own list was empty on every product measured 2026-09-19 and
    the outer entry's was not, so the outer one leads. Returns None rather
    than [] when there is nothing: an empty list in a CSV cell is
    indistinguishable from a product with no categories, and only one of
    those is a fact.
    """
    for source in (entry, holder):
        names = [c.get("name") for c in (source or {}).get("relatedCategories") or []
                 if isinstance(c, dict) and c.get("name")]
        if names:
            return names
    return None


def _as_json(payload: Any) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return None
    return None


def _is_int(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    """`backSoon` is the string "0" or "1", and "0" is truthy in Python."""
    if isinstance(value, str):
        return value.strip() not in ("", "0", "false", "False")
    return bool(value)


# ---------------------------------------------------------------------------
# The family surface
# ---------------------------------------------------------------------------
# `page_flow.py`, `diff_runs.py` and the engines are shared across this
# family of repos and call a fixed set of names on whatever
# `product_parser` is next to them. Implemented here against THIS site,
# including the parts that come out degenerate — a no-op is a measurement
# too, and a missing function would just move the failure to import time.

HOSTS = ("www.bershka.com", "bershka.com")

# Every locale prefix the sitemap index publishes, as `xx` or `xx/yy` path
# segments would appear in a URL. Left as the measured list rather than a
# curated one: see `parse_sitemap_index`.
LOCALES = ("gb", "de")     # the ones with a measured store id — see KNOWN_STORES
PAGINATED_MODES = ()       # nothing here pages; see `page_url`

_SITE_ASSET_MARKER = "static.bershka.net"
_STOREFRONT_HOOK = "var inditex="


def site_host(url: str) -> Optional[str]:
    try:
        return urlsplit(url).netloc.lower() or None
    except ValueError:
        return None


def is_supported_host(url: str) -> bool:
    return site_host(url) in HOSTS


def unsupported_reason(url: str) -> str:
    host = site_host(url)
    if not host:
        return "is not a URL this repo can route"
    if host.endswith(".bershka.net"):
        return ("is Bershka's static asset host, which serves images and "
                "scripts but no catalogue")
    return "is not bershka.com"


def locale_of(url: str) -> Optional[str]:
    """The storefront prefix in a product or category URL, e.g. `gb`."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}", parts[0]):
        return parts[0]
    return None


def sku_from_url(url: str) -> Optional[str]:
    """The id a product URL carries.

    Named `sku_from_url` for the family, but be clear about what it returns:
    the URL carries the PRODUCT id (`-c0p229723104.html`), and a SKU on this
    site is a size within a colour within that product. A URL cannot name a
    SKU, so this can never round-trip with `Product.sku`, and nothing in this
    repo pretends otherwise.
    """
    m = _PRODUCT_URL_RE.search(urlsplit(url).path)
    return m.group(3) if m else None


def category_from_url(url: str) -> Optional[str]:
    """No category is recoverable from a URL here.

    A listing URL is `/gb/women.html` — a page that renders no products at
    all. The category a row belongs to comes from the menu trail that led to
    its grid, which is why `parse_products_array` takes `category` as an
    argument instead of deriving one.
    """
    return None


def page_number_from_url(url: str) -> int:
    """Always 1. There is no paging parameter — see `page_url`."""
    return 1


def parse_money(text: Any, decimals: Optional[int] = -2) -> Optional[float]:
    """The family's money entry point.

    On this site prices never arrive as display text: the API returns minor
    units (`"2099"`), so this is `money()` with the store's exponent and not
    a symbol-and-separator parser. Passing it "£20.99" returns None rather
    than 20.99 — deliberately, because a repo that could read a price off a
    string would invite somebody to read one off a page that has none.
    """
    if isinstance(text, str) and not text.strip().lstrip("-").isdigit():
        return None
    return money(text, decimals)


def sitemap_url(locale: str, kind: str = "product", part: int = 0,
                segment: str = "") -> str:
    """One locale's sitemap.

    The naming is Inditex's, not Demandware's: categories are `familias` and
    products are `productos`, locales are `en-gb` rather than `gb`, product
    files are split by section (`_women`, `_men`) and everything under
    `productos` is gzipped while `familias` is not.
    """
    kind = {"product": "productos", "category": "familias"}.get(kind, kind)
    if kind == "productos":
        seg = f"_{segment}" if segment else ""
        return f"{BASE}/sitemap/productos/sitemap_productos_{locale}{seg}-part{part}.xml.gz"
    return f"{BASE}/sitemap/familias/sitemap_familias_{locale}-part{part}.xml"


def parse_sitemap(blob: Any) -> List[str]:
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    return _LOC_RE.findall(maybe_gunzip(blob or b""))


def product_urls_from_sitemap(blob: Any, rules: Optional[RobotsRules] = None) -> List[str]:
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    return parse_product_sitemap(blob or b"", rules)


def parse_category(payload: Any, url: str = "", **kw) -> List[Product]:
    """The family's listing entry point — a productsArray payload here."""
    return parse_products_array(payload, **kw).rows


def parse_product(payload: Any, url: str = "", **kw) -> List[Product]:
    """The family's detail entry point.

    Same payload shape as a listing: this API returns the full colour and
    size tree for a grid, so a "product page" adds nothing a listing did not
    already carry. That is the opposite of the sibling repos, where the
    product page was the richer source, and it is why `--mode product` here
    fetches the same endpoint for one id instead of a different one.
    """
    return parse_products_array(payload, **kw).rows


def is_self_clearing_challenge(html: str) -> bool:
    """Whether this page reloads itself once the challenge passes.

    The edge refusal deliberately does NOT match: its 195-byte body carries
    neither `bm-verify` nor `/_sec/verify`, so a run that meets it stops
    instead of waiting out a wall that will never clear.
    """
    if not html:
        return False
    return any(m in html for m in SELF_CLEARING_CHALLENGE_MARKERS)


def detect_bot_challenge(body: str, url: Optional[str] = None) -> Optional[str]:
    """The challenge vendor found in `body`, or None."""
    if not body:
        return None
    low = body.lower()
    for vendor, markers in BOT_CHALLENGE_MARKERS.items():
        if any(marker in low for marker in markers):
            return vendor
    return None


def detect_page_state(body: str, status: Optional[int] = None,
                      url: Optional[str] = None,
                      headers: Optional[Dict[str, str]] = None) -> str:
    """content | blocked | captcha | empty — see page_flow.STATE_POLICY.

    Ordered by how much each check PROVES, per CLAUDE.md §17:

    1. The edge refusal is checked first, headers included, because it is the
       one wall this site actually served and the only one whose remedy is a
       different exit rather than a retry.
    2. An empty body proves a failed fetch and nothing else.
    3. A JSON payload that parses is content. This is the normal case here
       and it has no HTML markers at all, so a check written for pages would
       call every successful fetch `blocked`.
    4. A sitemap is content, for the same reason.
    5. Then the challenge scan, then a non-2xx status, which proves something
       went wrong without saying what — and vetoes any positive above it.

    An empty catalogue is `empty`, not `blocked`: a grid can legitimately
    return zero ids, and a run must not retry its way around a real answer.
    """
    head = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if is_edge_refusal(status, body or "", head):
        return "blocked"
    if not body:
        return "blocked"

    status_ok = status is None or 200 <= status < 300
    stripped = body.lstrip()
    if status_ok and stripped[:1] in ("{", "["):
        data = _as_json(body)
        if data is None:
            return "blocked"
        if isinstance(data, dict) and (data.get("products") == [] or data.get("productIds") == []):
            return "empty"
        return "content"
    if status_ok and ("<urlset" in body or "<sitemapindex" in body):
        return "content"
    if status_ok and _STOREFRONT_HOOK in body and _SITE_ASSET_MARKER in body:
        return "content"

    vendor = detect_bot_challenge(body, url)
    if vendor:
        return "blocked"
    if not status_ok:
        return "blocked"
    return "empty"


def is_product_url(url: str) -> bool:
    """A product page, by the `-c{n}p{id}.html` shape the sitemap publishes."""
    return bool(_PRODUCT_URL_RE.search(urlsplit(url).path))


_CATEGORY_URL_RE = re.compile(r"/[a-z]{2}/[a-z0-9-]+\.html$", re.I)


def is_category_url(url: str) -> bool:
    """A listing page.

    True for `/gb/women.html`, and worth remembering what that page is: it
    renders no products at all. This exists so an engine can tell the two
    URL shapes apart, not because a category page is a source of rows.
    """
    path = urlsplit(url).path
    return bool(_CATEGORY_URL_RE.search(path)) and not is_product_url(url)


def parse_price(raw: Any, decimals: Optional[int] = -2) -> Optional[float]:
    """Alias kept for the family's callers. See `parse_money`."""
    return parse_money(raw, decimals)
