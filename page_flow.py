"""
page_flow.py
------------
The readiness and challenge waits the three browser engines share, and the
paid-solve allowance every engine that can buy a captcha answer goes through.

A browser engine here does not read products out of a page: a rendered
Bershka listing carries none (0 product ids, 0 JSON-LD blocks in 938,454
bytes, measured 2026-09-19). It opens the storefront so a session exists and
then calls the catalogue API from inside it. So the only things worth waiting
for are "the storefront arrived" and "a self-clearing challenge became the
storefront", and both waits live here once rather than in each engine.

The functions are either pure or driven through small callables, so each
engine passes its own driver's primitives and no JavaScript crosses this
boundary -- which keeps a site whose CSP lacks `unsafe-eval` from turning a
readiness wait into a crash.

Whether a RESPONSE is usable (status, edge refusal, JSON shape) is decided in
`catalog_walk._get` and `_json_or_raise`, which every transport shares; there
is no second policy table here.
"""

import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger("page_flow")


# What "the storefront has arrived" means, per mode.
#
# There is no product to wait for. Measured 2026-09-19, a fully rendered
# `/gb/women.html` (938,454 bytes) contains 0 product ids, 0 grid classes and
# 0 JSON-LD blocks — the grid is fetched as JSON after load and this repo
# fetches that JSON itself.
#
# So readiness here means "the storefront is up and the session exists",
# which is all a browser engine needs before it starts issuing same-origin
# API calls. The anchors are the two things a served Bershka page always has
# and a challenge page never does: the `var inditex={...}` configuration blob
# and a link to the static asset host. Both are confirmed by
# `product_parser.detect_page_state`, which is what the engines actually
# call; these selectors exist so the shared readiness wait has something
# countable to count.
READY_SELECTORS = {
    "category": "script[src*='static.bershka.net'], link[href*='static.bershka.net']",
    "product": "script[src*='static.bershka.net'], link[href*='static.bershka.net']",
}

# 1, which `ready_count` turns into "wait until 2 matches are visible". The
# floor is deliberately the lowest the family allows: the thing being
# confirmed is binary — the storefront loaded, or it did not — and a higher
# threshold would be a number chosen to look rigorous against an asset count
# nobody has measured. A served page carries both a script and a stylesheet
# from the static host, so 2 is reached immediately or not at all.
MIN_ROW_MATCHES = {"category": 1, "product": 1}

CONTENT_TIMEOUT_MS = {"category": 20000, "product": 15000}


def ready_selector(mode: str) -> str:
    return READY_SELECTORS.get(mode, READY_SELECTORS["category"])


def min_matches(mode: str) -> int:
    return MIN_ROW_MATCHES.get(mode, 2)


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS.get(mode, 20000)


def ready_count(mode: str) -> int:
    """How many matches the readiness wait must SEE before it stops.

    `min_matches` is a FLOOR a page must clear -- "more than three rows", not
    "three" -- which is what makes it immune to resolving on one unrelated
    row. `wait_for_count` below is written the other way round, waiting until
    it has seen at least `minimum`, because that is the shape the rest of the
    family uses. This converts between the two in ONE place rather than
    leaving a `+ 1` in each of the three engines, where the first one to lose
    it would wait on a different threshold than the other two and nothing
    offline would notice.
    """
    return min_matches(mode) + 1


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 250) -> int:
    """Poll `selector` until `minimum` elements match, or the budget runs out.

    Polls a COUNT rather than waiting on an evaluated string. Playwright's
    `wait_for_function` and pyppeteer's `waitForFunction` both hand the
    browser a STRING to evaluate, which a site whose CSP lacks `unsafe-eval`
    refuses outright -- elsewhere that has been an
    `EvalError` and exit 1 on the site's most obvious URL, reproducing on one
    of its two listing routes and not the other, because the two are served
    by different renderers with different headers.

    Bershka's own CSP was not the reason this was written this way and is
    not being worked around here; this is the eval simply not being invited back
    in when a header changes. `querySelectorAll` through the protocol is a
    CDP call, works under any CSP, and spells the same in all three drivers
    -- which is why all three `_driver()` adapters already expose `count`.

    `count` is expected to swallow its own driver errors and return 0: this
    polls a page that may be navigating under it, and an exception from the
    500th millisecond of a 20-second wait should read as "nothing there yet",
    not take the run down.

    Returns the last count seen, so a caller can tell "painted" from "timed
    out with three of them".
    """
    waited = 0
    seen = count(selector)
    while seen < minimum and waited < timeout_ms:
        sleep(poll_ms)
        waited += poll_ms
        seen = count(selector)
    if seen < minimum:
        logger.info("readiness wait ended at %d/%d matches for %s after %dms",
                    seen, minimum, selector, waited)
    return seen


