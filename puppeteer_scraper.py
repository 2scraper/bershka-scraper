#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
puppeteer_scraper.py
--------------------
pyppeteer transport for bershka-scraper.

Read `browser_bridge.py` first: on this site a browser engine opens the
storefront for its session and exit, then issues the same catalogue API calls
`api_scraper.py` makes, from inside the page's origin. The route and the
parsing are shared; this file is a driver and a CLI.

"The Puppeteer build" here means **pyppeteer**, a Python port of Puppeteer's
API rather than a Node process — the same choice the sibling repos made, kept
so the three engines can be compared under one interpreter.

pyppeteer is effectively unmaintained (its own README points at Playwright)
and its pins conflict with playwright's and selenium's. Install it in its own
virtualenv, which is what CI does.

pyppeteer is async and everything above it here is not, so this driver owns a
private event loop and runs each call to completion on it. That is the whole
of the asymmetry: one loop, created in `start()`, closed in `stop()`.

    python3 puppeteer_scraper.py --category "WOMEN / SALE" --max-grids 2
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import browser_bridge
import env_config
import proxy_pool
from output_writer import EXIT_REMOTE_API_ERROR, RemoteAPIError

# At module level, deliberately, and not inside start(). The offline suite
# guards `import puppeteer_scraper` behind try/except ImportError and REPORTS the skip,
# and CI's engine-smoke job imports this module with pyppeteer installed. Both
# only mean something if importing this module actually requires the driver
# (CLAUDE.md §10). Imported inside start(), the module loaded cleanly with no
# pyppeteer at all, so neither check could ever fail.
from pyppeteer import connect, launch  # noqa: E402

logger = logging.getLogger("puppeteer_scraper")


class PuppeteerDriver:
    name = "pyppeteer"

    def __init__(self, args, exit_url=None):
        self.args = args
        self.exit_url = exit_url
        self._loop = None
        self._browser = self._page = None

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def start(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        if self.args.cdp_endpoint:
            try:
                self._browser = self._run(connect(browserWSEndpoint=self.args.cdp_endpoint))
            except Exception as exc:  # noqa: BLE001
                raise RemoteAPIError(
                    "could not connect to --cdp-endpoint: "
                    f"{type(exc).__name__}: {browser_bridge.mask_text(str(exc))}") from exc
            pages = self._run(self._browser.pages())
            self._page = pages[0] if pages else self._run(self._browser.newPage())
        else:
            launch_args = []
            if self.exit_url:
                # Chromium takes the server here and the credentials through
                # an authentication callback below; putting a password in
                # --proxy-server would also put it in the process list.
                server = proxy_pool.to_playwright(self.exit_url)["server"]
                launch_args.append(f"--proxy-server={server}")
            self._browser = self._run(launch(headless=self.args.headless,
                                             args=launch_args))
            self._page = self._run(self._browser.newPage())
            user, password = proxy_pool.split_credentials(self.exit_url)
            if user:
                self._run(self._page.authenticate({"username": user,
                                                   "password": password}))

    def navigate(self, url):
        response = self._run(self._page.goto(
            url, waitUntil="domcontentloaded", timeout=self.args.timeout * 1000))
        return response.status if response else None

    def content(self):
        return self._run(self._page.content())

    def evaluate(self, js, arg=None):
        return self._run(self._page.evaluate(js, arg))

    def set_cookie(self, name, value, url):
        from urllib.parse import urlparse
        self._run(self._page.setCookie({
            "name": name, "value": value,
            "domain": urlparse(url).hostname or "", "path": "/"}))

    def count(self, selector):
        return len(self._run(self._page.querySelectorAll(selector)))

    def sleep(self, ms):
        self._run(asyncio.sleep(ms / 1000.0))

    def stop(self):
        if self._browser:
            self._run(self._browser.close())
        if self._loop:
            self._loop.close()


def scrape(args) -> int:
    pool = proxy_pool.from_args(args)
    exit_url = pool.current if pool else None
    if exit_url:
        logger.info("Exit: %s", proxy_pool.mask(exit_url))
    return browser_bridge.run(args, PuppeteerDriver(args, exit_url))


def parse_args():
    p = argparse.ArgumentParser(
        description="bershka-scraper — pyppeteer transport. The catalogue "
                    "route lives in catalog_walk.py; this is a driver.")
    browser_bridge.add_core_arguments(p, "bershka_products_puppeteer")
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
        print(f"[!] {exc}")
        sys.exit(EXIT_REMOTE_API_ERROR)
    except proxy_pool.ProxyError as exc:
        print(f"[!] {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(1)
