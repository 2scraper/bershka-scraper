#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scraper_api_client.py
---------------------
The fourth transport: 2captcha's Scraper API, which fetches over plain HTTPS
from its own exits with no local browser and no proxy of your own.

Same catalogue route, same parser, same row schema and same exit codes as the
other three engines — `catalog_walk.py` does the walking and this file only
turns a URL into bytes.

When this is the right tool
---------------------------
When you have a 2captcha key and no exit of your own. This site's one real
barrier is the exit address (every URL answered 403 from a residential
address on 2026-09-19, `/robots.txt` included), and this API supplies one
without a proxy list or a browser profile.

What it cannot do
-----------------
It gets one response per call and cannot wait anything out. That is a real
limit on a site with a self-clearing interstitial — but it is a smaller limit
here than in a sibling repo, because the catalogue endpoints this repo calls
are JSON and a challenge served in place of JSON is detected by
`detect_page_state` on the first response rather than waited out.

It also costs a billable task per call, and this repo's walk is call-heavy by
design: a grid plus its products is at least two, and a 200-product category
is six. `--max-grids` and `--max-products` are not decoration.

    python3 scraper_api_client.py --category "WOMEN / SALE" --max-grids 1

Prefer $TWOCAPTCHA_KEY in .env over --key: a key on the command line is
visible to anyone who can run `ps`, and it lands in shell history.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys

import requests

import browser_bridge
import catalog_walk
import env_config
import product_parser as P
from catalog_walk import Fetched
from output_writer import (EXIT_BLOCKED, EXIT_REMOTE_API_ERROR, finish_run,
                           scope_fingerprint)

logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s.
MAX_API_TIMEOUT = 120

# Global on purpose, not a single find/partition — see CLAUDE.md §8: a masker
# that handles the first occurrence prints the password the other four times
# and looks like it is working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def make_fetch(args):
    """A `fetch(url) -> Fetched` backed by one billable task per call."""

    def fetch(url: str) -> Fetched:
        payload = {
            "task_type": "scrape",
            "url": url,
            # `raw` because the catalogue endpoints return JSON and any
            # cleaning would be a lossy re-encoding of a payload the parser
            # reads field by field.
            "data_format": "raw",
            "format": "json",
            "timeout": min(args.timeout, MAX_API_TIMEOUT),
        }
        if args.proxy_country:
            payload["country"] = args.proxy_country
        if args.cdp_url:
            payload["cdpurl"] = args.cdp_url
            logger.info("Routing through an existing browser session: %s",
                        _mask_credentials(args.cdp_url))

        response = requests.post(
            SYNC_ENDPOINT,
            headers={"Authorization": f"Bearer {args.key}",
                     "Content-Type": "application/json"},
            json=payload,
            # More headroom than the API-side task timeout, so a task that
            # legitimately runs the full 120s does not look like a local
            # network failure.
            timeout=min(args.timeout, MAX_API_TIMEOUT) + 30)

        debug = response.headers.get("x-debug")
        if debug:
            logger.info("x-debug: %s", debug)

        if response.status_code != 200:
            # The API's OWN call failing, not the site refusing a page:
            # 422 a task that ran and errored, 402 out of balance, 408 the
            # sync wait exceeded. Mapped to EXIT_REMOTE_API_ERROR upstream.
            raise RuntimeError(
                f"Scraper API returned HTTP {response.status_code}: "
                f"{response.text[:400]}")

        body = response.json()
        upstream = body.get("status")
        text = body.get("body") or ""
        headers = body.get("headers") or {}
        logger.info("upstream %s, %d bytes", upstream, len(text))
        return Fetched(status=int(upstream or 0),
                       body=text.encode("utf-8", "replace"),
                       headers={str(k): str(v) for k, v in headers.items()})

    return fetch


def scrape(args) -> int:
    fetch = make_fetch(args)
    start_url = P.BASE
    try:
        known = P.KNOWN_STORES.get(args.site_locale)
        if known:
            store_id = known["store_id"]
        else:
            got = fetch(f"{P.BASE}/{args.site_locale}/")
            blob = P.store_id_from_page(got.text)
            if not blob or not blob.get("store_id"):
                print(f"[!] no store id on the {args.site_locale!r} storefront")
                return EXIT_BLOCKED
            store_id = int(blob["store_id"])

        if args.mode == "product":
            from api_scraper import run_product_mode
            store = catalog_walk.load_store(fetch, store_id)
            result = run_product_mode(args, fetch, store)
        else:
            result = catalog_walk.crawl(
                fetch, store_id, category=args.category, locale=args.site_locale,
                max_grids=args.max_grids, max_skus=args.max_skus,
                delay=args.delay)
    except catalog_walk.RobotsRefusal as exc:
        print(f"[!] {exc}")
        return EXIT_BLOCKED
    except (catalog_walk.EdgeRefusal, browser_bridge.BridgeError) as exc:
        print(f"[!] {exc}")
        return EXIT_BLOCKED
    except Exception as exc:  # noqa: BLE001
        logger.error("%s: %s", type(exc).__name__, _mask_credentials(str(exc)))
        return EXIT_REMOTE_API_ERROR

    from api_scraper import _report
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


def parse_args():
    p = argparse.ArgumentParser(
        description="bershka-scraper — 2captcha Scraper API transport "
                    "(no local browser, no proxy of your own).")
    browser_bridge.add_core_arguments(p, "bershka_products_scraperapi")
    # This transport's own flags. `--key` rather than a positional so the env
    # fallback below is the documented path.
    p.add_argument("--key", default=None,
                   help="2captcha.com API key, sent as a Bearer token. Falls "
                        "back to $TWOCAPTCHA_KEY via .env, which is safer: "
                        "`ps` reads argv.")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session "
                        "(the API's `cdpurl`). Not needed on this site.")
    p.add_argument("--proxy-country", default=None,
                   help="Pin the API's exit country, e.g. gb.")
    args = p.parse_args()
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "BERSHKA_CDP_ENDPOINT": "cdp_url",
    })
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.key:
        p.error("no --key given, and TWOCAPTCHA_KEY is not set in the "
                "environment or in .env.")
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
