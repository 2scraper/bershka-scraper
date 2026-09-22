#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
catalog_walk.py
---------------
The route through Bershka's catalogue, with the transport left out.

Every engine in this repo walks the same four steps — store config, menu,
grid, productsArray — and they differ only in how a URL becomes bytes. So the
walk lives here once and each engine hands it a `fetch` callable. That is not
tidiness for its own sake: the sibling repos each carry four copies of their
crawl, one per engine, and CLAUDE.md §11 is in the template because a copy of
a check is a check that drifts.

A `fetch` is::

    fetch(url: str) -> Fetched(status: int, body: bytes, headers: dict)

and may raise. Everything above it — robots enforcement, batching,
deduplication, the stop reasons — is here and is therefore identical across
all four engines by construction rather than by review.

What this costs, measured 2026-09-19 (store 44009506, `gb`)
-----------------------------------------------------------
    store config        179,035 b     1 request
    menu                342,820 b     1 request
    one grid             21,522 b     1 request
    40 products       1,031,388 b     1 request

The last line is the one to plan around: a full colour-and-size tree per
product makes productsArray roughly 25 KB per product, so a category of 200
is about 5 MB. `PRODUCTS_PER_CALL` trades round trips against that.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import product_parser as P
from output_writer import Product

logger = logging.getLogger("catalog_walk")


@dataclass
class Fetched:
    status: int
    body: bytes
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace") if self.body else ""


class RobotsRefusal(RuntimeError):
    """Raised instead of fetching a URL robots.txt disallows."""


class EdgeRefusal(RuntimeError):
    """The exit address was refused. Retrying from the same exit is pointless."""


@dataclass
class WalkResult:
    rows: List[Product] = field(default_factory=list)
    store: Optional[P.StoreConfig] = None
    grids_seen: int = 0
    grids_fetched: int = 0
    products_requested: int = 0
    payload_errors: List[Dict[str, Any]] = field(default_factory=list)
    duplicate_bundles: int = 0
    requests_made: int = 0
    bytes_read: int = 0
    stop_reason: str = "completed"


def _get(fetch: Callable[[str], Fetched], url: str, result: WalkResult,
         rules: Optional[P.RobotsRules] = None) -> Fetched:
    """Fetch one URL, refusing what robots refuses and naming what refused us."""
    if not P.is_robots_allowed(url, rules):
        raise RobotsRefusal(f"robots.txt disallows {url}")
    got = fetch(url)
    result.requests_made += 1
    result.bytes_read += len(got.body or b"")
    if P.is_edge_refusal(got.status, got.text[:4000], got.headers):
        raise EdgeRefusal(
            f"HTTP {got.status} from the edge for {url} — this is the exit "
            f"address being refused, not a challenge to wait out.")
    return got


def load_store(fetch: Callable[[str], Fetched], store_id: int,
               result: Optional[WalkResult] = None,
               rules: Optional[P.RobotsRules] = None) -> P.StoreConfig:
    result = result if result is not None else WalkResult()
    got = _get(fetch, P.store_config_url(store_id), result, rules)
    store = P.parse_store_config(got.body)
    if store.catalog_id is None:
        logger.warning("store %s published no type-1 catalogue; productsArray "
                       "will have nothing to address", store_id)
    return store


def load_grids(fetch: Callable[[str], Fetched], store_id: int,
               result: Optional[WalkResult] = None,
               rules: Optional[P.RobotsRules] = None) -> List[P.MenuGrid]:
    result = result if result is not None else WalkResult()
    got = _get(fetch, P.menu_url(store_id), result, rules)
    return P.walk_menu(got.body)


def select_grids(grids: Sequence[P.MenuGrid], category: Optional[str] = None,
                 limit: Optional[int] = None) -> List[P.MenuGrid]:
    """Grids whose menu trail contains `category`, case-insensitively.

    Matching on the trail rather than the leaf name is what makes
    `--category "WOMEN / SALE"` mean the whole SALE subtree and
    `--category dresses` mean every dresses grid under any section. There is
    no category id to look up: the menu is the only index of grids there is.
    """
    picked = list(grids)
    if category:
        needle = category.strip().lower()
        picked = [g for g in picked if needle in g.trail.lower()]
    return picked[:limit] if limit else picked


