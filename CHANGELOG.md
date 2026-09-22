# Changelog

All notable changes to this project are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.0.2] — 2026-09-22

The rest of the audit: both P2 findings and the code notes worth acting on.

### Fixed

* **A diff would compare two different markets.** `_check_comparable`
  checked status and mode; the metadata did not even carry the store or the
  locale, so two complete runs of the same SKU in `gb`/GBP and `de`/EUR were
  considered comparable and every row read as a price change. Runs now carry
  a scope — store, locale, category, a digest of the grids actually walked,
  the limits and a schema version — and a diff refuses a mismatch, naming
  which field differs. `--force` compares anyway, knowingly. A run with no
  scope recorded is reported as unknown rather than waved through.

* **A bundle's owner was only stable within one batch.** The lowest entry id
  was resolved per payload, so the same bundle reached from a lower entry in
  a later batch kept whichever arrived first: `product_id` and `url` moved
  with batch order, and any downstream join moved with them. The owner map
  now spans the whole run and a canonicalising pass runs after everything is
  read, so the answer cannot depend on order.

### Added

* **`categories`**, every menu trail that lists a SKU, sorted. `category`
  still holds the first for compatibility. Measured on a three-grid run: 687
  of 1,907 rows belong to more than one category, which a single value could
  not say.
* **A product-id cache across grids.** Deduplication happened after the
  payload arrived, so a product listed by four grids was downloaded four
  times at roughly 25 KB each. The same three-grid run now requests 133
  products instead of 162 and reads 18.47 MiB instead of 23.07 — the same
  1,907 rows. Category membership is collected from the grids' id lists
  rather than from the fetches, so nothing is lost by not re-downloading.
* **Atomic output.** JSON, CSV and the metadata sidecar are written through a
  temporary file and replaced, and the sidecar is written last — so it is the
  commit point and a reader never sees a fresh JSON beside yesterday's CSV.

### Changed

* **`--max-products` is now `--max-skus`.** It always counted rows, and a row
  is a SKU: one dress in 4 colours and 8 sizes is 32 of them. The old
  spelling still works. The stop reason is `max_skus_reached`.
* **The Docker image runs `api_scraper.py`**, the primary engine on this
  site, instead of Playwright — and the stale `--mode market-values --pages`
  example a sibling repo left behind is gone.
* The store config is read once. `api_scraper.resolve_store` already had it
  and `crawl` fetched it again, in a request that was not even counted in
  the run's own tally.

---

## [1.0.1] — 2026-09-22

Correctness fixes found by an audit of the v1.0.0 tree. All four were
reproduced before being fixed, and each has a regression test.

### Fixed

* **A failed request became an empty category, and the run still said
  `completed`.** `catalog_walk._get` rejected only the edge refusal and
  handed every other response to the parser, where unparseable JSON became
  an empty grid. Reproduced on HTTP 500, HTTP 429 and HTML-at-200: each gave
  `exit 0`, `status=complete` and silently dropped a whole category. For a
  price monitor that reads as "those products are delisted", which is the
  worst thing this repo could get wrong.

  Responses are now classified — `HttpError`, `SchemaError`,
  `TransportError`, `RobotsRefusal`, `EdgeRefusal` — 429 and 5xx get bounded
  retries with jittered backoff honouring `Retry-After`, every failure is
  recorded on the result with its URL and status, and a run with any failure
  reports `requests_failed`, which is not a complete stop reason. It is
  written as partial and exits 6.

* **A failure after a good batch lost everything collected.** The exception
  escaped `crawl`, the engine mapped it to an exit code and `finish_run` was
  never reached — no JSON, no CSV, no metadata. `crawl` now records what
  stopped it and returns the partial result, so the rows survive and the
  reason travels with them into `.meta.json`'s `pages_failed`.

* **`--proxy-rotate` was accepted and did nothing.** The exit was read once
  into a closure; `advance()` was called only in tests. A run that started on
  one of the 4-in-10 refused exits had no way to reach a working address.
  Rotation now rebinds the session with the address — cookies issued against
  one IP must not be replayed from another — is bounded by the size of the
  pool, and `per-page` rotates at each grid through a hook the walk calls.

* **The robots snapshot did not travel with the wheel.** `py-modules` carries
  no data files, so an installed wheel had **0 rules** and called `/ru/` and
  `/itxrest/1/marketing/` allowed: the enforcement disappeared on delivery.
  The snapshot is packaged now, the loader looks beside the module and under
  `sys.prefix`, and a missing or empty snapshot raises
  `RobotsSnapshotMissing` instead of silently permitting everything. Verified
  by installing the built wheel into a clean venv: 140 rules, `/ru/` refused.

