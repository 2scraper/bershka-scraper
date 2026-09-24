#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
playwright_scraper.py
---------------------
Playwright transport for bershka-scraper.

Read `browser_bridge.py` first: on this site a browser engine does not read a
page. It opens the storefront so a session and an exit exist, then issues the
same catalogue API calls `api_scraper.py` makes, from inside the page's own
origin. The route, the batching, the robots enforcement and the parsing all
live in `catalog_walk.py` and `product_parser.py`; this file is the driver
and the CLI and nothing else.

`api_scraper.py` is the engine to reach for on this site. This one exists for
the operator who has a Scraping Browser profile and no proxy — see
`browser_bridge`'s docstring for the two narrow reasons a browser is worth
its cost here.

    python3 playwright_scraper.py --category "WOMEN / SALE" --max-grids 2
    python3 playwright_scraper.py --cdp-endpoint "$BERSHKA_CDP_ENDPOINT"
"""

from __future__ import annotations

import argparse
import logging
import sys

import browser_bridge
import env_config
import product_parser as P
import proxy_pool
from output_writer import EXIT_REMOTE_API_ERROR, RemoteAPIError

# At module level, deliberately, and not inside start(). The offline suite
# guards `import playwright_scraper` behind try/except ImportError and REPORTS the skip,
# and CI's engine-smoke job imports this module with playwright installed. Both
# only mean something if importing this module actually requires the driver
# (CLAUDE.md §10). Imported inside start(), the module loaded cleanly with no
# playwright at all, so neither check could ever fail.
from playwright.sync_api import sync_playwright  # noqa: E402

logger = logging.getLogger("playwright_scraper")


class PlaywrightDriver:
    name = "playwright"

    def __init__(self, args, exit_url=None):
        self.args = args
        self.exit_url = exit_url
        self._pw = self._browser = self._context = self._page = None

    def start(self):
        self._pw = sync_playwright().start()
        if self.args.cdp_endpoint:
            try:
                self._browser = self._pw.chromium.connect_over_cdp(self.args.cdp_endpoint)
            except Exception as exc:  # noqa: BLE001
                # The REMOTE service failing on its own terms, not the site
                # blocking a page. Raised as RemoteAPIError so the family's
                # exit-code contract can tell 5 from 1 and 3 — see
                # output_writer.RemoteAPIError.
                raise RemoteAPIError(
                    "could not connect to --cdp-endpoint: "
                    f"{type(exc).__name__}: {browser_bridge.mask_text(str(exc))}") from exc
            # A connected browser already has its own context; making a new
            # one would leave the profile's cookies and exit behind.
            self._context = (self._browser.contexts[0] if self._browser.contexts
                             else self._browser.new_context())
        else:
            launch = {"headless": self.args.headless}
            proxy = proxy_pool.to_playwright(self.exit_url)
            if proxy:
                launch["proxy"] = proxy
            self._browser = self._pw.chromium.launch(**launch)
            self._context = self._browser.new_context()
        self._page = self._context.new_page()

    def navigate(self, url):
        response = self._page.goto(url, wait_until="domcontentloaded",
                                   timeout=self.args.timeout * 1000)
        return response.status if response else None

    def content(self):
        return self._page.content()

    def evaluate(self, js, arg=None):
        return self._page.evaluate(js, arg)

    def set_cookie(self, name, value, url):
        from urllib.parse import urlparse
        self._context.add_cookies([{
            "name": name, "value": value,
            "domain": urlparse(url).hostname or "", "path": "/"}])

    def count(self, selector):
        return len(self._page.query_selector_all(selector))

    def sleep(self, ms):
        self._page.wait_for_timeout(ms)

    def stop(self):
        for closer in (self._context, self._browser):
            if closer:
                closer.close()
        if self._pw:
            self._pw.stop()


def scrape(args) -> int:
    pool = proxy_pool.from_args(args)
    exit_url = pool.current if pool else None
    if exit_url:
        logger.info("Exit: %s", proxy_pool.mask(exit_url))
    return browser_bridge.run(args, PlaywrightDriver(args, exit_url))


def parse_args():
    p = argparse.ArgumentParser(
        description="bershka-scraper — Playwright transport. The catalogue "
                    "route lives in catalog_walk.py; this is a driver.")
    browser_bridge.add_core_arguments(p, "bershka_products_playwright")
    browser_bridge.add_proxy_arguments(p)
    browser_bridge.add_browser_arguments(p)
    browser_bridge.add_captcha_arguments(p)
    args = p.parse_args()
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "twocaptcha_key",
        "BERSHKA_CDP_ENDPOINT": "cdp_endpoint",
        "BERSHKA_PROXY": "proxy",
    })
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.mode == "product" and not args.product_id:
        p.error("--mode product needs --product-id.")
    return args


def main() -> int:
    return scrape(parse_args())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RemoteAPIError as exc:
        # 5, not 1 and not 3: the remote service failed, the site did not
        # refuse anything, and the run did not crash.
        print(f"[!] {exc}")
        sys.exit(EXIT_REMOTE_API_ERROR)
    except proxy_pool.ProxyError as exc:
        print(f"[!] {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(1)
