# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and
takes about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but two things are
**not** masked: raw HTML dumps and your shell history. Before pasting any
output into an issue or a PR, replace keys, proxy passwords and full
`ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed.
That check is a backstop, not a review — a leaked key has to be rotated
whether or not the check caught it.

## Reporting a site change

Inditex changing its catalogue payload is the normal way this stops working,
and it has its own issue template. Worth knowing before you file:

**This repo does not parse HTML.** A rendered listing page carries no products
at all — 0 JSON-LD blocks, 0 product ids, 0 grid classes in 938,454 bytes,
measured 2026-09-19. Everything comes from JSON under `/itxrest/`. So "the
markup changed" is almost never the diagnosis here; "the payload changed" is.

Three things can move, in decreasing order of durability:

1. **The endpoints** — the `/itxrest/{n}/...` paths in `product_parser.py`.
   Their version prefixes (2, 3, 4) differ per endpoint and have drifted
   before, in the generation of this scraper that preceded this one.
2. **The payload shape** — `bundleProductSummaries[].detail.colors[].sizes[]`
   and the fields on a size. `parse_products_array()`'s docstring says what
   each level is expected to hold, and why reading the outer `detail`
   instead gives a product with no price.
3. **The menu** — how grids are reached. It is the only index of grids there
   is; no category id can be looked up instead.

Saying which of the three broke, and pasting the payload fragment rather than
describing it, turns a bug report into a fix.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a
single file of plain functions with inline HTML fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Several properties in this repo exist because they were once absent, cost
real time to find, and are now pinned by a test so a PR that breaks one fails
rather than silently regressing:

- **The outer array entry is not the product.** Every visible entry is a
  `BundleBean` whose own `detail.colors` is empty; the colours, sizes and
  prices hang off `bundleProductSummaries[0]`. Reading the outer `detail`
  gives a product with no price and looks exactly like a parser that works.
- **An error object arrives inside HTTP 200**, positionally, with no id on
  it: `{"description": "Item not found", "key": "_ERR_PRODUCT_NOT_FOUND"}`.
  `parse_products_array()` returns those separately; never let one become a
  row of `None`s.
- **A row's `product_id` must not depend on arrival order.** Several entries
  can share one bundle and the grid promises no order, so the lowest id
  wins. Before that was fixed, two runs of one category attributed all 131
  rows to two different ids, which would have made `diff_runs.py` report
  every row as changed on every run.
- **`size_name` is not unique within a colour.** One colour served two sizes
  both called `XS`, with different `sku`, `partnumber` and country of
  origin. `sku` is the key; nothing else is.
- **robots.txt has three separate `User-agent: *` groups.** A parser that
  stops at the first keeps 28 of 140 rules. `parse_robots()` merges them and
  applies longest-match precedence, which is the only reason the single
  `Allow` in the file means anything.
- **There is no pagination**, so `product_parser.page_url()` returns its
  input unchanged and a grid is one fetch. Do not make it construct a paged
  URL: the storefront's own paging parameters (`sort=`, `price=`, `size=`)
  are robots-disallowed. Breadth comes from the menu's 585 grids.

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows, `5` remote error, `6` partial. A
  pipeline branches on these.
- **An EMPTY result is never retried and never counted as blocked.** A grid
  that genuinely lists nothing is a correct answer to the question that was
  asked, not a failure. `page_flow.STATE_POLICY` holds that for every engine
  so they cannot disagree about it.
- **The edge refusal and the interstitial are different walls.** One is
  fixed by a different exit and the other by waiting; `is_edge_refusal()`
  and `is_self_clearing_challenge()` are deliberately separate and the 403
  matches only the first. Do not fold them together.
- **A `sku` already written earlier in the same run is dropped, not
  duplicated.** See `dedupe_by_key` in `output_writer.py`. A `sku` here is a
  (product, colour, size) triple rather than a product, so one dress in four
  colours and eight sizes is 32 of them.

There is also a naming check: certain phrases are banned repo-wide and the
suite fails naming them. If it trips, read the message — the phrase is wrong
for a reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it
  that way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has
  needed an explicit timeout its own API does not provide, and each has
  needed its own route out of the runtime — reporting a timeout is not the
  same as exiting on one. If you add a call to a remote browser or API,
  bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is a
  common bug class. A selector that matches the *wrong* element is worse
  than one that matches nothing, because the second one tells you.
- **Never present a guess as a fact.** A price that could not be read is
  `None`, not `0.0` — see `money()`, which returns `None` when the store
  config supplied no exponent rather than silently scaling by one. A row
  carries a currency only when the store config that produced it did.

### If your change needs a live run

Most do not — the suite covers the parser, the walk, the writers, the robots
matcher and the CLI contract against inline fixtures cut from real payloads.

If yours genuinely needs bershka.com, say in the PR what you ran
(`--category`, `--max-grids`, which engine), whether you used a proxy, and
what you got — including the exit code and `.meta.json`'s `stop_reason`.

Be aware that **you cannot develop this from an unproxied address**: every
URL on this host, `/robots.txt` included, answered HTTP 403 in 195 bytes from
a residential address on 2026-09-19. "It returned nothing" without saying
what exit you used is not a reproducible report.

Do not add anything that requires logging in, or that submits a form on the
site. This project only reads what an anonymous visitor is served.

## Scope

This repo reads Bershka's **public catalogue** through the same API the
storefront itself calls, exactly as an anonymous visitor is served it. Out of
scope: anything behind a login, anything that submits a form, anything under
`/itxrest/1/marketing/` or any other path robots.txt disallows, and anything
that defeats a protection rather than passing it the way an ordinary browser
does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
