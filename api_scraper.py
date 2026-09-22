#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api_scraper.py
--------------
The primary engine: plain HTTPS to Bershka's own catalogue API, no browser.

Why this is the primary here, and not a fallback
================================================
In every sibling repo the browser engines are the real ones and the
HTTP client is the cheap alternative. On this site it is the other way round,
and the reason is measured rather than preferred:

* A rendered listing page carries **zero** products — 0 JSON-LD blocks,
  0 product ids, 0 grid classes in 938,454 bytes (2026-09-19). There is
  nothing in the DOM for a browser to read that is not already in the JSON.
* The catalogue API answered **HTTP 200 to a plain `requests` call** through
  a proxy exit, with no browser, no cookie jar and no TLS impersonation —
  checked twice, once with impersonation explicitly disabled, on 2026-09-19.
  The same call from this machine's own address returned 403.

So a browser buys nothing here except an exit address and a cookie jar that
the API does not ask for. The three browser engines are kept for parity (and
because an exit can be all you have), but this is the one to run.

The exit is not optional
------------------------
Measured 2026-09-19 from a Moscow residential address, every URL on this host
returned HTTP 403 with a 195-byte `Service Unavailable` body — including
`/robots.txt`. Through a proxy the same three URLs returned 200. This engine
therefore treats "no proxy configured" as a thing worth saying out loud
before it spends a run finding out.

Usage
-----
    python3 api_scraper.py --category "WOMEN / SALE" --max-grids 2
    python3 api_scraper.py --mode product --product-id 229723104
    python3 api_scraper.py --list-categories | head -40

Credentials come from `.env` through `env_config`, never from argv: `ps`
reads argv, and a proxy URL carries a password.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import browser_bridge
import env_config
import product_parser as P
import proxy_pool
from catalog_walk import EdgeRefusal, Fetched, RobotsRefusal, WalkResult, crawl, load_store
from output_writer import (EXIT_BLOCKED, EXIT_REMOTE_API_ERROR, Product,
                           finish_run, scope_fingerprint)

logger = logging.getLogger("api_scraper")

# A browser's UA, because the edge refuses on address rather than on client
# string and an honest-looking one keeps the request boring. Nothing here
# depends on it: the 200s above were measured with this exact string, and the
# vendor's own client returned 200 with TLS impersonation switched OFF, so
# this is not a fingerprint trick and is not documented as one.
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def build_fetch(args, pool: Optional[proxy_pool.ProxyPool]):
    """A `fetch(url) -> Fetched` whose session is BOUND to one exit.

    The exit used to be read once and captured in the closure forever, so
    `--proxy-rotate` was accepted and did nothing and a pool of 55 addresses
    burned exactly one. That matters here rather than being theoretical: 4 of
    10 exits were refused outright on 2026-09-20, and a run that starts on one
    of them had no way to reach a working address.

    Rotating is not just calling `pool.advance()`. The session goes with the
    address: cookies an edge issued against one IP, replayed from another,
    are a stronger signal than either address on its own — see
    `proxy_pool`'s module docstring. So `_rebind()` throws the session away
    and builds a new one, and every caller of a rotation goes through it.

    Bounded by the size of the pool: with no address left that has not been
    tried, the failure is raised rather than looped on.
    """
    try:
        import requests
    except ImportError:  # pragma: no cover - reported, not raised
        raise SystemExit("api_scraper.py needs `requests`: pip install -r requirements.txt")

    state: Dict[str, Any] = {"session": None, "exit": None, "tried": set()}

    def _rebind():
        state["exit"] = pool.current if pool else None
        session = requests.Session()
        session.headers.update({"User-Agent": args.user_agent, "Accept": "*/*"})
        state["session"] = session
        if state["exit"]:
            state["tried"].add(state["exit"])
            logger.info("Exit: %s", proxy_pool.mask(state["exit"]))

    _rebind()
    if not state["exit"]:
        logger.warning(
            "No proxy configured. Measured 2026-09-19, this host answers 403 "
            "to every request from a residential address, robots.txt "
            "included. Set BERSHKA_PROXY in .env or pass --proxy-file.")

    def rotate(reason: str) -> bool:
        """Move to an exit this run has not used. False when there is none."""
        if pool is None or len(pool) < 2:
            return False
        for _ in range(len(pool)):
            pool.advance(reason)
            if pool.current not in state["tried"]:
                logger.warning("rotating exit after %s -> %s", reason,
                               proxy_pool.mask(pool.current))
                _rebind()
                return True
        logger.warning("every exit in the pool has been tried; not rotating again")
        return False

    def _once(url: str) -> Fetched:
        exit_url = state["exit"]
        proxies = {"http": exit_url, "https": exit_url} if exit_url else None
        response = state["session"].get(url, proxies=proxies,
                                        timeout=args.timeout, allow_redirects=True)
        if P.is_self_clearing_challenge(response.text) and clear_interstitial(response, url):
            response = state["session"].get(url, proxies=proxies,
                                            timeout=args.timeout, allow_redirects=True)
        return Fetched(status=response.status_code, body=response.content,
                       headers=dict(response.headers))

    def clear_interstitial(response, url: str) -> bool:
        """Answer the shim's arithmetic over plain HTTP. True if accepted.

        This is rung 1 for the engine that has no browser. Measured
        2026-09-20: 8 of 10 exits served this shim on the storefront — HTTP
        200, ~2,142 bytes — and it does NOT clear by itself for a plain
        client. Three fetches in one session, keeping `ak_bmsc`, returned the
        shim all three times, so retrying without answering it is a loop.

        The catalogue API is not gated by it, which is why this engine
        normally never meets one: it goes straight to the API.
        """
        payload = P.parse_interstitial(response.text)
        if not payload:
            return False
        origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        exit_url = state["exit"]
        proxies = {"http": exit_url, "https": exit_url} if exit_url else None
        logger.info("self-clearing interstitial on %s — answering it", url)
        try:
            verified = state["session"].post(
                origin + P.INTERSTITIAL_VERIFY_PATH,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json", "Referer": url,
                         "Origin": origin, "Sec-Fetch-Mode": "cors",
                         "Sec-Fetch-Site": "same-origin",
                         "Sec-Fetch-Dest": "empty"},
                proxies=proxies, timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("interstitial verify failed: %s", exc)
            return False
        logger.info("interstitial verify -> HTTP %s", verified.status_code)
        return verified.status_code == 200

    def fetch(url: str) -> Fetched:
        got = _once(url)
        # An address that is refused stays refused; the pool exists for
        # exactly this, and rotating is the only thing that can help.
        if P.is_edge_refusal(got.status, got.text[:4000], got.headers):
            if rotate(f"edge refusal on {url}"):
                got = _once(url)
        return got

    fetch.rotate = rotate          # so per-page rotation can reach it
    return fetch


