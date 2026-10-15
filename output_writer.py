"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers used by every engine in this repo.

One kind of row
---------------
Bershka is a catalogue, so this repo carries one `Product` dataclass. The
first ten columns are the family prefix -- `source`, `scraped_at`, `url`,
`sku`, `title`, `image_url`, `price`, `currency`, `category`,
`price_source` -- so a consumer already written against another repo in this
family reads them unchanged. Everything Bershka-specific is appended after
them.

`sku` is the catalogue API's own `sizes[].sku`: one row per (product,
colour, size), because that is the only identifier in the payload that is
unique per row. See the comment on the field below for the measurement.

Columns that are NOT here
-------------------------
- `rating` / `review_count`: the catalogue API publishes neither, and a
  column that is null on every row of every run should not exist.
- `description`: not part of the grid / productsArray payload this repo
  reads; adding it would mean a request per product.
- a 30-day-lowest-price column: no such figure appears in the productsArray
  payload. `original_price` is the API's own `oldPrice`.
"""

import contextlib
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
# to hand to diff_runs.py. Both qualify: a product can sit in several grids,
# and the walk dedupes on `sku` across all of them.
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


# CSV cannot hold a list (`related_categories`, `categories`). Joining with " | " keeps the cell
# readable in a spreadsheet and round-trippable by splitting on the same
# separator; the JSON output keeps the real list.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v


# A spreadsheet treats a cell beginning with one of these as a FORMULA rather
# than as text. Every string column here (titles, colour names, category
# trails) is text the site's merchandisers wrote, not this code, so a CSV
# opened in Excel, LibreOffice or Google Sheets must not execute it. The tab
# and the line breaks are here because some readers strip a leading one and
# expose whatever follows it.
CSV_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r", "\n")

# Prefixed to such a cell. Spreadsheets read a leading apostrophe as "the
# rest is text" and do not display it; a program reading the CSV with `csv`
# sees it, which is why the count goes into the sidecar.
CSV_FORMULA_ESCAPE = "'"


def _csv_escape(v: Any):
    """Neutralise a formula-shaped cell. Returns `(value, was_escaped)`.

    Only STRINGS are touched: escaping a number would turn `-5` into the text
    `'-5` and break every sum over the column. CSV only -- the JSON output
    keeps the site's bytes exactly, and `csv_cells_escaped` in the sidecar
    declares that the two files differ.
    """
    if isinstance(v, str) and v.startswith(CSV_FORMULA_LEADS):
        return CSV_FORMULA_ESCAPE + v, True
    return v, False


@contextlib.contextmanager
def _atomic(path: str, newline: Optional[str] = None):
    """Write through a temporary file in the same directory, then replace.

    `open(path, "w")` truncates before a byte is written, so a crash, a kill
    or a full disk half way through left a shorter file where yesterday's
    good one was -- and the three outputs could disagree with each other.

    * The temporary file sits in the TARGET's directory: `os.replace` is
      atomic only within one filesystem.
    * `flush` + `fsync` before the rename, so the content is durable and not
      merely visible.
    * Mode: `NamedTemporaryFile` creates 0600 and a rename keeps that, which
      would make every output owner-only. An existing target keeps its own
      mode (someone may have set it on purpose); a new one gets what
      `open()` would have given it, `0o666 & ~umask`.
    * On any exception the temporary file is removed and the old target is
      left untouched.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline=newline, dir=directory,
        prefix=os.path.basename(path) + ".", suffix=".tmp", delete=False)
    try:
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def write_json(rows: Sequence[Any], path: str) -> None:
    with _atomic(path) as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> int:
    """Write the CSV and return how many cells were formula-escaped.

    An empty result still gets the header row, from `row_cls` rather than
    the first row, so a mode that finds nothing still writes its columns.
    `newline=""` is the csv module's requirement and is passed through to
    the temporary file, or every row gains a stray CR on Windows.
    """
    fieldnames = [f.name for f in fields(row_cls)]
    escaped = 0
    with _atomic(path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            row = {}
            for k, v in asdict(r).items():
                # AFTER `_csv_value`: a list joined into one cell is text too,
                # and its first element can be formula-shaped.
                value, was_escaped = _csv_escape(_csv_value(v))
                row[k] = value
                escaped += was_escaped
            writer.writerow(row)
    return escaped


# Exit code for a run that asked and was told "nothing": the category or
# product genuinely holds no rows.
EXIT_NO_PRODUCTS = 4

# Exit code for a run refused before it had any rows: the exit address was
# refused by the edge, or robots.txt disallows the URL.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early.
EXIT_PARTIAL = 6

# Exit code for a run that holds NOTHING because the content was never
# obtained: a request that kept failing (5xx, timeout, dropped connection), a
# response that was not the JSON the endpoint promises, a served payload that
# parsed to zero rows, or one of this project's own remote calls (the
# Fingerprint API, a Scraping Browser connect, the Scraper API) failing on its
# own terms. Different from 4 on purpose: 4 is a fact about the catalogue, 5
# says nothing can be concluded about it from this run. A run that gathered
# rows and THEN failed is 6, never 5.
#
# A captcha-solve failure is not on this list: it is a warning, the run
# continues, and the page it was meant to clear decides the outcome.
EXIT_REMOTE_API_ERROR = 5
EXIT_FETCH_FAILED = EXIT_REMOTE_API_ERROR


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
    with _atomic(path) as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
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
             scope: Optional[dict] = None,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `extra` is merged at the top level; a run field wins a name collision, so
    a caller cannot overwrite `status` by accident.
    """
    meta = {
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
    if extra:
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product,
         stats: Optional[dict] = None) -> int:
    """Write JSON/CSV and return a process exit code.

    On zero rows, nothing is written at all unless `allow_empty`: a run that
    finds nothing must not silently replace the previous good output with an
    empty file, because a consumer cannot tell an empty category from a
    failed run.

    The number of formula-escaped CSV cells is reported through `stats`
    rather than the return value, because the return value IS the exit code.
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
        escaped = write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.csv")
        if stats is not None:
            stats["csv_cells_escaped"] = escaped
        if escaped:
            print(f"[!] {escaped} CSV cell(s) began with a formula character "
                  f"and were prefixed with {CSV_FORMULA_ESCAPE!r} so a "
                  f"spreadsheet reads them as text. The JSON output is "
                  f"unchanged — see csv_cells_escaped in the sidecar.")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")

# Stop reasons under which ZERO rows is a true answer about the catalogue
# rather than a failure to read it: every request succeeded, or the menu /
# the product id itself said there was nothing to read. Only these may end a
# zero-row run with EXIT_NO_PRODUCTS.
EMPTY_ANSWER_STOP_REASONS = COMPLETE_STOP_REASONS + (
    "no_grid_matched_category", "menu_listed_no_grids", "product_not_found")

# Stop reasons that mean the site refused this run's exit. With no rows they
# are exit 3 even when the engine did not raise, because the walk records an
# edge refusal met mid-run on its result instead of raising it away.
BLOCKED_STOP_REASONS = ("edge_refusal", "robots_refusal")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[Any]] = None,
               mode: str = "category", source: str = SOURCE_DEFAULT,
               scope: Optional[dict] = None,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by every engine so the status/exit-code mapping cannot drift:

        rows, complete              0
        rows, not complete          6  (partial; output written)
        no rows, refused            3
        no rows, a true empty       4  (only for EMPTY_ANSWER_STOP_REASONS)
        no rows, anything else      5  (the content was never obtained)

    The zero-row branch is keyed on an allowlist of EMPTY answers, not on a
    list of failures: a stop reason nobody has named yet falls through to
    "nothing can be concluded" (5), never to "the category is empty" (4).

    The sidecar is written only when the row files are. A zero-row failure
    writes nothing at all -- not even with `--allow-empty`, which exists for
    a genuinely empty answer -- so the previous good output is not left next
    to a "failed" sidecar that contradicts it.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)

    if not rows:
        if blocked or stop_reason in BLOCKED_STOP_REASONS:
            print(f"[!] Refused before any rows were read ({stop_reason}) — "
                  f"exit {EXIT_BLOCKED}. Nothing was written.")
            return EXIT_BLOCKED
        if stop_reason not in EMPTY_ANSWER_STOP_REASONS or pages_failed:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be concluded "
                  f"about the catalogue from this run, and nothing was written.")
            return EXIT_FETCH_FAILED

    write_stats: dict = {}
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls,
              stats=write_stats)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        # Zero rows reach here only as a true empty answer under
        # --allow-empty: "complete" when every request succeeded, "empty"
        # when the menu or the product id said there was nothing to read.
        if rows:
            status = "complete" if complete else "partial"
        else:
            status = "complete" if complete else "empty"
        # Merged UNDER the caller's extra: an engine's own keys win, because
        # a collision here would be housekeeping silently dropping site data.
        if write_stats:
            extra = {**write_stats, **(extra or {})}
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source, scope=scope,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
