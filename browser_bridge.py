#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser_bridge.py
-----------------
What the three browser engines have in common, which on this site is almost
everything.

A browser engine here does not read a page. It opens the storefront so a
session exists, then issues the SAME catalogue API calls `api_scraper.py`
makes, from inside the page's own origin. That is the only honest shape
available: a rendered listing contains no products (0 ids, 0 grid classes,
0 JSON-LD in 938,454 bytes, measured 2026-09-19), so there is nothing to
scrape out of the DOM and the browser's contribution is its exit address and
its cookie jar.

Why keep browser engines at all, then
-------------------------------------
Two reasons, both narrow and both stated rather than implied:

* An operator may have a Scraping Browser profile and no proxy. The profile
  is an exit, and this site's only real barrier is the exit.
* If the edge ever starts requiring the session cookies it currently issues
  and ignores, a same-origin `fetch` from a real page already carries them.
  `api_scraper.py` does not.

They are NOT faster, NOT more reliable and NOT more capable than
`api_scraper.py` on this site, and the README says so.

The bridge
----------
`make_fetch(evaluate)` turns "run this JavaScript and give me the result"
into the `fetch(url) -> Fetched` callable `catalog_walk` wants. Every engine
supplies its own one-line `evaluate`; none of them carries a copy of the
route, the batching, the robots check or the parsing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, Optional

import page_flow
import product_parser as P
import proxy_pool
from captcha_solver import (AWS_WAF_COOKIE, INJECT_TOKEN_JS, detect_aws_waf,
                            detect_recaptcha_in_page, detect_recaptcha_v3,
                            reconcile_detections, solve_recaptcha)
from catalog_walk import Fetched

logger = logging.getLogger("browser_bridge")

# Same-origin fetch, returning the status and the body as text.
#
# `credentials: "include"` is deliberate and is the whole point of doing this
# from a page rather than from a socket: the edge sets `ITXSESSIONID` and
# `BSKSESSION` on every response, including the 403, and this is the path
# that would carry them if they ever start mattering. They did not matter on
# 2026-09-19 — a cookie-less `requests` call got 200 — and this comment is
# here so nobody later reads the flag as evidence that they do.
#
# The body is returned as text, not JSON, so a non-JSON wall (a challenge
# page, an HTML error) reaches `detect_page_state` intact instead of being
# swallowed by a parse error inside the browser.
PAGE_FETCH_JS = """
async (url) => {
  try {
    const r = await fetch(url, {credentials: "include",
                               headers: {"Accept": "*/*"}});
    return {status: r.status, body: await r.text(), ok: true};
  } catch (e) {
    return {status: 0, body: String(e), ok: false};
  }
}
"""


