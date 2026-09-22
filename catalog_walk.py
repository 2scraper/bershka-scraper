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
import random
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


class HttpError(RuntimeError):
    """A non-2xx response that is not the edge refusal."""

    def __init__(self, message: str, status: int, url: str, retryable: bool):
        super().__init__(message)
        self.status = status
        self.url = url
        self.retryable = retryable


class SchemaError(RuntimeError):
    """A 2xx response whose body is not the shape this endpoint promises."""

    def __init__(self, message: str, url: str):
        super().__init__(message)
        self.url = url


class TransportError(RuntimeError):
    """The fetch itself failed — a timeout, a dropped connection, a bad TLS
    handshake. The site never answered, so nothing can be read into it."""

    def __init__(self, message: str, url: str):
        super().__init__(message)
        self.url = url


# Statuses worth trying again, and how hard to try.
#
# Everything else is a hard failure: a 404 means the endpoint is gone, a 403
# that is not the edge refusal means the application refused, and repeating
# either only spends bandwidth. 429 and the 5xx range are the two cases where
# the same request later is a different question.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_RETRIES = 2
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 20.0


@dataclass
class WalkResult:
    rows: List[Product] = field(default_factory=list)
    store: Optional[P.StoreConfig] = None
    grids_seen: int = 0
    grids_fetched: int = 0
    # The grids this run actually read, for the scope fingerprint a diff
    # compares. Ids, not names: a menu label can be translated or renamed
    # without the grid changing.
    grid_ids: List[str] = field(default_factory=list)
    products_requested: int = 0
    payload_errors: List[Dict[str, Any]] = field(default_factory=list)
    duplicate_bundles: int = 0
    requests_made: int = 0
    bytes_read: int = 0
    # Every request that did not produce usable data, with a name for why.
    # A run with a non-empty list here is NOT complete, whatever it managed
    # to collect — see `settle()`.
    failures: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "completed"

    def record_failure(self, kind: str, url: str, detail: str,
                       status: Optional[int] = None) -> None:
        self.failures.append({"kind": kind, "url": url, "detail": detail,
                              "status": status})
        logger.warning("%s on %s: %s", kind, url, detail)

    def settle(self) -> "WalkResult":
        """Give the run an honest stop reason before it is reported.

        `completed` is a claim that everything asked for was seen. If any
        request failed, that claim is false however many rows came back, and
        `requests_failed` is not in `output_writer.COMPLETE_STOP_REASONS`, so
        the run is written as partial and exits 6 instead of 0.
        """
        if self.failures and self.stop_reason == "completed":
            self.stop_reason = "requests_failed"
        return self


def _retry_after(got: Fetched, attempt: int) -> float:
    """Seconds to wait, honouring `Retry-After` when the server sent one.

    Exponential with jitter otherwise: several workers that back off on the
    same schedule return together and reproduce the burst that earned the 429.
    """
    header = None
    for key, value in (got.headers or {}).items():
        if str(key).lower() == "retry-after":
            header = value
            break
    if header:
        try:
            return min(float(str(header).strip()), RETRY_MAX_DELAY)
        except ValueError:
            pass
    return min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY) * (0.5 + random.random())


def _get(fetch: Callable[[str], Fetched], url: str, result: WalkResult,
         rules: Optional[P.RobotsRules] = None, retries: int = DEFAULT_RETRIES,
         sleep: Callable[[float], None] = time.sleep) -> Fetched:
    """Fetch one URL and prove the response is usable, or raise saying why.

    Before this, ONLY the edge refusal was rejected and every other response
    was handed to the parser. A 500, a 429 and an HTML error page at HTTP 200
    all became "this category lists no products", and the run went on to
    report `completed` — measured 2026-09-22 on all three. For a price
    monitor that reads as "those products are gone", which is the worst thing
    this repo could get wrong, so each failure now has a name and none of
    them is silent.
    """
    if not P.is_robots_allowed(url, rules):
        raise RobotsRefusal(f"robots.txt disallows {url}")

    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            got = fetch(url)
        except (RobotsRefusal, EdgeRefusal):
            raise
        except Exception as exc:  # noqa: BLE001 — named, not swallowed
            last = TransportError(f"{type(exc).__name__}: {exc}", url)
            if attempt < retries:
                delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                logger.info("transport failure on %s (%s); retrying in %.1fs",
                            url, exc, delay)
                sleep(delay)
                continue
            raise last from exc

        result.requests_made += 1
        result.bytes_read += len(got.body or b"")

        if P.is_edge_refusal(got.status, got.text[:4000], got.headers):
            raise EdgeRefusal(
                f"HTTP {got.status} from the edge for {url} — this is the exit "
                f"address being refused, not a challenge to wait out.")

        if 200 <= got.status < 300:
            return got

        retryable = got.status in RETRYABLE_STATUSES
        last = HttpError(f"HTTP {got.status} for {url}", got.status, url, retryable)
        if retryable and attempt < retries:
            delay = _retry_after(got, attempt)
            logger.info("HTTP %s on %s; retrying in %.1fs (attempt %d of %d)",
                        got.status, url, delay, attempt + 1, retries)
            sleep(delay)
            continue
        raise last

    raise last if last else TransportError("no response and no error", url)