def resolve_store(args, fetch) -> P.StoreConfig:
    """The store id for `--site-locale`, measured or read off the storefront.

    Only `gb` has a store id in `product_parser.KNOWN_STORES`, because only
    `gb` has had one measured. For anything else this fetches the storefront
    and reads `var inditex={...}` rather than guessing from the 26 other ids
    that page happens to list.
    """
    known = P.KNOWN_STORES.get(args.site_locale)
    if known:
        return load_store(fetch, known["store_id"])

    url = f"{P.BASE}/{args.site_locale}/"
    if not P.is_robots_allowed(url):
        raise SystemExit(f"{url} is disallowed by robots.txt; refusing to fetch it.")
    got = fetch(url)
    blob = P.store_id_from_page(got.text)
    if not blob or not blob.get("store_id"):
        # Say WHICH wall it was. "no store id in an HTTP 200" reads like the
        # page changed; an unanswered challenge is a different problem with a
        # different fix, and this engine met exactly that before it could
        # answer one.
        if P.is_self_clearing_challenge(got.text):
            raise SystemExit(
                f"{url} is still serving a self-clearing challenge after it "
                f"was answered. Use a browser engine for this locale.")
        raise SystemExit(
            f"could not read a store id out of {url} (HTTP {got.status}, "
            f"{len(got.body)} bytes). Known locales: "
            f"{', '.join(sorted(P.KNOWN_STORES))}.")
    logger.info("Read store %s for locale %r off the storefront",
                blob["store_id"], args.site_locale)
    return load_store(fetch, int(blob["store_id"]))


def run_product_mode(args, fetch, store: P.StoreConfig) -> WalkResult:
    """One product id, through the same endpoint a listing uses.

    There is no separate detail endpoint worth calling: productsArray already
    returns the whole colour and size tree, so `--mode product` differs from
    `--mode category` only in how the id list is chosen.
    """
    result = WalkResult(store=store)
    url = P.products_array_url(store.store_id, store.catalog_id, [args.product_id])
    if not P.is_robots_allowed(url):
        raise RobotsRefusal(url)
    got = fetch(url)
    result.requests_made += 1
    result.bytes_read += len(got.body or b"")
    if P.is_edge_refusal(got.status, got.text[:4000], got.headers):
        raise EdgeRefusal(f"HTTP {got.status} from the edge for a product request")
    parsed = P.parse_products_array(got.body, store, locale=args.site_locale)
    result.rows = parsed.rows
    result.payload_errors = parsed.errors
    result.duplicate_bundles = parsed.duplicate_bundles
    result.products_requested = 1
    if not parsed.rows and parsed.errors:
        result.stop_reason = "product_not_found"
    return result


