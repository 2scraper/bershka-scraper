"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers used by all three scrapers.

One kind of row
---------------
Bershka is a catalogue, so this repo carries the family's ordinary
single `Product` dataclass rather than the two classes transfermarkt-scraper
needed for people and events. The first nine columns are the family prefix —
`source`, `scraped_at`, `url`, `sku`, `title`, `image_url`, `price`,
`currency`, `category` — so a consumer already written against another repo
in this family reads them unchanged.

`sku` is the VARIANT id (`P000476`), not the master/style id, because the
variant is what is actually unique per row: a category tile, the PDP's own
JSON-LD `sku`, and the tile's `data-pid` all agree on it, while one master id
(`F20100269`) covers up to nineteen shades of the same lipstick. The master
id is kept beside it in `master_id` — it is what the product URL carries, so
a consumer needs both to get from a row back to a page.

Columns that are NOT here, and the measurements that removed them
----------------------------------------------------------------
- `lowest_price_30d`: the EU Omnibus 30-day-low disclosure CLAUDE.md §4
  warns about. Measured 2026-09-18 over 317 tiles in 20 captures spanning
  `us`, `gb`, `int/en`, `jp` and `ru`: zero occurrences of a second struck
  price of any kind. The one struck price found is a LIST price ABOVE its
  sale price (£134.00 against £105.20), which is what `original_price`
  holds. Add the column back with a measurement showing the disclosure, not
  by analogy with mediamarkt-scraper.
- `description`: the PDP's JSON-LD carries one (several hundred words of
  marketing copy). Deliberately not exported — it would dominate every CSV
  row of a price-monitoring run. `product_parser.parse_product` reads the
  JSON-LD whole, so a caller that wants it has it.
- `rating` / `review_count`: the tiles carry a Bazaarvoice placeholder
  (`pr-category-snippet`) that is EMPTY in server-rendered HTML — the widget
  fills it client-side. Measured 0 populated ratings across the same 317
  tiles. A column that is null on every row of every run should not exist.