def _json_or_raise(got: Fetched, url: str, required: str) -> Any:
    """The parsed body, or a SchemaError naming what was missing.

    `product_parser._as_json` returns None for unparseable input and the
    parsers turn None into an empty result — correct for a parser, wrong for
    a fetch, because "the body was not JSON" and "the category is empty" are
    then indistinguishable. This is where they are separated.
    """
    data = P._as_json(got.body)
    if data is None:
        head = got.text[:120].replace("\n", " ")
        raise SchemaError(
            f"HTTP {got.status} for {url} carried {len(got.body or b'')} bytes "
            f"that are not JSON: {head!r}", url)
    if required and isinstance(data, dict) and required not in data:
        raise SchemaError(
            f"HTTP {got.status} for {url} is JSON but has no {required!r} "
            f"(keys: {sorted(data)[:8]})", url)
    return data


def load_store(fetch: Callable[[str], Fetched], store_id: int,
               result: Optional[WalkResult] = None,
               rules: Optional[P.RobotsRules] = None) -> P.StoreConfig:
    result = result if result is not None else WalkResult()
    got = _get(fetch, P.store_config_url(store_id), result, rules)
    _json_or_raise(got, P.store_config_url(store_id), "catalogs")
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
    _json_or_raise(got, P.menu_url(store_id), "items")
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
               delay: float = 0.0, max_skus: Optional[int] = None,
               rules: Optional[P.RobotsRules] = None,
               result: Optional[WalkResult] = None,
               on_grid: Optional[Callable[[], None]] = None) -> WalkResult:
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
    # One map for the whole walk — see parse_products_array's `owners`.
    owners: Dict[Any, Dict[str, Any]] = {}
    # Product ids already requested in this run. Deduplication used to happen
    # AFTER the payload came back, so a product listed in four grids was
    # downloaded four times — at roughly 25 KB each, on paid bandwidth, to be
    # thrown away. The ids are known before the request, so the saving is
    # free; category membership is collected separately in `trails`, which is
    # why dropping the duplicate fetch loses nothing.
    fetched_ids: set = set()
    # Which menu trails list a given PRODUCT ID. Keyed on the id from the
    # grid's own list rather than on the fetched rows, because the id cache
    # below deliberately does not re-fetch a product a previous grid already
    # returned — and membership of the second category is still a fact about
    # the product. `category` keeps the first trail for compatibility.
    id_trails: Dict[int, List[str]] = {}
    # entry id -> bundle, filled while parsing, so a row can be joined back
    # to every grid that listed any entry pointing at its bundle.
    bundle_entries: Dict[Any, set] = {}

    for grid in grids:
        if max_skus is not None and len(result.rows) >= max_skus:
            result.stop_reason = "max_skus_reached"
            break
        # `--proxy-rotate per-page` means a new exit per grid, and it has to
        # be a hook rather than something the walk does itself: the walk has
        # no idea what a proxy is, and the engine that owns the pool must
        # rebuild its session with the new address at the same moment.
        if on_grid is not None:
            on_grid()
        grid_url = P.grid_url(store.store_id, grid.grid_id)
        try:
            got = _get(fetch, grid_url, result, rules)
            _json_or_raise(got, grid_url, "productIds")
        except RobotsRefusal as exc:
            result.record_failure("robots_refusal", grid_url, str(exc))
            continue
        except HttpError as exc:
            # The grid was not read. Recording it and moving on keeps one bad
            # category from ending a run, while `settle()` makes sure the run
            # can never call itself complete afterwards.
            result.record_failure("http_error", grid_url, str(exc), exc.status)
            continue
        except SchemaError as exc:
            result.record_failure("schema_error", grid_url, str(exc))
            continue
        except TransportError as exc:
            result.record_failure("transport_error", grid_url, str(exc))
            continue
        result.grids_fetched += 1
        result.grid_ids.append(str(grid.grid_id))
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

        for product_id in ids:
            seen = id_trails.setdefault(product_id, [])
            if grid.trail not in seen:
                seen.append(grid.trail)
        fresh = [i for i in ids if i not in fetched_ids]
        if len(fresh) != len(ids):
            logger.info("grid %s: %d of %d product(s) already fetched this run",
                        grid.grid_id, len(ids) - len(fresh), len(ids))
        fetched_ids.update(fresh)
        for start in range(0, len(fresh), P.PRODUCTS_PER_CALL):
            batch = fresh[start:start + P.PRODUCTS_PER_CALL]
            url = P.products_array_url(store.store_id, store.catalog_id, batch)
            try:
                got = _get(fetch, url, result, rules)
                _json_or_raise(got, url, "products")
            except RobotsRefusal as exc:
                result.record_failure("robots_refusal", url, str(exc))
                continue
            except HttpError as exc:
                result.record_failure("http_error", url, str(exc), exc.status)
                continue
            except SchemaError as exc:
                result.record_failure("schema_error", url, str(exc))
                continue
            except TransportError as exc:
                result.record_failure("transport_error", url, str(exc))
                continue
            result.products_requested += len(batch)
            parsed_products = P.parse_products_array(
                got.body, store, locale=locale, category=grid.trail,
                grid_id=grid.grid_id, page=1, owners=owners,
                bundle_entries=bundle_entries)
            result.payload_errors.extend(parsed_products.errors)
            result.duplicate_bundles += parsed_products.duplicate_bundles
            for row in parsed_products.rows:
                if row.sku and row.sku in seen_sku:
                    continue
                if row.sku:
                    seen_sku.add(row.sku)
                result.rows.append(row)
                if max_skus is not None and len(result.rows) >= max_skus:
                    break
            if delay:
                time.sleep(delay)
            if max_skus is not None and len(result.rows) >= max_skus:
                result.stop_reason = "max_skus_reached"
                break
    _canonicalise(result, owners, id_trails, bundle_entries, locale)
    return result.settle()