---

## [1.0.0] — 2026-09-20

First release. Built and measured against `www.bershka.com` on 2026-09-19 and
2026-09-20; every number below was read off a live run or a live capture on
one of those two days.

### Added

* **`api_scraper.py`, the primary engine.** Plain HTTPS to Inditex's
  catalogue API, no browser. Chosen on a measurement, not a preference: the
  API answered HTTP 200 to a plain `requests` call through a proxy, with no
  browser, no cookie jar and no TLS impersonation — checked twice, once with
  impersonation explicitly disabled.
* **`product_parser.py`.** The store config, menu, grid, productsArray and
  sitemap parsers, the two bot-wall vocabularies, and a robots.txt matcher
  that merges every group for a user-agent and applies longest-match
  precedence.
* **`catalog_walk.py`.** The route — store config → menu → grid →
  productsArray — with robots enforcement, batching and deduplication, shared
  by all four engines so none of them carries a copy.
* **`browser_bridge.py`** and the three browser engines
  (`playwright_scraper.py`, `puppeteer_scraper.py`, `selenium_scraper.py`).
  They open the storefront for its session and exit, then issue the same API
  calls from inside the page's origin.
* **`scraper_api_client.py`**, the 2captcha Scraper API transport, rewritten
  from HTML fetching to JSON.
* **`smoke_test.py`**, offline and network-free: the site checks written for
  this site, plus fifteen family checks carried over unchanged from the
  sibling repo they were written in.
* **The challenge ladder**, in `browser_bridge.py` and shared by all three
  browser engines rather than copied into each: the browser clears a
  challenge itself first, the 2captcha solver API is the fallback behind it,
  and a wall that clears neither way stops the run instead of producing
  nulls. `--solve-captcha when-blocked|always|never` controls the paid rung.
  It runs on the storefront open and on every later API call, because this
  edge decides per request. The edge refusal is deliberately excluded: there
  is nothing in a 195-byte 403 to solve.
* `robots.snapshot.txt`, so the rules are enforced offline too.
* `sample_output.json` / `.csv` — a real run, 131 rows, 36 columns,
  131/131 priced.

### Measured

* **The edge refuses by address.** From a Moscow residential address every
  URL on this host answered HTTP 403 in 195 bytes, `/robots.txt` included,
  carrying `x-reference-error` and `akamai-cache-status: Error from child`
  and setting `ITXSESSIONID`/`BSKSESSION` while refusing. Through a proxy the
  same URLs answered 200.
* **A rendered listing has no products in it.** `/gb/women.html`, 938,454
  bytes fully rendered: 0 JSON-LD blocks, 0 product ids, 0 grid classes,
  0 product URLs.
* **robots.txt has three separate `User-agent: *` groups** (lines 79, 123,
  192) carrying 28, 62 and 50 rules. A first-group-only reader keeps 28 and
  drops 112, including `Disallow: /ru/`. A bare `Disallow: /itxrest` appears
  twice, in the `proximic` and `Yandex` groups only.
* **The menu is the index**: 585 grid nodes on `gb`, plus 57 redirections and
  33 marketing nodes, the last of which is the one catalogue subtree robots
  disallows.
* **122 locales** in the sitemap index's union — 121 with a category sitemap,
  85 with a product sitemap.
* **The sitemap is not the catalogue**: the three GB product sitemaps hold
  8,654 URLs, and none of 53 products taken from two category grids appeared
  in them.
* **Engine parity**: one category run through `api_scraper.py` and through
  `playwright_scraper.py` over a CDP endpoint produced 131 rows each, the
  same SKU set and zero differing fields.

### Fixed during development

Each of these was found by a live run, and none of them was visible to an
offline check or a coverage count:

* `section` was filled on 100% of 1,907 rows with the string `"1"` — the
  payload's numeric section code — because the parser fell back to
  `entry["section"]` when a name was absent. `page` was 0% filled because
  nothing passed it.
* `product_id` and `url` were attributed to whichever array entry reached a
  bundle first, and the grid does not promise an order: two runs of one
  category attributed all 131 rows to two different product ids. Left alone,
  `diff_runs.py` would have reported every row as changed on every run. The
  lowest id now wins. `related_categories` had the same flaw and the same
  fix, which also raised its coverage from 67 to 99 of 131 rows.
