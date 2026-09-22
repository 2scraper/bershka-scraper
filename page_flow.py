"""
page_flow.py
------------
bershka.com's page-state policy and readiness anchors, shared by all
three engines.

Two modes, and only one of them has anything to wait for:

    category   a listing page. Server-rendered in full on the first
               response — the tiles, their prices and their ids are all in
               the bytes a plain `curl` returns (measured 2026-09-17/18 on
               20 captures, 317 tiles, none of them added by script). The
               readiness anchor therefore confirms the GRID arrived, it does
               not wait out a hydration step.
    product    one product page. Likewise server-rendered, including the
               JSON-LD block that carries the price.

Why there is no pagination wait here
------------------------------------
There is no page 2 to wait for. This site's listing paging is Demandware's
`?start=N&sz=25`, requested by a "show more" control, and robots.txt
disallows both parameters (`*?start=*`, `*sz*`) — so `product_parser.
page_url` returns its input unchanged and a listing run is one fetch. See
that function's comment for what covers a whole locale instead.

That makes CLAUDE.md §7's layer 3 — "this page added no new sku" — the only
terminating condition in play, which is the layer the family trusts most: a
missing link is a property of markup, an exhausted listing is a property of
the catalogue.

What a page can answer
----------------------
    content    parse it
    blocked    a network- or vendor-level refusal before any content arrived
    captcha    a challenge widget — the paid path
    empty      a real page with nothing to parse on it. NOT hypothetical
               here and NOT a fault: `/es/.../sets/` and `/it/.../sets/` are
               both served, both 200, and both list zero products (measured
               2026-09-17). Report it, do not retry it, and do not go
               looking for a proxy problem.

Three copies of that triage would drift, exactly as this family's other
repos found.

The functions here are either pure or driven through small callables, so
each engine passes its own driver's primitives — no JavaScript crosses this
boundary, which is what keeps a site whose CSP lacks `unsafe-eval` from
turning a readiness wait into a crash.
"""

import logging
from typing import Callable, Dict, Optional

from product_parser import detect_page_state, page_url

logger = logging.getLogger("page_flow")


# What "the page has painted" means, per mode. Both modes are server-rendered
# in full on the first response (measured — see module docstring), so these
# anchors exist to confirm the right grid/page arrived, not to wait out a
# hydration step.
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
    refuses outright -- on a sibling repo (tokopedia-scraper) that was an
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


# A page holding less than this fraction of page 1's own row count is logged
# as thin -- INFORMATIONAL ONLY, never a retry trigger and never a reason to
# change the exit code. A short final page is a correct answer (measured:
# both paginated listings return exactly 25 rows per full page, so a last
# page of a handful is expected, not a fault) -- what this catches is the
# case CLAUDE.md's own family lessons warn about: a page beyond the site's
# real depth, or a markup regression, coming back with only a few rows
# instead of a clean "no new products" the sku-freshness check would have
# caught outright. This repo deliberately does NOT add a numeric PAGE_CAP
# (as catawiki-scraper does) on top of this: the sku-based "no new rows"
# stop condition already terminates on DATA rather than a guessed page
# count, which is the family's own stated preference (CLAUDE.md §7) -- a
# hardcoded cap here would be redundant insurance for a failure mode this
# repo's fresher, sku-driven check already covers.
THIN_PAGE_RATIO = 0.4


def is_thin_page(row_count: int, first_page_count: int) -> bool:
    if first_page_count <= 0:
        return False
    return row_count < first_page_count * THIN_PAGE_RATIO


# On a PRICED locale every tile carries a price: measured 2026-09-18, 16/16
# on `/us/makeup/lips/`, 25/25 on `/gb/makeup/lips/`, 1/1 on jp and 100% on
# each of the twelve larger `us` captures -- 291 of 291 tiles outside the
# showcase locales. So coverage below this floor is a parsing regression to
# flag, not the site's own doing, and the floor sits at the family's own 90%
# convention (CLAUDE.md §4) rather than lower.
#
# It must NOT be applied to a showcase locale. `/int/en/` and `/ru/` render
# no price container at all (17 of 17 and 9 of 9 tiles), so 0% coverage
# there is the correct reading of a correct page, and warning about it would
# train the reader to ignore the warning that matters. The engines check
# `product_parser.is_showcase_locale` before consulting this.
PRICE_COVERAGE_FLOOR = 0.90


# How long an engine spends waiting for a SELF-CLEARING challenge page to
# turn into the real page before reporting it as blocked.
#
# The Akamai `sec-cpt` interstitial this site served on 2026-09-18 ships its
# own `location.reload(true)`, fired when its challenge XHR returns — so the
# page is asking to be waited for, and reporting it 0.4s after the fetch is
# the false exit 3 CLAUDE.md §6 lists as this family's cheapest open
# improvement. Paying that debt here, for the one challenge this site was
# actually measured serving.
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