def _canonicalise(result: WalkResult, owners: Dict[Any, Dict[str, Any]],
                  id_trails: Dict[int, List[str]],
                  bundle_entries: Dict[Any, set], locale: str) -> None:
    """Re-attribute every row to the run-wide owner of its bundle.

    A row emitted in batch 1 may have been attributed to the only entry seen
    so far; a lower entry id for the same bundle can turn up in batch 7. Left
    alone, `product_id` and `url` depend on which batch a bundle appeared in
    first, and two runs that split batches differently export different
    values for identical data. This pass runs once, after everything is read,
    so the answer cannot depend on order at all.
    """
    for row in result.rows:
        owner = owners.get(row.bundle_id)
        if owner and owner.get("id") is not None and owner["id"] != row.product_id:
            row.product_id = owner["id"]
            slug = P.canonical_slug(owner.get("productUrl"))
            row.url = P.product_url(locale, owner["id"], slug)
        # Every grid that listed ANY entry pointing at this bundle, not just
        # the one whose payload produced the row. Sorted, not in traversal
        # order: two runs that walk the same grids in a different order must
        # export the same bytes, or `diff_runs` reports the traversal.
        found: set = set()
        for entry_id in bundle_entries.get(row.bundle_id, {row.product_id}):
            found.update(id_trails.get(entry_id, []))
        row.categories = sorted(found) or None


def crawl(fetch: Callable[[str], Fetched], store_id: int,
          category: Optional[str] = None, locale: str = P.DEFAULT_LOCALE,
          max_grids: Optional[int] = None, max_skus: Optional[int] = None,
          delay: float = 0.0, rules: Optional[P.RobotsRules] = None,
          on_grid: Optional[Callable[[], None]] = None,
          store: Optional[P.StoreConfig] = None) -> WalkResult:
    """store config -> menu -> grids -> products, in one call.

    **This never raises away rows it has already collected.** It used to: an
    exception from the second batch escaped, the engine mapped it to an exit
    code, and `finish_run` was never reached — so a timeout after a good
    batch wrote no JSON, no CSV and no metadata at all (measured 2026-09-22).
    Anything that stops the walk is now recorded on the result and the result
    is returned, so the caller always has both the rows and the reason.
    """
    result = WalkResult()
    try:
        # The caller may already have read it — `api_scraper.resolve_store`
        # does, to find the store id in the first place. Fetching it a second
        # time cost an extra request per run and, worse, that request was not
        # counted in the run's own tally, so the reported cost was wrong.
        store = store or load_store(fetch, store_id, result, rules)
        result.store = store
        grids = load_grids(fetch, store_id, result, rules)
    except (HttpError, SchemaError, TransportError) as exc:
        # Nothing was collected yet, but the failure still has to be named
        # rather than turned into a bare exit code.
        result.record_failure(type(exc).__name__, getattr(exc, "url", ""), str(exc),
                              getattr(exc, "status", None))
        result.stop_reason = "setup_failed"
        return result

    picked = select_grids(grids, category, max_grids)
    if not picked:
        result.stop_reason = ("no_grid_matched_category" if category
                              else "menu_listed_no_grids")
        result.grids_seen = 0
        return result

    try:
        return walk_grids(fetch, store, picked, locale=locale, delay=delay,
                          max_skus=max_skus, rules=rules, result=result,
                          on_grid=on_grid)
    except EdgeRefusal as exc:
        # The exit was refused mid-walk. That ends the run — a different exit
        # is the only fix — but the rows already parsed are still real.
        result.record_failure("edge_refusal", getattr(exc, "url", ""), str(exc))
        result.stop_reason = "edge_refusal"
        return result.settle()
    except Exception as exc:  # noqa: BLE001 — recorded, and the rows survive
        result.record_failure(type(exc).__name__, "", str(exc))
        result.stop_reason = "aborted"
        return result.settle()