* A CDP connect failure was masked with `proxy_pool.mask()`, which parses its
  argument as a URL: handed an exception message it returned `"?://?"`,
  destroying the only description of what had gone wrong. Free text now goes
  through a masker that redacts credentials and leaves the text.
* `smoke_test.py` hung and exhausted memory. It patched
  `argparse.ArgumentParser` with a subclass, and argparse's own `__init__`
  resolves `super(ArgumentParser, self)` through the module global — so the
  patch made that line recurse forever. It patches the method now.
* `.github/ci_checks.py` refused a `proxylist.txt` left in the working tree
  and a real `ITXSESSIONID` embedded in a test fixture. Both were removed.
* `diff_runs.TRACKED_FIELDS` was carried over naming `shade`, `shade_count`,
  `size` and `badge` — four columns this schema does not have. `.get()`
  returns None on both sides of a missing key, so the diff looked healthy
  while comparing nothing for them and skipping `is_buyable` and `back_soon`,
  which are what actually move here. A test now pins every tracked field to a
  real column.
* The Selenium driver's `evaluate` assumed everything handed to it was
  async and called `.then` on the result. The bridge's fetch helper is async;
  captcha_solver's discovery script is an ordinary synchronous function, so
  detection there would have thrown and been reported as "no captcha found".

### Measured by ten live runs, 2026-09-20

* **The interstitial is real and common.** 8 of 10 exits were served a
  self-clearing Akamai shim on the storefront — HTTP 200, ~2,142 bytes,
  `bm-verify` plus a split arithmetic operand. The other 2 got the flat 403.
  Before these runs this repo said no challenge had ever been seen here.
* **It gates the storefront only.** On all 8 the catalogue API answered
  179,035 bytes of JSON to a cookie-less request in the same seconds.
* **It does not clear by waiting** for a plain client: three fetches in one
  session, carrying `ak_bmsc`, returned the shim every time. A browser gets
  the real page at once (889,836 bytes, no shim).
* **`sku` is stable across locales and price is not.** The same category in
  `gb` and `de` returned the same 131 SKUs with 0 of 131 sharing a number.

### Measured by ten more runs, 2026-09-20

* **The challenge is Akamai Bot Manager's interstitial, not a captcha.**
  Scanned against every vendor this family knows across twenty exits, the
  only markers ever present were Akamai's. No reCAPTCHA, hCaptcha,
  Turnstile, AWS WAF, DataDome, PerimeterX, Imperva, Kasada or `sec-cpt`.
  Nothing on it is answerable by a human or by a solver API.
* **Clearing it, six exits that served it:** retrying without answering
  cleared 0 of 6; answering the arithmetic cleared 6 of 6 with verify HTTP
  200 every time, in 0.55-2.79 s; a fresh session that answers on first
  sight cleared 6 of 6.
* **A cleared session stays cleared.** Five further requests all returned the
  real page; the verify response sets `_abck` and `bm_sz` next to `ak_bmsc`.
  The cost is one round trip per session, not per request.
* **The API is not behind it at all** — cookie-less requests got content on
  all six.

### Fixed by those runs

* **`is_edge_refusal` called the interstitial an address ban.** Akamai sends
  `x-reference-error` on the shim too, and that header was in the refusal's
  marker list. Since the refusal is tested FIRST and raises, every real
  challenge would have been reported as "change your exit" — the one remedy
  that does not apply — and the challenge ladder would never have run. The
  discriminator is the cache status (`Error from child` against
  `NotCacheable from child`) and the body, not that header.
* **`api_scraper.py` now answers the shim over plain HTTP**, so a locale
  without a measured store id is reachable without a browser. Before this it
  failed with "could not read a store id out of … (HTTP 200)", which reads
  like the page changed rather than like an unanswered challenge.
* **`de` added to `KNOWN_STORES`** (44009504 / 40259546 / EUR), measured
  rather than inferred.

### Known limits

* Only `gb` has a measured store id; any other locale is resolved by reading
  the storefront's own configuration blob.
* Per-locale pricing has not been compared.
* No paging: a grid returns its whole id list at once and the storefront's
  own paging parameters are robots-disallowed.
* The solver rung has still never been exercised against a real challenge:
  the only challenge this host serves clears on rung 1 (the browser, or the
  plain-HTTP handshake). The solver path is wired, tested offline, and
  unproven live.