def walk_grids(fetch: Callable[[str], Fetched], store: P.StoreConfig,
               grids: Sequence[P.MenuGrid], locale: str = P.DEFAULT_LOCALE,
               delay: float = 0.0, max_products: Optional[int] = None,
               rules: Optional[P.RobotsRules] = None,
               result: Optional[WalkResult] = None) -> WalkResult:
    """Fetch each grid and expand its products into rows.

    Rows are deduplicated on `sku` ACROSS grids as well as within one, because
    the menu genuinely lists the same grid twice in places and a product can
    sit in more than one category. The row that wins is the first one seen,
    so `category` on a row names the first menu trail that reached it — not
    the only one. That is a real limitation and it is written down rather
    than papered over by inventing a category list column nobody measured.
    """
    result = result if result is not None else WalkResult()
    result.store = store
    result.grids_seen = len(grids)
    seen_sku: set = set()

    for grid in grids:
        if max_products is not None and len(result.rows) >= max_products:
            result.stop_reason = "max_products_reached"
            break
        try:
            got = _get(fetch, P.grid_url(store.store_id, grid.grid_id), result, rules)
        except RobotsRefusal as exc:
            logger.warning("skipping grid %s: %s", grid.grid_id, exc)
            continue
        result.grids_fetched += 1
        parsed = P.parse_grid(got.body)
        ids = parsed.product_ids
        if not ids:
            logger.info("grid %s (%s) listed no products", grid.grid_id, grid.trail)
            continue
        if len(parsed.sorted_product_ids) != len(ids):
            # Not an error. The SALE dresses grid served 14 and 13 on
            # 2026-09-19 and the extra id was the one whose product came back
            # as `_ERR_PRODUCT_NOT_FOUND`. Worth a line in the log so a run
            # that suddenly loses half a grid is visible.
            logger.info("grid %s: %d productIds, %d sortedProductIds",
                        grid.grid_id, len(ids), len(parsed.sorted_product_ids))

        for start in range(0, len(ids), P.PRODUCTS_PER_CALL):
            batch = ids[start:start + P.PRODUCTS_PER_CALL]
            url = P.products_array_url(store.store_id, store.catalog_id, batch)
            try:
                got = _get(fetch, url, result, rules)
            except RobotsRefusal as exc:
                logger.warning("skipping batch: %s", exc)
                continue
            result.products_requested += len(batch)
            parsed_products = P.parse_products_array(
                got.body, store, locale=locale, category=grid.trail,
                grid_id=grid.grid_id, page=1)
            result.payload_errors.extend(parsed_products.errors)
            result.duplicate_bundles += parsed_products.duplicate_bundles
            for row in parsed_products.rows:
                if row.sku and row.sku in seen_sku:
                    continue
                if row.sku:
                    seen_sku.add(row.sku)
                result.rows.append(row)
                if max_products is not None and len(result.rows) >= max_products:
                    break
            if delay:
                time.sleep(delay)
            if max_products is not None and len(result.rows) >= max_products:
                result.stop_reason = "max_products_reached"
                break
    return result


def crawl(fetch: Callable[[str], Fetched], store_id: int,
          category: Optional[str] = None, locale: str = P.DEFAULT_LOCALE,
          max_grids: Optional[int] = None, max_products: Optional[int] = None,
          delay: float = 0.0, rules: Optional[P.RobotsRules] = None) -> WalkResult:
    """store config -> menu -> grids -> products, in one call."""
    result = WalkResult()
    store = load_store(fetch, store_id, result, rules)
    grids = load_grids(fetch, store_id, result, rules)
    picked = select_grids(grids, category, max_grids)
    if not picked:
        result.stop_reason = ("no_grid_matched_category" if category
                              else "menu_listed_no_grids")
        result.store = store
        result.grids_seen = 0
        return result
    return walk_grids(fetch, store, picked, locale=locale, delay=delay,
                      max_products=max_products, rules=rules, result=result)