# Global on purpose, not a single find/partition — see CLAUDE.md §8: a masker
# that handles the first occurrence prints the password the other four times.
#
# This is NOT `proxy_pool.mask()`, and the difference was paid for live on
# 2026-09-20. `mask()` parses its argument AS a URL and rebuilds it; handed an
# exception message it returned `"?://?"`, which destroyed the only
# description of why a CDP connect had failed. A masker for free text has to
# leave the text alone and redact only what looks like a credential.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def mask_text(text: str) -> str:
    """Redact `user:pass@` inside any URL in free text, keeping the text."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


class BridgeError(RuntimeError):
    """The browser could not perform the fetch at all."""


def make_fetch(evaluate: Callable[[str, str], Any],
               on_challenge: Optional[Callable[[str], bool]] = None
               ) -> Callable[[str], Fetched]:
    """`evaluate(js, url) -> dict` becomes `fetch(url) -> Fetched`.

    `on_challenge` is the challenge ladder, and it is wired here as well as
    into the storefront open for a reason: the storefront can come back clean
    and a LATER same-origin call still meet a wall, because the edge decides
    per request and not per session. A ladder that only runs on the first
    page would leave every subsequent API call to fail as "blocked" with no
    attempt to clear anything.

    It retries the fetch exactly ONCE after a successful clear. Retrying
    further would turn a persistent wall into a loop that spends a solver
    call per turn.
    """

    def raw(url: str) -> Fetched:
        result = evaluate(PAGE_FETCH_JS, url)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                raise BridgeError(
                    f"browser returned non-JSON for {url}: {result[:120]!r}")
        if not isinstance(result, dict):
            raise BridgeError(f"browser returned {type(result).__name__} for {url}")
        if not result.get("ok", True) and not result.get("status"):
            # A same-origin fetch that throws is a transport failure, not a
            # refusal by the site: reported as itself so a run does not read
            # it as a block and rotate exits for nothing.
            raise BridgeError(f"in-page fetch failed for {url}: {result.get('body')}")
        body = result.get("body") or ""
        return Fetched(status=int(result.get("status") or 0),
                       body=body.encode("utf-8", "replace"))

    def fetch(url: str) -> Fetched:
        got = raw(url)
        if on_challenge is None:
            return got
        text = got.text
        # An edge refusal is not a challenge and is left for the caller to
        # raise on: there is nothing in it to solve.
        if P.is_edge_refusal(got.status, text[:4000], got.headers):
            return got
        if P.detect_page_state(text, got.status, url=url) == "content":
            return got
        if on_challenge(url):
            logger.info("cleared a challenge met on %s; retrying the call once", url)
            return raw(url)
        return got

    return fetch


def open_storefront(navigate: Callable[[str], Optional[int]],
                    content: Callable[[], str],
                    sleep: Callable[[int], None],
                    locale: str = P.DEFAULT_LOCALE,
                    count: Optional[Callable[[str], int]] = None,
                    mode: str = "category",
                    driver=None, args=None) -> str:
    """Land on the storefront, waiting out a self-clearing challenge if one
    appears, and return the settled HTML.

    The wait is `page_flow.wait_out_self_clearing_challenge`, not a copy of
    it, and it is a POSITIVE wait — it stops when the page looks like
    content, not when the challenge markers vanish. The distinction was paid
    for in a sibling repo, where waiting for the markers to disappear handed
    the parser an intermediate fingerprinting document and a row of nulls.

    An edge refusal is NOT waited out. It is raised, because the remedy is a
    different exit and 30 seconds of polling only delays finding that out.
    """
    url = f"{P.BASE}/{locale}/"
    if not P.is_robots_allowed(url):
        raise BridgeError(f"robots.txt disallows {url}")
    status = navigate(url)
    html = content() or ""

    if P.is_edge_refusal(status, html[:4000]):
        raise BridgeError(
            f"HTTP {status} from the edge for {url} — the exit address was "
            f"refused. A different exit is the fix; waiting is not.")

    # Rung 1: let the browser clear it on its own terms, on a bounded budget.
    if P.is_self_clearing_challenge(html):
        logger.info("self-clearing challenge on %s; giving it "
                    "%d ms", url, page_flow.CHALLENGE_SETTLE_MS)
        html, cleared = page_flow.wait_out_self_clearing_challenge(
            content, sleep, url=url, log=logger.info)
    else:
        cleared = True

    # Rung 2: only now, and only if rung 1 did not produce content.
    if driver is not None and not cleared:
        if handle_challenge(driver, args, url):
            html = content() or html
            cleared = P.detect_page_state(html, 200, url=url) == "content"
    if not cleared:
        raise BridgeError(
            f"challenge on {url} did not clear: the browser did not settle it "
            f"and the solver fallback did not either")

    # Readiness, through page_flow's shared wait rather than a sleep. What it
    # confirms here is narrow and worth naming: that the storefront's own
    # assets are present, i.e. a real page arrived rather than a wall. It is
    # NOT waiting for products — there are none in the DOM to wait for.
    #
    # A wait that times out is not a failure: `detect_page_state` has the
    # final say below, and a served page with an unusual asset layout should
    # not stop a run that can already see the configuration blob.
    if count is not None:
        seen = page_flow.wait_for_count(
            count, sleep, page_flow.ready_selector(mode),
            page_flow.ready_count(mode), page_flow.content_timeout_ms(mode))
        if seen < page_flow.ready_count(mode):
            logger.info("storefront readiness wait saw %d of %d asset "
                        "reference(s); continuing on the page state instead",
                        seen, page_flow.ready_count(mode))
        html = content() or html

    state = P.detect_page_state(html, status, url=url)
    if state != "content":
        raise BridgeError(
            f"{url} did not come back as a storefront (state={state}, "
            f"HTTP {status}, {len(html)} bytes)")
    return html


# ---------------------------------------------------------------------------
# The challenge ladder
# ---------------------------------------------------------------------------
# Three rungs, in this order, and the order is the point:
#
#   1. **The browser clears it itself.** Over the Scraping Browser API that
#      is the vendor's own extension; on a self-clearing Akamai interstitial
#      it is the page reloading itself once its arithmetic passes. This costs
#      nothing and is the documented primary path.
#   2. **The solver API buys an answer.** Only if rung 1 did not clear it.
#      Firing the paid call on DETECTION rather than on failure means the
#      fallback always wins the race and the primary is never exercised —
#      which is how a repo ends up paying for every page and never noticing
#      that its free path broke.
#   3. **Give up and say which wall it was.** A run that reports success with
#      a row of nulls is worse than one that stops.
#
# The edge refusal is NOT on this ladder at all. It is a 195-byte 403 keyed
# on the exit address, with no challenge in it to solve and nothing to wait
# for; `open_storefront` raises on it before any of this runs. Buying a
# captcha answer for it would be paying for a token no page asked for.
#
# What has actually been seen on this host: ZERO challenges of any kind, over
# every request this repo has made. That is why the detectors below stay
# BROAD rather than being narrowed to what has been measured — the family
# rule is that detection is not tuned down to the sample you happen to have.
# `detect_aws_waf` in particular cannot fire here (this edge is Akamai, not
# AWS) and is left wired anyway, because the cost of a detector that never
# fires is one function call and the cost of a missing one is a silent run of
# nulls.


def handle_challenge(driver, args, url: str, ready_selector: str = "") -> bool:
    """Rung 2. True if something was solved and the page was reloaded.

    Called only after rung 1 has had its chance. Returns False — not an
    exception — when there is nothing to solve, no key to solve it with, or
    the solve failed: every one of those leaves the caller holding whatever
    the page already has, which `detect_page_state` then judges.
    """
    if getattr(args, "solve_captcha", "when-blocked") == "never":
        logger.info("--solve-captcha never: not calling the solver for %s", url)
        return False

    html = driver.content() or ""
    if not html:
        return False

    # Content already present means rung 1 worked. Solving now would buy an
    # answer to a question that has already been answered.
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"
    already_content = P.detect_page_state(html, 200, url=url) == "content"
    if when_blocked and already_content:
        return False

    challenge = detect_aws_waf(html, url)
    if challenge is None:
        from_html = detect_recaptcha_v3(html, url)
        in_page = detect_recaptcha_in_page(
            lambda js: driver.evaluate(js, None), page_url=url)
        challenge = reconcile_detections(from_html, in_page)
    if not challenge:
        return False

    key = getattr(args, "twocaptcha_key", None)
    if not key:
        logger.warning(
            "%s detected via %s on %s, and no TWOCAPTCHA_KEY is set to solve "
            "it with. Continuing with whatever the page already holds.",
            challenge.kind, challenge.source, url)
        return False

    logger.warning("%s detected via %s (sitekey=%s) — the browser did not "
                   "clear it, falling back to the solver API.",
                   challenge.kind, challenge.source, challenge.sitekey)
    try:
        token = solve_recaptcha(challenge, key,
                               api_version=getattr(args, "captcha_api", "v2"),
                               min_score=getattr(args, "min_score", 0.7),
                               proxy=getattr(args, "proxy", None))
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        logger.error("Solving failed (%s) — continuing with the page as it is.",
                     mask_text(str(exc)))
        return False

    if challenge.is_aws_waf:
        # AWS WAF reads its answer from a COOKIE, not a form field, so there
        # is no `g-recaptcha-response` to fill. Kept for the same reason the
        # detector is kept; if it ever fires here, the handling exists.
        driver.set_cookie(AWS_WAF_COOKIE, token, url)
    else:
        driver.evaluate(INJECT_TOKEN_JS, token)
    driver.sleep(1500)
    driver.navigate(url)
    return True


def store_from_storefront(html: str, locale: str) -> Dict[str, Any]:
    """The store id for a locale, read off the page the browser just opened.

    A browser engine is already on the storefront, so it reads the real
    `var inditex={...}` blob rather than consulting
    `product_parser.KNOWN_STORES` — which holds exactly one measured entry.
    """
    blob = P.store_id_from_page(html)
    if not blob or not blob.get("store_id"):
        known = P.KNOWN_STORES.get(locale)
        if known:
            logger.warning("no inditex blob on the storefront; falling back to "
                           "the measured store id for %r", locale)
            return dict(known)
        raise BridgeError(
            f"no store id on the {locale!r} storefront and none measured for it")
    return blob


# ---------------------------------------------------------------------------
# The shared run
# ---------------------------------------------------------------------------
# All three browser engines do exactly this, and differ only in the driver
# they hand it. Three copies of this function is how two of them end up with
# a different stop reason for the same condition and nothing offline notices
# — the same reasoning as page_flow's callables, one level up.

def run(args, driver) -> int:
    """Drive one browser engine end to end and return its exit code.

    `driver` is an object with `start()`, `navigate(url)`, `content()`,
    `evaluate(js, arg)`, `sleep(ms)` and `stop()`.
    """
    import catalog_walk
    from output_writer import EXIT_BLOCKED, EXIT_REMOTE_API_ERROR, finish_run

    start_url = f"{P.BASE}/{args.site_locale}/"
    try:
        driver.start()
    except Exception as exc:  # noqa: BLE001 — mapped to an exit code, not swallowed
        logger.error("could not start %s: %s: %s", driver.name, type(exc).__name__, exc)
        return EXIT_REMOTE_API_ERROR

    try:
        html = open_storefront(driver.navigate, driver.content, driver.sleep,
                               args.site_locale, count=driver.count,
                               mode=args.mode, driver=driver, args=args)
        blob = store_from_storefront(html, args.site_locale)
        fetch = make_fetch(
            driver.evaluate,
            on_challenge=lambda url: handle_challenge(driver, args, url))

        if args.mode == "product":
            from api_scraper import run_product_mode
            store = catalog_walk.load_store(fetch, int(blob["store_id"]))
            result = run_product_mode(args, fetch, store)
        else:
            result = catalog_walk.crawl(
                fetch, int(blob["store_id"]), category=args.category,
                locale=args.site_locale, max_grids=args.max_grids,
                max_products=args.max_products, delay=args.delay)
    except catalog_walk.RobotsRefusal as exc:
        print(f"[!] {exc}")
        return EXIT_BLOCKED
    except (catalog_walk.EdgeRefusal, BridgeError) as exc:
        print(f"[!] {exc}")
        return EXIT_BLOCKED
    except Exception as exc:  # noqa: BLE001
        logger.error("%s: %s", type(exc).__name__, exc)
        return EXIT_REMOTE_API_ERROR
    finally:
        try:
            driver.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s did not shut down cleanly: %s", driver.name, exc)

    from api_scraper import _report
    _report(result)
    return finish_run(
        result.rows, args.out, args.format, args.allow_empty,
        blocked=False, stop_reason=result.stop_reason,
        pages_requested=result.grids_seen or 1,
        pages_completed=result.grids_fetched or 1,
        start_url=start_url, final_url=P.BASE, mode=args.mode)


def add_core_arguments(p, default_out: str):
    """The flags EVERY engine in this repo shares.

    Defined once so `smoke_test.test_engine_parity` compares five engines
    against one definition instead of five hand-kept copies — the same rule
    CLAUDE.md §11 states for workflow steps, applied to a CLI.
    """
    p.add_argument("--mode", choices=["category", "product"], default="category")
    p.add_argument("--category", default=None,
                   help="Match against the menu trail, e.g. 'WOMEN / SALE'.")
    p.add_argument("--product-id", type=int, default=None,
                   help="For --mode product. This is the id a product URL "
                        "carries, NOT a sku.")
    p.add_argument("--site-locale", default=P.DEFAULT_LOCALE)
    p.add_argument("--max-grids", type=int, default=None)
    p.add_argument("--max-products", type=int, default=None,
                   help="Stop once this many rows exist. Rows are per SKU, so "
                        "one dress in 4 colours and 8 sizes is 32 of them.")
    p.add_argument("--delay", type=float, default=0.5)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default=default_out)
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p


def add_proxy_arguments(p):
    """Exit selection, for the engines that choose their own."""
    p.add_argument("--proxy", default=None,
                   help="A single exit. Prefer BERSHKA_PROXY in .env — a "
                        "proxy URL on the command line is visible to `ps`.")
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-rotate", choices=proxy_pool.ROTATE_MODES,
                   default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-sessions", type=int, default=None)
    return p


def add_captcha_arguments(p):
    """The challenge ladder's own flags, shared by every browser engine.

    `--solve-captcha when-blocked` is the default and is what keeps the paid
    rung a FALLBACK: with `always` the solver fires on detection even when
    the browser had already cleared the page, which spends money to learn
    nothing.
    """
    p.add_argument("--twocaptcha-key", default=None,
                   help="2captcha.com API key for the solver fallback. Prefer "
                        "TWOCAPTCHA_KEY in .env — `ps` reads argv.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always", "never"],
                   default="when-blocked",
                   help="when-blocked (default): only after the browser has "
                        "failed to clear it. always: also when content is "
                        "already present. never: no paid call at all.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to ask the solver for.")
    return p


def add_browser_arguments(p):
    """Flags that only mean something when a browser is involved."""
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to a running browser over CDP instead of "
                        "launching one. Falls back to $BERSHKA_CDP_ENDPOINT "
                        "in .env — never pass a credential on the command "
                        "line, `ps` reads argv.")
    return p