def classify(html: Optional[str], *, status: Optional[int] = None,
             url: Optional[str] = None,
             headers: Optional[Dict[str, str]] = None) -> str:
    """The page's state, as the engines see it. A thin wrapper over
    product_parser.detect_page_state, for the same reason the sibling repos
    keep one: every engine reaches the policy through one name.

    `status` and `headers` are both OPTIONAL because not every engine can
    supply them, and the classifier has to stay correct for the ones that
    cannot. Playwright exposes the response object, and the Scraper API
    returns `{"status", "headers", "body"}` — those two can pass both.
    Selenium and pyppeteer read the rendered DOM through the driver and have
    no response object to ask, so they pass neither, and for them the vendor
    markers are the ONLY thing standing between an AWS WAF captcha and a run
    that reports it as an empty listing. That asymmetry is why A1's markers,
    not this header, are the load-bearing half of the fix.

    Keyword-only after `html` on purpose: `classify(html, status, url)` with
    `status` positional is the exact signature that crashed two of three
    engines in a sibling repo on their first fetch (§17, check 1), because a
    caller wrote `classify(html, url=...)` and bound `url` to `status`.
    """
    return detect_page_state(html or "", status=status, url=url, headers=headers)


# What each state means for the run. Kept as data, not as three copies of an
# if-chain, for the reason CLAUDE.md gives: an engine cannot then quietly
# disagree with its twins about whether a page is worth retrying or paying
# for.
STATE_POLICY = {
    "content": {"retry": False, "solve": False, "blocked": False},
    "blocked": {"retry": True, "solve": False, "blocked": True},
    "captcha": {"retry": True, "solve": True, "blocked": True},
    # NOT retried: an out-of-range page or an empty squad URL is a correct
    # answer to the question that was asked.
    "empty": {"retry": False, "solve": False, "blocked": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("retry", False)


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("solve", False)


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("blocked", False)


def comparable(url: str) -> str:
    """`url` reduced to the parts that decide WHICH PAGE it addresses.

    Bershka's own in-page links were not observed to carry tracking
    parameters worth stripping (none were seen across any of the 18 captured
    pages), so this only drops the fragment and normalises query-parameter
    ORDER — a next-link and a constructed URL that agree on
    every parameter but its order must still compare equal.
    """
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    parts = urlparse(url)
    kept = parse_qsl(parts.query, keep_blank_values=True)
    return urlunparse(parts._replace(query=urlencode(sorted(kept)), fragment=""))


def _query_only(url: str) -> str:
    """`url` reduced to JUST its query parameters, order-independent.

    See pagination_is_addressable's "A site can rename its own listing path"
    note for why the PATH is deliberately excluded here even though
    `comparable()` (used elsewhere for exhausted-pagination checks) keeps it.
    """
    from urllib.parse import urlparse, parse_qsl, urlencode
    query = parse_qsl(urlparse(url).query, keep_blank_values=True)
    return urlencode(sorted(query))


def pagination_is_addressable(mode: str, page1_url: str,
                              site_next_url: Optional[str]) -> bool:
    """Whether page N can be fetched without first fetching page N-1.

    On THIS site the answer is always False, and the first check below says
    why in terms of `page_url` rather than hardcoding it: the listing's own
    paging is robots-disallowed, so no page-2 address is built and there is
    nothing for a next-link to agree with.

    The rest is kept working and kept identical to the siblings': it is the
    general form of the question ("does page 1's own next-link agree with
    what `page_url()` would build for page 2?"), and a repo that deleted it
    would be the one that quietly diverged if this site ever exposed an
    allowed pagination parameter.

    `mode` is kept in the signature for callers and for the case a THIRD
    pagination shape turns up on some other mode later, even though no mode
    currently branches on it here.

    A site can rename its own listing path without changing how pagination
    works
    ----------------------------------------------------------------------
    Measured live 2026-09-12, two days after the gallery-trap
    re-measurement above: `/statistik/neuestetransfers`'s own `<link
    rel="next">` had moved to a DIFFERENT PATH —
    `/transfers/neuestetransfers/statistik?page=2` — while the query string
    was still the plain `?page=2` this function expects. Fetching BOTH the
    old constructed URL and the new next-link agreed byte-for-byte (the same
    25 transfer ids, 0 overlap with page 1 either way), so the site had
    simply exposed a second path for the same listing, not changed the
    pagination contract. The ORIGINAL version of this function compared the
    FULL url (via `comparable()`, which keeps the path) and would have
    called this unaddressable, silently falling back to slow link-chaining
    for a mode that was, in fact, still perfectly addressable. So only the
    QUERY is compared below, never the path.

    The one thing still checked against the path is the gallery trap's own
    signature: the page number encoded IN the path (`.../page/8/page/3//
    page/2`) rather than passed as a `?page=N` query parameter. A next-link
    shaped like that is never trusted as equivalent to a constructed URL
    regardless of what its query string says (it usually has none at all).
    """
    # On this site `page_url` returns its input unchanged (the listing's own
    # paging is robots-disallowed), so there IS no page 2 address to agree
    # with and the honest answer is False for every listing here. Written as
    # a property of `page_url` rather than as `return False` so that a
    # future repo-level decision to page some other way needs no edit here.
    if page_url(page1_url, 2) == page1_url:
        return False
    if not site_next_url:
        return True
    from urllib.parse import urlsplit
    if "/page/" in urlsplit(site_next_url).path:
        return False
    return _query_only(site_next_url) == _query_only(page_url(page1_url, 2))
