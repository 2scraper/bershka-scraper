# bershka-scraper

[![release](https://img.shields.io/github/v/release/2scraper/bershka-scraper?sort=semver)](https://github.com/2scraper/bershka-scraper/releases)
[![tests](https://github.com/2scraper/bershka-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/bershka-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/bershka-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/bershka-scraper/actions/workflows/canary.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![licence](https://img.shields.io/badge/licence-MIT-lightgrey)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20CDP%20%7C%20Scraper%20API-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without%20an%20account-yes-brightgreen)](#the-exit-decides-whether-anything-works)

Scrapes **bershka.com** — an Inditex storefront whose catalogue lives entirely
behind its own `/itxrest/` API — into one row per SKU, as JSON and CSV. Three
browser engines plus two browserless HTTP clients, one parser, one row schema.

Part of the 2scraper family: the row schema's first ten columns are identical
across every repo in it, so output from several of them merges without
translation.

---

## What it reads

```bash
# A category, by its trail through the site's own menu
python3 api_scraper.py --category "WOMEN / Accessories / Bags and purses"

# Every grid the menu lists — 507 of them on gb (2026-09-24), so mean it
python3 api_scraper.py --list-categories | head -40

# One product, by the id its URL carries
python3 api_scraper.py --mode product --product-id 229723104

# A different locale: the store id is read off that storefront
python3 api_scraper.py --site-locale de --category "DAMEN / SALE"

# No local browser at all, over 2Captcha's Scraper API
python3 scraper_api_client.py --category "WOMEN / Clothes / Jeans" --max-grids 1
```

A run prints what it did and what it cost:

```
[i] store 44009506 catalog 40259534 GB GBP
[i] 1/1 grid(s), 86 product(s) requested, 131 row(s)
[i] 6 request(s), 5.21 MiB read
[i] 30 array entry(ies) pointed at a bundle already seen — folded, not dropped silently
[+] Saved 131 row(s) -> bershka_products_api.json
```

`sample_output.json` / `.csv` in this repo are a real run, not a mock-up:
131 rows, 36 columns, 131 of 131 priced.

---

## Install

```bash
pip install -r requirements.txt          # the API engines need nothing else
pip install -r requirements-playwright.txt   # or -puppeteer / -selenium
cp .env.example .env                     # then fill in BERSHKA_PROXY
```

Python 3.9+. The browser engines pin incompatible dependencies against each
other — install exactly one per environment, which is what CI does.

---

## Usage

```bash
# Walk a subtree instead of one grid; --max-grids bounds the work
python3 api_scraper.py --category "WOMEN / Clothes" --max-grids 5

# Spread a long run over a pool of exits rather than one session
python3 api_scraper.py --proxy-file proxylist.txt --proxy-shuffle \
    --category "WOMEN / Accessories"

# A browser, either launched here or attached over CDP
python3 playwright_scraper.py --category "WOMEN / Clothes / Jeans" --max-grids 1
python3 playwright_scraper.py --cdp-endpoint "$BERSHKA_CDP_ENDPOINT" \
    --category "WOMEN / Clothes / Jeans" --max-grids 1

# Bound the rows rather than the grids. A row is one SKU, so this is not a
# product count — `--max-products` still works as the old spelling.
python3 api_scraper.py --category "WOMEN / Clothes" --max-skus 500

# Write only CSV, and accept an empty result as the answer
python3 api_scraper.py --category "MEN / Clothes / Polos" --format csv --allow-empty
```

`--category` matches a menu **trail**, not a leaf name, so `"WOMEN / SALE"`
means that whole subtree and `"Jeans"` means every jeans grid under any
section. Without it a run walks all 507 grids; pair it with `--max-grids`
unless you mean that.

`tools/browser_profile_client.py` builds a Scraping Browser API connection
string for `--cdp-endpoint`, optionally with your own exit attached to it:

```bash
python3 tools/browser_profile_client.py accounts
python3 tools/browser_profile_client.py use --account-id N --write-env
```

---

## The exit decides whether anything works

Measured 2026-09-19 from a residential address:

| URL | plain `curl` | through a proxy |
|---|---|---|
| `/robots.txt` | **403**, 195 bytes | **200** |
| `/gb/` | **403**, 195 bytes | **200** |
| `/itxrest/2/catalog/store/44009506` | **403**, 195 bytes | **200**, 179,035 bytes |

That 403 is Akamai's edge refusing the **address**. It carries
`x-reference-error` and `akamai-cache-status: Error from child`, and it sets
`ITXSESSIONID` and `BSKSESSION` even while refusing. Nothing in it is a
challenge to solve or a wait to sit out.

Which exits are refused is not "residential good, datacentre bad" — it is
per address. Measured over ten exits on 2026-09-20: **4 were refused
outright**, 6 were served the storefront and answered the API. A bare GitHub
runner, a datacentre address, was **not** refused and reached the API with no
proxy at all. So the rule is that you need an address this edge accepts, and
finding one is what a pool is for.

Put one proxy URL in `BERSHKA_PROXY`, or a pool in a file:

```bash
python3 api_scraper.py --proxy-file proxylist.txt --proxy-shuffle \
    --category "WOMEN / Clothes / Jeans"
```

`.gitignore` covers `proxylist*.txt` and `.github/ci_checks.py` refuses to
let one be committed — it caught exactly that during this repo's own
development.

A session-pinned exit holds one address for the session and then moves: a run
that was getting 200s started getting the 403 mid-session on 2026-09-20. For
anything long, use `--proxy-file` with a real pool.

---

## There is no DOM parser here, and that is measured

A fully rendered `/gb/women.html` — 938,454 bytes through a paid exit,
2026-09-19 — contains:

| looked for | found |
|---|---|
| `application/ld+json` blocks | **0** |
| product id attributes | **0** |
| product grid classes | **0** |
| product URLs in the markup | **0** |

The grid arrives afterwards as JSON, so this repo walks the API instead:

```
/itxrest/2/catalog/store/{store}            store config: currency, catalogId
/api/storefront/1/stores/{store}/menu       the category tree -> grid ids
/itxrest/4/catalog/store/{store}/grid/{id}  -> productIds
/itxrest/3/catalog/store/{store}/{cat}/productsArray?productIds=...
```

robots.txt permits every one of them — see below.

Breadth comes from the menu, which carried **507 grids** on `gb` on
2026-09-24 (597 nodes are typed `grid`; the other 90 name a category key,
answer 404 on the grid endpoint, and are skipped), not from
paging: a grid returns its whole id list in one response and there is no
cursor. `--category` matches a menu **trail**, so `"WOMEN / SALE"` means that
whole subtree.

---

## What a row looks like

A row is one **SKU**, and here a SKU is a *(product, colour, size)* triple
rather than a product. The 131 rows above are 86 products.

| column | example | note |
|---|---|---|
| `sku` | `229704949` | the API's `sizes[].sku` — the only field unique per row |
| `title` | `Lace strap midi dress` | |
| `price` / `currency` | `20.99` / `GBP` | scaled from minor units; currency from the store config |
| `original_price` | `29.99` | the struck price, when there is one |
| `discount_pct` / `discount_source` | `30.0` / `api` | `api` when stated, `computed` when derived |
| `color_name` / `size_name` | `Black` / `XS` | **`size_name` is not unique within a colour** |
| `availability` | `InStock` / `OutOfStock` / `BackSoon` | folded from `isBuyable` and `backSoon` |
| `url` | `…/gb/lace-strap-midi-dress-c0p229723104.html` | the product page, not the SKU |
| `related_categories` | `["Dresses and Jumpsuits", "Long"]` | the site's own list; 99 of 131 rows carried one |
| `categories` | `["WOMEN / SALE / Bershka", "… / Trousers and jeans"]` | every menu trail that lists the SKU; 687 of 1,907 rows had more than one |

The first ten columns are this family's shared prefix and keep their order.
Everything after them is Bershka's business; `output_writer.py`'s `Product`
documents each one.

### Prices are per-locale and SKUs are not

The same category in `gb` and `de` returned the **same 131 SKUs**, and **0 of
those 131 carried the same number** — £12.99 against €14.99, and so on down.
So a SKU identifies the same thing everywhere and its price does not.

---

## Blocks, and what the paid products buy

Two different walls, and telling them apart is most of the work.

**The edge refusal** is the 403 above. It is keyed on the exit address, has
no challenge in it, and the only remedy is a different exit.
`product_parser.is_edge_refusal()` names it and a run stops rather than
retrying into it.

**The interstitial** is what a cookie-less client gets on the storefront:
HTTP 200, ~2,142 bytes, a `bm-verify` token and one line of arithmetic

```js
var i = 1789910678; var j = i + Number("3886" + "11036");
```

whose operand is split across a string concatenation so a regex looking for
one number literal finds nothing. The page's own script POSTs
`{"bm-verify": token, "pow": i + 388611036}` to
`/_sec/verify?provider=interstitial` and reloads.

**It is not a captcha.** Twenty exits were scanned for every vendor this
family knows — reCAPTCHA, hCaptcha, Cloudflare Turnstile and IUAM, AWS WAF,
DataDome, PerimeterX, Imperva, Kasada, Akamai `sec-cpt` — and only Akamai's
own markers ever appeared. There is no image, no checkbox and no token to
buy, which is why the solver fallback has never fired here.

Ten runs, six of which were served it:

| strategy | cleared |
|---|---|
| retry in the same session, answer nothing | **0 / 6** |
| answer the arithmetic, then re-request | **6 / 6** (verify HTTP 200 each time) |
| fresh session, answer on first sight | **6 / 6** |
| don't touch the storefront — call the API | **6 / 6**, nothing to clear |

The answer costs 0.55–2.79 s and the cleared session **stays** cleared: five
further requests all returned the real page, because the verify response sets
`_abck` and `bm_sz` next to `ak_bmsc`. One round trip per session, not per
request.

So the handling, in order: **do not meet it** — the catalogue API is not
behind it, and cookie-less requests got 179,035 bytes of JSON on all six;
**when you must, answer once and keep the session**, which `api_scraper.py`
does with one answer and one retry, never a loop; **a browser needs nothing**
— it gets the real page immediately, 889,836 bytes, no shim.

The browser engines carry a three-rung ladder in `browser_bridge.py`: the
browser clears it, then the 2Captcha solver API buys an answer, then the run
stops and names the wall. `--solve-captcha when-blocked|always|never`
controls the paid rung, and `when-blocked` is the default so the free rung
is always exercised first. `scraper_api_client.py` cannot do any of this —
each of its calls is a stateless task with no session to carry cookies.

---

## robots.txt

The catalogue API is allowed. The file does not reward a substring check:

* **Three separate `User-agent: *` groups** (lines 79, 123, 192) carrying 28,
  62 and 50 rules. A parser that takes the first match and stops keeps 28 and
  drops 112 — including `Disallow: /ru/` and fifty per-country product-page
  bans such as `/kz/*-c0*p*.html`. RFC 9309 merges records with the same
  user-agent, and so does `parse_robots()`.
* A bare `Disallow: /itxrest` appears twice, but in the `proximic` and
  `Yandex` groups. For `*` the only `/itxrest` rule is
  `Disallow: /itxrest/1/marketing/`.
* The one `Allow` (`/*/q/*index=1*`) means nothing without longest-match
  precedence, which is implemented.
* Search, filters and sorting are disallowed. Nothing here builds such a URL.

A snapshot taken 2026-09-19 ships as `robots.snapshot.txt` so the rules are
enforced offline too. It is a record of that day, not a licence.

---

## Locales

The sitemap index lists 375 files covering **122 locales** in union — 121
with a category sitemap, 85 with a product sitemap. `gb` and `de` have
measured store ids; any other locale is resolved by reading the storefront's
own `var inditex={…}` blob, which works over plain HTTP because the engine
answers the interstitial in front of it.

**The sitemap is not the catalogue.** The three GB product sitemaps hold
8,654 product URLs, and **none of 53 products taken from two category grids
appeared in them**. Sitemap-driven coverage is not a substitute for walking
the menu.

---

## Traps that look like bugs

Each is a real behaviour, met while building this, and each has a test
pinning it:

* **An error object arrives inside HTTP 200.** A request for 14 ids returned
  13 products and `{"description": "Item not found", "key":
  "_ERR_PRODUCT_NOT_FOUND"}` in the array, positionally, with no id saying
  which request it answers. Every grid measured had exactly one more
  `productIds` than `sortedProductIds`, and exactly one such object.
* **The product is not the product.** Every visible entry is a `BundleBean`
  whose own `detail.colors` is empty; the colours, sizes and prices hang off
  `bundleProductSummaries[0]`. Reading the outer `detail` yields a product
  with no price and looks like a working parser.
* **Several entries share one bundle.** 13 products resolved to 9 bundles;
  emitting per entry gave 195 rows for 131 SKUs. Bundles are emitted once and
  the fold is reported, not hidden.
* **Attribution is chosen, not left to arrival order.** Two runs of one
  category attributed all 131 rows to two different product ids, because the
  grid promises no order. The lowest id wins — arbitrary, but stable, so
  `diff_runs.py` does not report every row as changed on every run.
* **Size names repeat.** One colour served two sizes both called `XS`, with
  different `sku`, `partnumber` and country of origin.
* **Prices are strings of minor units** (`"2099"`) with the exponent in the
  store config (`currencyDecimals: -2`). There is no currency anywhere in the
  product payload.
* **A grid can legitimately list nothing.** The "view all" parents do. That
  is a correct answer, not a failure — and this repo's own canary shipped
  once pointed at one.

---

## Engines

| engine | transport | needs |
|---|---|---|
| **`api_scraper.py`** | plain HTTPS | a proxy |
| `playwright_scraper.py` | Playwright | a browser or a CDP endpoint |
| `puppeteer_scraper.py` | pyppeteer | same |
| `selenium_scraper.py` | Selenium | a browser it launches itself |
| `scraper_api_client.py` | 2Captcha Scraper API | a 2Captcha key |

`api_scraper.py` is the one to run. The catalogue API answered **HTTP 200 to
a plain `requests` call** through a proxy — no browser, no cookie jar, no TLS
impersonation, checked twice and once with impersonation explicitly disabled.
A browser buys nothing here except an exit and a cookie jar the API does not
ask for.

The browser engines are kept for two narrow reasons: an operator may have a
profile on the Scraping Browser API and no proxy of their own — connect one
with `--cdp-endpoint`, and `tools/browser_profile_client.py` builds the
connection string, optionally with your own exit attached — and if the edge
ever starts requiring
the session cookies it currently issues and ignores, a same-origin `fetch`
from a real page already carries them.

Parity is a comparison, not a claim. The same category run through
`api_scraper.py` and through `playwright_scraper.py` over a CDP endpoint
produced **131 rows each, the same SKU set, and zero differing fields** once
`scraped_at` and the image cache-buster are set aside.

Selenium cannot attach to an authenticated CDP endpoint and cannot
authenticate a proxy — chromedriver has nowhere to put the credentials. It
refuses rather than connecting as somebody else.

---

## Configuration

`.env` is the only place credentials live, and `env_config.py` the only
loader. Never a command line: `ps` reads argv.

| variable | used by |
|---|---|
| `TWOCAPTCHA_KEY` | `scraper_api_client.py`, the solver fallback, the tools |
| `BERSHKA_PROXY` | every engine that picks its own exit |
| `BERSHKA_CDP_ENDPOINT` | the three browser engines |
| `BERSHKA_URL` | optional override |

`.env.example` documents each one and is the file the test suite compares the
key set against.

---

## Exit codes

| code | meaning |
|---|---|
| 0 | rows written |
| 1 | crash |
| 2 | bad usage |
| 3 | blocked — the exit was refused, or robots disallows the URL |
| 4 | ran fine, zero rows (which can be the correct answer) |
| 5 | a remote service failed on its own terms |
| 6 | partial run; `.meta.json` names the stop reason |

A run that finds nothing writes nothing rather than overwriting a good file
with `[]`. Pass `--allow-empty` when empty is the expected answer.

**Exit 6 is the one to wire an alert to.** Any request that failed — a 5xx, a
429 that outlived its retries, a body that was not JSON, a timeout — is
recorded with its URL and status, and the run reports `requests_failed`
rather than `completed`. The rows it did collect are still written, so a
partial run is usable; what it must never be is mistaken for a full one. 429
and the 5xx range are retried first, bounded, with jittered backoff that
honours `Retry-After`; a 404 or an application 403 is not retried, because
the same request later is the same answer.

---

## Comparing two runs

```bash
python3 diff_runs.py yesterday.json today.json
```

**It refuses two runs that did not cover the same ground.** Each run records
a scope — store, locale, category, a digest of the grids actually walked, the
limits and a schema version — and a mismatch is reported rather than diffed:
two complete runs of the same SKU in `gb`/GBP and `de`/EUR are two markets,
not a price change. `--force` compares them anyway, knowingly. A run with no
scope recorded is reported as unknown rather than assumed to match.

Matches on `sku` and reports added, removed and changed rows. It compares
`price`, `currency`, `original_price`, `discount_pct`, `availability`,
`title`, `is_buyable`, `back_soon` and `promotion_id` — not `row_index` or
`page`, because a row's position inside a payload is the site's
merchandising: two runs twenty minutes apart produced identical rows with 44
of 131 positions changed.

---

## Docker

```bash
docker build -t bershka-scraper .
docker run --rm --env-file .env bershka-scraper --category "WOMEN / Clothes / Jeans"
```

The image runs `api_scraper.py`, the primary engine here, so it is a
`requests` install rather than a Chromium download. The browser engines are
in it too and reachable with `--entrypoint`, but it deliberately carries no
browser for them. CI builds the image on every push, because nothing else in
a repo like this ever would.

---

## Tests

```bash
python3 smoke_test.py             # offline: no network, no browser, no engine library
pytest                            # the same checks, wrapped
python3 .github/ci_checks.py --all
```

`smoke_test.py` pins values, not shapes — `price == 20.99`, not
`price is not None`. That is not pedantry: the first live run filled
`section` on 100% of 1,907 rows with the string `"1"`, the payload's numeric
section code, and no coverage check would have noticed.

A daily canary runs the real thing against the live site and calls those same
shipped checks rather than re-implementing them.

---

## Troubleshooting

**Every request comes back exit 3.** Check the exit address first. A 195-byte
HTTP 403 with `x-reference-error` and `akamai-cache-status: Error from child`
is the edge refusing your address before the application ever saw the
request. No setting changes that; a different exit does. A residential address
in Moscow was refused on every URL including `/robots.txt`, while a GitHub
runner was not — it is the address, not the hosting type.

**A 2 KB page where the storefront should be.** That is Akamai's interstitial,
not a captcha and not a block — see above. `api_scraper.py` answers it and
retries once. Retrying without answering it never clears it: 0 of 6 exits.

**Exit 4 on a category that plainly has products.** Read the log line naming
the grid before blaming the parser. The menu's "view all" parents list no
products at all, and `--max-grids 1` on a broad `--category` picks exactly
those. This repo's own canary shipped once pointed at one.

**Exit 5 with `SSLError ... Max retries exceeded`.** The connection to the
site failed rather than the site refusing it — usually the proxy exit
dropping. With a pool configured the engine now leaves that exit by itself.

**Exit 6 and a list of failed URLs.** Some of what you asked for was not
read. `.meta.json`'s `pages_failed` names them and the run log gives the
status for each. Do not diff a partial run against a complete one without
reading that list first.

**HTTP 500 from the Scraping Browser endpoint**, `proxy_timeout` or
`browser_timeout`. A vendor-side failure, and transient: two failures then a
3.0 s connect with no change in between. A profile also allows one live
connection at a time, and a second run holding the same `pid` fails with
`profile_locked`.

---

## Measurements in this README

Everything above was measured rather than estimated. The numbers come from
live runs on **2026-09-19**, **2026-09-20** and **2026-09-22** against the
`gb` and `de` storefronts, from a residential exit, a pool of 55 proxy exits
and a GitHub runner, plus page and payload captures taken the same days. The
offline suite pins field values from those captures — the bundle indirection,
the error object inside a 200, two sizes sharing a name, the interstitial and
the edge refusal side by side — so a change in the payload fails a test rather
than quietly emptying a column.

The challenge classification is by exclusion: twenty exits scanned for
reCAPTCHA, hCaptcha, Cloudflare Turnstile and IUAM, AWS WAF, DataDome,
PerimeterX, Imperva, Kasada and Akamai `sec-cpt`, with only Akamai's own
markers ever present.

`smoke_test.py`: no network, no browser, no credentials.

---

## Legal

For research, price monitoring and comparison. You are responsible for
complying with Bershka's terms, with `robots.txt` — which this repo enforces
rather than merely reads — and with the data protection law that applies to
you. It reads the same public catalogue the storefront itself calls, and does
not attempt to reach anything behind an account.

MIT licensed — see [LICENSE](LICENSE).

Built by [2Captcha](https://2captcha.com).