# How long an engine spends waiting for a SELF-CLEARING challenge page to
# turn into the real page before reporting it as blocked.
#
# The Akamai `sec-cpt` interstitial this site served on 2026-09-18 ships its
# own `location.reload(true)`, fired when its challenge XHR returns — so the
# page is asking to be waited for, and reporting it 0.4s after the fetch would
# be a false "blocked" for a page a browser clears by itself, for free.
#
# Budget and poll are deliberately separate from the captcha solver's
# (AUTOSOLVE_WAIT_MS, 180s): nothing is being paid for here and nothing is
# being solved, so a long wait buys only latency.
CHALLENGE_SETTLE_MS = 30000
CHALLENGE_POLL_MS = 1000


def wait_out_self_clearing_challenge(content, sleep, url=None,
                                     budget_ms=None, poll_ms=None, log=None):
    """Give a self-clearing challenge page its bounded chance to become the
    real page. Returns `(html, cleared)`.

    Driven through `content()` and `sleep(ms)` so all three engines share ONE
    copy — the same reason the rest of this module is written as callables.
    Three copies of a wait loop is how two of them end up with a different
    budget and nothing offline notices.

    The wait condition is POSITIVE: it waits for `detect_page_state` to say
    `content`, NOT for the challenge markers to disappear. That distinction
    was measured, not assumed. On 2026-09-18 the Akamai `sec-cpt`
    interstitial (4,383 bytes) turned after 4s into a 2,173-byte ThreatMetrix
    device-fingerprinting document (326 bytes of it once the auto-solve
    extension's injected script tags are stripped) — not the challenge any more, and not the
    product page either. A loop waiting for "no longer the challenge"
    stopped there and handed the parser a row of nulls, which is the silent
    success this function exists to prevent, one stage further along.
    Waiting for the site's own content hooks cannot stop early on a stage
    nobody has seen yet, and this is a CHAIN of defences whose length is not
    ours to know.

    `cleared` matters to the caller: the HTTP status of the ORIGINAL
    navigation describes the interstitial, not the document now in the page,
    so re-classifying with that status would veto the content just obtained.
    """
    from product_parser import detect_page_state, is_self_clearing_challenge
    budget = CHALLENGE_SETTLE_MS if budget_ms is None else budget_ms
    poll = CHALLENGE_POLL_MS if poll_ms is None else poll_ms
    latest = content() or ""
    if not is_self_clearing_challenge(latest):
        return latest, False
    if log:
        log("Self-clearing challenge page (%d bytes) — waiting up to %.0fs "
            "for the real page." % (len(latest), budget / 1000.0))
    waited = 0
    while waited < budget:
        try:
            sleep(poll)
        except Exception:
            # A driver error mid-wait is "nothing there yet", not a reason to
            # take the run down -- the same contract `wait_for_count` uses.
            return latest, False
        waited += poll
        fresh = content() or ""
        if fresh:
            latest = fresh
        if fresh and detect_page_state(fresh, None, url) == "content":
            if log:
                log("Challenge cleared after %.0fs (%d bytes)."
                    % (waited / 1000.0, len(fresh)))
            return fresh, True
    if log:
        log("Challenge did not clear within %.0fs (last document %d bytes) — "
            "reporting it rather than parsing it."
            % (budget / 1000.0, len(latest)))
    return latest, False


# How many paid captcha answers one URL may buy in a run. One: a token that
# did not clear the page will not be followed by a better one for the same
# page, and the retries `catalog_walk._get` makes on a 5xx must not each buy
# their own.
SOLVES_PER_PAGE = 1


class SolveBudget:
    """The run's allowance of paid solves, counted per URL.

    One object per run, handed to EVERY call site that can pay
    (`browser_bridge.open_storefront` and the challenge hook on each API
    call). `spend(url)` is called BEFORE the purchase and returns False when
    the URL's allowance is gone; a failed purchase still counts, because the
    attempt is what is billed. Nothing resets it -- not a retry of the same
    URL, not a new exit.
    """

    def __init__(self, per_page: int = SOLVES_PER_PAGE):
        self.per_page = per_page
        self.spent: Dict[str, int] = {}

    def remaining(self, url: str) -> int:
        return max(0, self.per_page - self.spent.get(url, 0))

    def spend(self, url: str) -> bool:
        if self.remaining(url) <= 0:
            return False
        self.spent[url] = self.spent.get(url, 0) + 1
        return True

    @property
    def total(self) -> int:
        return sum(self.spent.values())