"""

import csv
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# One platform, one hostname: every locale of Bershka is a PATH
# prefix on www.bershka.com (`/us/`, `/gb/`, `/int/en/`), not a
# country TLD the way this family's MediaMarkt and Transfermarkt repos have.
# So `source` is constant here and the locale lives in its own column.
SOURCE_DEFAULT = "bershka.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # One row per SKU, and on this site a SKU is a (product, colour, size)
    # triple — not a product. Measured 2026-09-19 on the grid
    # `87687eba-fdc3-4348-8570-972923c68698` ("WOMEN / SALE / Bershka /
    # Dresses and jumpsuits"): 13 products carried 4 colours each and 8 sizes
    # per colour. `sku` is the API's own `sizes[].sku`, which is the only
    # field in the payload that is unique per row — size NAME is not. The
    # same colour of one dress served two sizes both called "XS", with
    # different `sku`, different `partnumber` and different `country`
    # ("MAINLAND CHINA" and "CAMBODIA"). Keying on size name would silently
    # collapse them.
    sku: Optional[str] = None
    title: Optional[str] = None
    image_url: Optional[str] = None
    price: Optional[float] = None
    # Never defaulted and never guessed from a symbol. The API returns price
    # as a string of minor units with no currency anywhere in the product
    # payload (`"price": "2099"`); the currency is a property of the STORE,
    # read from `store_config.details.locale.currencyCode` ("GBP") and scaled
    # by `currencyDecimals` (-2), both measured 2026-09-19 on store 44009506.
    # A row therefore only carries a currency when the store config that
    # produced it did.
    currency: Optional[str] = None
    category: Optional[str] = None     # the menu trail this row came from
    # Where `price` and `currency` were read:
    #   "api-size"   — `bundleProductSummaries[].detail.colors[].sizes[].price`,
    #                  the only price the catalogue API publishes, and the
    #                  source of all 131 rows in sample_output.
    #   "api-bundle" — a bundle that prices itself rather than its sizes.
    #                  Defined because the payload has the shape; NOT seen in
    #                  any capture yet, and the writer records it rather than
    #                  folding it into "api-size" so a first sighting is
    #                  visible instead of silent.
    # There is no DOM price source in this repo at all: a rendered listing
    # page carries zero products (0 JSON-LD blocks, 0 product ids, 0 grid
    # classes in 938,454 bytes, measured 2026-09-19), so there is no tile to
    # read a price off and no second source to overlay against.
    price_source: Optional[str] = None

    # ---- Bershka-specific, appended so the family prefix above is stable ----
    locale: Optional[str] = None       # storefront path prefix, e.g. "gb"
    store_id: Optional[int] = None     # 44009506 for gb — the API's own key
    product_id: Optional[int] = None   # the grid's id; the one the URL carries
    bundle_id: Optional[int] = None    # `bundleProductSummaries[].id`, the
                                       # record that actually holds the detail
    reference: Optional[str] = None    # "01218714-I2026", the style reference
    display_reference: Optional[str] = None   # "1218/714", what the site shows
    color_id: Optional[str] = None     # "800"
    color_name: Optional[str] = None   # "Black"
    size_name: Optional[str] = None    # "XS" — NOT unique within a colour
    size_id: Optional[str] = None      # `mastersSizeId`, e.g. "101"
    partnumber: Optional[str] = None   # "0121872980001-I2026"
    # `isBuyable` and `backSoon` are separate fields and disagree on purpose:
    # a size can be buyable now, or out of stock with a restock flagged. This
    # column folds them into the family's vocabulary and the two raw fields
    # are kept beside it rather than thrown away.
    availability: Optional[str] = None      # "InStock" / "OutOfStock" / "BackSoon"
    is_buyable: Optional[bool] = None
    back_soon: Optional[bool] = None
    original_price: Optional[float] = None  # `oldPrice`, the struck price
    # Read from `discountsPercentages.oldPriceDiscount` when the payload
    # states it, and computed from the pair when it does not. Which of the
    # two happened is not guessed at: see `discount_source`.
    discount_pct: Optional[float] = None
    discount_source: Optional[str] = None   # "api" / "computed"
    promotion_id: Optional[int] = None
    section: Optional[str] = None      # "WOMEN"
    family: Optional[str] = None       # familyName, e.g. "DRESSES"
    subfamily: Optional[str] = None    # subFamilyName
    grid_id: Optional[str] = None      # the grid this row was collected from
    # Every category the payload says this product belongs to, by name. This
    # is the honest answer to a limitation of `category`: a row's `category`
    # names the FIRST menu trail that reached it, because a walk dedupes on
    # sku and a product sits in several grids. `related_categories` is the
    # site's own list and does not depend on the order a walk happened to
    # take. Measured 2026-09-19: one dress carried 3 of them.
    related_categories: Optional[List[str]] = None
    # Every menu trail this run reached the SKU through, not just the first.
    # `category` is the first and is kept as-is so the column's meaning does
    # not change under anyone reading it; this is the honest answer to "which
    # categories is it in", which a single value cannot give.
    categories: Optional[List[str]] = None
    country_of_origin: Optional[str] = None  # sizes[].country
    page: Optional[int] = None
    row_index: Optional[int] = None


# Row class by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {
    "category": Product,
    "product": Product,
}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both qualify, and deduping is not theoretical
# here: one `gb` listing served the same tile twice (`F20100141`, 25 tiles
# over 24 distinct ids, measured 2026-09-18).
UNIQUE_BY_SKU_MODES = ("category", "product")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list (nationalities). Joining with " | " keeps the cell
# readable in a spreadsheet and round-trippable by splitting on the same
# separator; the JSON output keeps the real list.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def _atomic(path: str, write: Any) -> None:
    """Write through a temporary file in the same directory, then replace.

    The three outputs used to be written straight to their final names, so a
    failure between them — a full disk, an interrupt — left a fresh JSON
    beside yesterday's CSV and a metadata sidecar describing neither. Nothing
    downstream could tell. `os.replace` is atomic on the same filesystem, so
    a reader sees either the old file or the new one.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="",
                                         dir=directory, delete=False)
    try:
        with handle:
            write(handle)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def write_json(rows: Sequence[Any], path: str) -> None:
    _atomic(path, lambda f: json.dump([asdict(r) for r in rows], f,
                                      ensure_ascii=False, indent=2))


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row, from `row_cls` rather than
    # the first row, so a mode that finds nothing still writes the columns
    # that mode would have used.
    fieldnames = [f.name for f in fields(row_cls)]

    def _write(f):
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})

    _atomic(path, _write)