def scrape(args) -> int:
    pool = proxy_pool.from_args(args)
    fetch = build_fetch(args, pool)
    start_url = P.BASE

    try:
        store = resolve_store(args, fetch)
        if args.list_categories:
            from catalog_walk import load_grids
            for grid in load_grids(fetch, store.store_id):
                print(f"{grid.grid_id}  {grid.trail}")
            return 0
        if args.mode == "product":
            result = run_product_mode(args, fetch, store)
        else:
            on_grid = (lambda: fetch.rotate("per-page rotation")) \
                if args.proxy_rotate == "per-page" else None
            result = crawl(fetch, store.store_id, category=args.category,
                           locale=args.site_locale, max_grids=args.max_grids,
                           max_skus=args.max_skus, delay=args.delay,
                           on_grid=on_grid, store=store)
    except RobotsRefusal as exc:
        print(f"[!] {exc}")
        return EXIT_BLOCKED
    except EdgeRefusal as exc:
        print(f"[!] {exc}")
        print("[!] Change exit — a retry from this address will get the same 403.")
        return EXIT_BLOCKED
    except Exception as exc:  # noqa: BLE001 - reported, then mapped to an exit code
        logger.error("%s: %s", type(exc).__name__, exc)
        return EXIT_REMOTE_API_ERROR

    _report(result)
    return finish_run(
        result.rows, args.out, args.format, args.allow_empty,
        blocked=False, stop_reason=result.stop_reason,
        pages_requested=result.grids_seen or 1,
        pages_completed=result.grids_fetched or 1,
        pages_failed=[f["url"] for f in result.failures] or None,
        scope=scope_fingerprint(
            store_id=result.store.store_id if result.store else None,
            locale=args.site_locale, category=args.category,
            grid_ids=result.grid_ids, max_grids=args.max_grids,
            max_skus=args.max_skus),
        start_url=start_url, final_url=P.BASE, mode=args.mode)


def _report(result: WalkResult) -> None:
    store = result.store
    print(f"[i] store {store.store_id} catalog {store.catalog_id} "
          f"{store.country_code} {store.currency_code}")
    print(f"[i] {result.grids_fetched}/{result.grids_seen} grid(s), "
          f"{result.products_requested} product(s) requested, "
          f"{len(result.rows)} row(s)")
    print(f"[i] {result.requests_made} request(s), "
          f"{result.bytes_read / 1_048_576:.2f} MiB read")
    if result.duplicate_bundles:
        print(f"[i] {result.duplicate_bundles} array entry(ies) pointed at a "
              f"bundle already seen — folded, not dropped silently")
    if result.payload_errors:
        keys = {e.get("key") for e in result.payload_errors}
        print(f"[!] {len(result.payload_errors)} payload error(s) inside HTTP 200: "
              f"{', '.join(sorted(k for k in keys if k))}")
    # Loud, and above the row count in importance: a run with failures has
    # holes in it, and a monitor that reads the rows without reading this
    # will see the missing products as delisted.
    for failure in result.failures:
        print(f"[!] {failure['kind']}"
              f"{' ' + str(failure['status']) if failure['status'] else ''}: "
              f"{failure['detail'][:150]}")
    if result.failures:
        print(f"[!] {len(result.failures)} request(s) failed — this run is NOT a "
              f"complete view of what was asked for.")


def parse_args():
    p = argparse.ArgumentParser(
        description="bershka-scraper — catalogue API engine (no browser). "
                    "This is the primary engine: see the module docstring "
                    "for why a browser buys nothing on this site.")
    browser_bridge.add_core_arguments(p, "bershka_products_api")
    browser_bridge.add_proxy_arguments(p)
    # This engine's own two, which no other engine has:
    p.add_argument("--list-categories", action="store_true",
                   help="Print every grid id and menu trail, then exit.")
    p.add_argument("--user-agent", default=DEFAULT_UA)
    args = p.parse_args()
    env_config.apply(args)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")

    if args.mode == "product" and not args.product_id:
        p.error("--mode product needs --product-id.")
    return args


def main() -> int:
    return scrape(parse_args())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
