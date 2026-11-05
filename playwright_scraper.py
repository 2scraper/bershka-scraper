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
import fingerprint_client
import product_parser as P
import proxy_pool
from output_writer import EXIT_REMOTE_API_ERROR, RemoteAPIError

# At module level, deliberately, and not inside start(). The offline suite
# guards `import playwright_scraper` behind try/except ImportError and REPORTS the skip,
# and CI's engine-smoke job imports this module with playwright installed. Both
# only mean something if importing this module actually requires the driver.
# Imported inside start(), the module loaded cleanly with no
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
            # A fingerprint, when asked for, is applied HERE only: over CDP
            # the remote browser brings its own.
            fp = self._fingerprint() if getattr(self.args, "fingerprint", False) else None
            ctx = {}
            if fp:
                ctx.update(fingerprint_client.playwright_context_kwargs(fp))
            self._context = self._browser.new_context(**ctx)
            if fp:
                self._context.add_init_script(fingerprint_client.playwright_init_script(fp))
        self._page = self._context.new_page()
        if self._fp is not None:
            self._apply_ua_override(self._page, self._fp)

    # --- 2Captcha fingerprint (local browser only) ---------------------------
    # Fetched once per run; each fetch is billed.
    _fp = None
    fingerprint_facts = None

    def _fingerprint(self):
        if self._fp is None:
            # FingerprintError propagates: run() reports it and exits 5 before
            # any page is fetched without the identity that was asked for.
            self._fp = fingerprint_client.get_fingerprint(
                self.args.twocaptcha_key, tags=self.args.fp_tags,
                country=self.args.fp_country, refresh=self.args.fp_refresh)
            self.fingerprint_facts = {
                "id": self._fp.get("id"), "country": self._fp.get("country"),
                "user_agent": fingerprint_client.fingerprint_user_agent(self._fp),
                "not_in_response": fingerprint_client.missing_fields(self._fp),
                "client_hints": False, "readback": None, "mismatches": None,
            }
            print("[i] fingerprint %s (%s): %s" % (
                self._fp.get("id"), self._fp.get("country"),
                (self.fingerprint_facts["user_agent"] or "")[:90]), file=sys.stderr)
            if self.fingerprint_facts["not_in_response"]:
                print("[!] fingerprint fields not in the response, left as the "
                      "browser's own: %s" % ", ".join(self.fingerprint_facts["not_in_response"]),
                      file=sys.stderr)
        return self._fp

    def _apply_ua_override(self, page, fp):
        """User agent WITH client hints, so Sec-CH-UA* and userAgentData match
        the User-Agent header. Per page: CDP emulation is per target."""
        override = fingerprint_client.user_agent_override(fp)
        if not override:
            return
        try:
            self._context.new_cdp_session(page).send("Emulation.setUserAgentOverride", override)
            self.fingerprint_facts["client_hints"] = True
        except Exception as exc:  # noqa: BLE001
            print("[!] could not set the fingerprint's client hints: %s"
                  % str(exc).splitlines()[0][:120], file=sys.stderr)

    def _read_back_fingerprint(self):
        """Once, after the first page: what the live page reports, compared
        with the fingerprint. Recorded in the run metadata."""
        facts = self.fingerprint_facts
        if not self._fp or facts is None or facts.get("readback") is not None:
            return
        try:
            seen = self._page.evaluate(fingerprint_client.READBACK_JS)
        except Exception as exc:  # noqa: BLE001
            facts["readback"] = {"error": str(exc).splitlines()[0][:120]}
            return
        facts["readback"] = seen
        facts["mismatches"] = fingerprint_client.readback_mismatches(self._fp, seen)
        if facts["mismatches"]:
            print("[!] the page does not report the fingerprint for: %s"
                  % "; ".join(facts["mismatches"]), file=sys.stderr)

    def navigate(self, url):
        response = self._page.goto(url, wait_until="domcontentloaded",
                                   timeout=self.args.timeout * 1000)
        if self._fp is not None:
            self._read_back_fingerprint()
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


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="bershka-scraper — Playwright transport. The catalogue "
                    "route lives in catalog_walk.py; this is a driver.")
    browser_bridge.add_core_arguments(p, "bershka_products_playwright")
    browser_bridge.add_proxy_arguments(p)
    browser_bridge.add_browser_arguments(p)
    browser_bridge.add_captcha_arguments(p)
    # Playwright only: the other two engines do not apply a fingerprint, and
    # argparse refuses the flag there (exit 2) instead of ignoring it.
    fingerprint_client.add_arguments(p)
    args = p.parse_args(argv)
    # Read before env_config fills it: a typed endpoint and one from .env are
    # treated differently under --fingerprint.
    typed_cdp = args.cdp_endpoint
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "twocaptcha_key",
        "BERSHKA_CDP_ENDPOINT": "cdp_endpoint",
        "BERSHKA_PROXY": "proxy",
    })
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.mode == "product" and not args.product_id:
        p.error("--mode product needs --product-id.")
    if args.fingerprint:
        # A fingerprint is for a browser launched HERE; never stacked on the
        # Scraping Browser API, which brings its own.
        if typed_cdp:
            p.error("--fingerprint applies to a locally launched browser and "
                    "cannot be combined with --cdp-endpoint: the Scraping "
                    "Browser API brings its own fingerprint.")
        if args.cdp_endpoint:
            print("[i] --fingerprint: ignoring BERSHKA_CDP_ENDPOINT from the "
                  "environment and launching a local browser.", file=sys.stderr)
            args.cdp_endpoint = None
        if not args.twocaptcha_key:
            p.error("--fingerprint needs TWOCAPTCHA_KEY (in .env or the "
                    "environment): the Fingerprint API uses that key, on a "
                    "separate subscription.")
    return args


def main(argv=None) -> int:
    return scrape(parse_args(argv))


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