# Exit code used when a run completes but produced nothing.
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early.
EXIT_PARTIAL = 6

# Exit code for a failure in one of THIS PROJECT's own 2Captcha-product calls
# -- the Fingerprint API rejecting a request (bad key, bad --tags, rate
# limit), or a Scraping Browser CDP connection failing (e.g. profile_locked)
# -- as opposed to EXIT_BLOCKED (the TARGET SITE refusing a page) or an
# uncaught crash (1). Per CLAUDE.md's family exit-code contract ("5" =
# "remote API error"). Deliberately NOT what a captcha-solve failure gets:
# per that same document's captcha section, a solver error is a WARNING that
# lets the run continue (see captcha_solver.py and each engine's
# handle_captcha_if_present) -- exit 5 is for calls the user explicitly
# opted into (--fingerprint, --cdp-endpoint) where silently continuing
# without them would hide a billing/plan/profile-lock problem rather than a
# page the site declined to serve.
EXIT_REMOTE_API_ERROR = 5


class RemoteAPIError(RuntimeError):
    """A 2Captcha product call (Fingerprint API, Scraping Browser CDP
    connect) failed on its own terms, not the target site blocking a page.

    Raised by fingerprint_client.get_fingerprint and by each engine's
    --cdp-endpoint connect path; caught once at each engine's entry point
    and mapped to EXIT_REMOTE_API_ERROR, so the three engines cannot drift
    on which of 1 (crash) / 3 (blocked) / 5 (remote API error) a given
    failure gets -- before this, both paths raised a bare RuntimeError with
    no engine catching it, so either failure reached the interpreter as an
    unhandled exception and exited 1 (a raw traceback, no run-metadata
    sidecar) regardless of which one it actually was.
    """


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Written LAST and atomically, so the sidecar is the commit point: if it is
    there, the JSON and CSV beside it are the ones it describes.
    """
    path = f"{out_prefix}.meta.json"
    _atomic(path, lambda f: json.dump(meta, f, ensure_ascii=False, indent=2))
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def schema_version() -> str:
    """A short fingerprint of the row schema, so a diff can refuse to compare
    a run written before a column changed with one written after."""
    names = ",".join(f.name for f in fields(Product))
    return hashlib.sha256(names.encode()).hexdigest()[:12]


def scope_fingerprint(store_id: Optional[int] = None, locale: Optional[str] = None,
                      category: Optional[str] = None,
                      grid_ids: Optional[Sequence[str]] = None,
                      max_grids: Optional[int] = None,
                      max_skus: Optional[int] = None) -> dict:
    """What a run actually covered, in the fields a diff must agree on.

    Without this, `diff_runs.py` compared only status and mode — so two
    complete runs of the SAME sku in `gb`/GBP and `de`/EUR were considered
    comparable and every row looked like a price change. The currency change
    would be reported, but the tool did nothing to stop the false alert, and
    the metadata did not even carry the market.

    `grids` is a hash rather than the list: a run over 400 grids should not
    put 400 ids in a sidecar, and the only question a diff asks of it is
    whether the two runs covered the same set.
    """
    ids = sorted(str(g) for g in (grid_ids or []))
    digest = hashlib.sha256("|".join(ids).encode()).hexdigest()[:12] if ids else None
    return {
        "store_id": store_id,
        "locale": locale,
        "category": category,
        "grids": {"count": len(ids), "digest": digest},
        "max_grids": max_grids,
        "max_skus": max_skus,
        "schema_version": schema_version(),
    }


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "category", source: str = SOURCE_DEFAULT,
             scope: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run."""
    return {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "scope": scope if scope is not None else scope_fingerprint(),
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    On zero rows, nothing is written at all unless `allow_empty` — see the
    family invariant in CLAUDE.md §8: a run that finds nothing must not
    silently replace yesterday's good output with an empty file.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "category", source: str = SOURCE_DEFAULT,
               scope: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them. See mediamarkt-scraper's output_writer.py for
    the full rationale; the logic here is unchanged.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source, scope=scope,
            start_url=start_url, final_url=final_url, products=len(rows)))

    if not rows:
        return EXIT_BLOCKED if blocked else rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
