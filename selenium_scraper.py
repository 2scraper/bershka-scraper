#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selenium_scraper.py
-------------------
Selenium transport for bershka-scraper.

Read `browser_bridge.py` first: on this site a browser engine opens the
storefront for its session and exit, then issues the same catalogue API calls
`api_scraper.py` makes, from inside the page's origin. The route and the
parsing are shared; this file is a driver and a CLI.

Two limits this engine has and the other two do not, both inherited from
chromedriver rather than chosen here:

* **It cannot attach to an authenticated CDP endpoint.** `debuggerAddress`
  takes a bare `host:port` with nowhere to put credentials, so
  `--cdp-endpoint` with a user and password in it is refused with a message
  saying which engine to use instead — not accepted and then silently
  connected as somebody else.
* **It cannot authenticate a proxy.** Chromium's `--proxy-server` carries no
  credentials and Selenium has no authentication callback. A credentialled
  proxy is therefore refused rather than stripped and used unauthenticated,
  because a run that quietly leaves from the wrong address is worse than one
  that stops.

Both matter more here than in a sibling repo: this site's only real barrier
is the exit address, so an engine that cannot reach the exit you configured
cannot do the job at all.

    python3 selenium_scraper.py --category "WOMEN / SALE" --max-grids 2
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import browser_bridge
import env_config
import proxy_pool
from output_writer import EXIT_REMOTE_API_ERROR, RemoteAPIError

# At module level, deliberately, and not inside start(). The offline suite
# guards `import selenium_scraper` behind try/except ImportError and REPORTS the skip,
# and CI's engine-smoke job imports this module with selenium installed. Both
# only mean something if importing this module actually requires the driver
# (CLAUDE.md §10). Imported inside start(), the module loaded cleanly with no
# selenium at all, so neither check could ever fail.
from selenium import webdriver  # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402

logger = logging.getLogger("selenium_scraper")


class SeleniumDriver:
    name = "selenium"

    def __init__(self, args, exit_url=None):
        self.args = args
        self.exit_url = exit_url
        self.driver = None

    def start(self):
        options = Options()
        if self.args.cdp_endpoint:
            user, _ = proxy_pool.split_credentials(self.args.cdp_endpoint)
            if user:
                raise browser_bridge.BridgeError(
                    "this --cdp-endpoint carries credentials and chromedriver "
                    "has nowhere to put them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated endpoint.")
            options.add_experimental_option(
                "debuggerAddress", self.args.cdp_endpoint.split("//")[-1])
        else:
            if self.args.headless:
                options.add_argument("--headless=new")
            if self.exit_url:
                user, _ = proxy_pool.split_credentials(self.exit_url)
                if user:
                    raise browser_bridge.BridgeError(
                        "this proxy needs a username and password, and Selenium "
                        "cannot supply them. Refusing rather than leaving from "
                        "an address you did not choose. Use playwright_scraper.py "
                        "or puppeteer_scraper.py.")
                options.add_argument(
                    "--proxy-server=" + proxy_pool.to_playwright(self.exit_url)["server"])
        try:
            self.driver = webdriver.Chrome(options=options)
        except Exception as exc:  # noqa: BLE001
            if self.args.cdp_endpoint:
                # The REMOTE browser failing on its own terms. Mapped to
                # RemoteAPIError so the family's exit-code contract can tell
                # 5 from 1 and 3 — the same mapping the other two engines make.
                raise RemoteAPIError(
                    "could not connect to --cdp-endpoint: "
                    f"{type(exc).__name__}: {browser_bridge.mask_text(str(exc))}") from exc
            raise
        self.driver.set_page_load_timeout(self.args.timeout)

    def navigate(self, url):
        self.driver.get(url)
        # Selenium does not expose the response status. Returning None is
        # honest; `browser_bridge.open_storefront` then judges the page by
        # its body, which is what `detect_page_state` does anyway.
        return None

    def content(self):
        return self.driver.page_source

    def evaluate(self, js, arg=None):
        # `execute_script` takes a statement, not an expression, and cannot
        # await, so this goes through the async form. `Promise.resolve(...)`
        # wraps the call because the two things handed to this method are
        # NOT the same shape: the bridge's fetch helper is async, while
        # captcha_solver's discovery script is an ordinary synchronous arrow
        # function. Calling `.then` directly on the second one throws, and
        # the failure surfaces as "captcha detection found nothing" — which
        # reads exactly like a page with no captcha on it.
        return self.driver.execute_async_script(
            "const cb = arguments[arguments.length - 1];"
            "Promise.resolve((" + js + ")(arguments[0])).then(cb).catch("
            "e => cb({status: 0, body: String(e), ok: false}));", arg)

    def set_cookie(self, name, value, url):
        self.driver.add_cookie({"name": name, "value": value, "path": "/"})

    def count(self, selector):
        return len(self.driver.find_elements(By.CSS_SELECTOR, selector))

    def sleep(self, ms):
        time.sleep(ms / 1000.0)

    def stop(self):
        if self.driver:
            self.driver.quit()


def scrape(args) -> int:
    pool = proxy_pool.from_args(args)
    exit_url = pool.current if pool else None
    if exit_url:
        logger.info("Exit: %s", proxy_pool.mask(exit_url))
    return browser_bridge.run(args, SeleniumDriver(args, exit_url))


def parse_args():
    p = argparse.ArgumentParser(
        description="bershka-scraper — Selenium transport. The catalogue "
                    "route lives in catalog_walk.py; this is a driver.")
    browser_bridge.add_core_arguments(p, "bershka_products_selenium")
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
