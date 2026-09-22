#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku`.

    python3 diff_runs.py --old bershka_products.2026-09-01.json \\
                          --new bershka_products.2026-09-08.json

Typical use is a scheduled re-run of one category, kept under a dated
filename and diffed against the previous one to track price moves,
restocks and delistings.

Five buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new (delisted, or
                   just off this particular listing)
  changed        — sku in both, with a different tracked field
  source_changed — sku in both, whose price differs AND whose
                   `price_source` differs. Reported separately and ignored
                   by --fail-on-change, because it says something about our
                   own two readings, not about the site. See below.
  unmatched      — a row this project's parser could not recover a sku for,
                   counted rather than folded into added/removed

Why `source_changed` exists here when the transfermarkt sibling has no such
bucket: this site has FOUR ways of stating a price and they are not equally
trustworthy — a tile's microdata, a gift-set tile's bare text, a product
page's JSON-LD, and a product page's rendered price (see
output_writer.Product.price_source). A category run and a product run of the
same sku therefore read the same price through different instruments, and
so do two category runs if the site changes a tile from a set-price block to
a normal one. Reporting that as a price change would be reporting our own
instrument, which is exactly the false "price changed" CLAUDE.md §8 forbids.

There is no `--price-tolerance-pct`: every row of one run comes from one
locale and therefore one currency (`price` and `currency` are read together
from the same node), so there is no exchange-rate tick to absorb. A price on
this site moves in whole currency units, not continuously.

A run against a showcase locale (`int/en`, `ru`) has `price=None` on every
row. Diffing two of those is legitimate and reports title, availability and
delisting changes; it will simply never report a price change, because the
site publishes none.
"""

import argparse
import json
import re
import sys
from typing import Dict, List, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

TRACKED_FIELDS = (
    # Family prefix — the two every repo in this family diffs.
    "price", "currency",
    # What actually moves on this catalogue between two runs. A row here is
    # one SKU — a (product, colour, size) triple — so these are all
    # properties of that exact SKU and not of the product it belongs to:
    # a markdown starting or ending (`original_price` goes from None to a
    # figure and back), the size going out of stock or being flagged for
    # restock, a rename, and the promotion behind a discount changing.
    "original_price", "discount_pct", "availability", "title",
    "is_buyable", "back_soon", "promotion_id",
    # NOT tracked, and each for its own measured reason:
    #
    #   `image_url`      — carries a `?ts=` cache-buster that moves without
    #                      the image moving.
    #   `scraped_at`     — the clock, not the catalogue.
    #   `page`/`row_index` — a row's position inside a payload. Measured
    #                      2026-09-20: two runs of one category twenty
    #                      minutes apart produced identical rows with 44 of
    #                      131 row_index values changed, because the grid
    #                      reordered. Tracking it would report every such
    #                      run as 44 changes and nothing real.
    #   `color_name`/`size_name` — part of the SKU's identity, not a
    #                      property that changes under it. If one of them
    #                      changed for a given `sku`, the right report is
    #                      that the site reused an id, which is a different
    #                      and louder event than a field edit.
    #   `related_categories` — merchandising. A product moving between
    #                      collections is not a change to the product.
)


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(rows: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for r in rows:
        sku = r.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = r
    return indexed, unmatchable


def diff_rows(old: List[dict], new: List[dict]) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed = []
    source_changed = []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue
        entry = {"sku": sku, "title": after.get("title"),
                 "changes": field_changes}
        # A price difference that arrives WITH a price_source difference is
        # our two instruments disagreeing, not the shelf price moving. It
        # goes in its own bucket and --fail-on-change ignores it.
        if "price" in field_changes and \
                before.get("price_source") != after.get("price_source"):
            entry["price_source"] = {"old": before.get("price_source"),
                                     "new": after.get("price_source")}
            source_changed.append(entry)
        else:
            changed.append(entry)

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result.get('source_changed', []))} read differently.")
    for r in result["added"]:
        print(f"  + {r.get('sku')}  {r.get('title')}  {r.get('price')} {r.get('currency')}")
    for r in result["removed"]:
        print(f"  - {r.get('sku')}  {r.get('title')}  {r.get('price')} {r.get('currency')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result.get("source_changed", []):
        src = c.get("price_source", {})
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[read differently: {src.get('old')!r} -> {src.get('new')!r}, "
              f"not counted as a price change]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str):
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


# What two runs must agree on before a row-level diff means anything.
# Comparing a `gb`/GBP run with a `de`/EUR one used to be allowed: the same
# SKUs, every price different, and the tool reported it as change over time
# rather than as two markets. Each key here is checked independently so the
# message can say WHICH one differs.
SCOPE_KEYS = ("store_id", "locale", "category", "max_grids", "max_skus",
              "schema_version")


def _scope_problems(args) -> List[str]:
    """Refuse a diff across markets, categories or schema versions.

    Missing scope means the run predates this metadata; that is reported as
    unknown rather than waved through, because "no scope recorded" and "same
    scope" are not the same statement.
    """
    if getattr(args, "force", False):
        return []
    scopes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        _, meta = _run_status(path)
        scopes[label] = ((meta or {}).get("scope") or None)

    if scopes["--old"] is None or scopes["--new"] is None:
        missing = [l for l, s in scopes.items() if s is None]
        return [f"{' and '.join(missing)} carr{'y' if len(missing) > 1 else 'ies'} "
                f"no scope in its metadata, so this tool cannot tell whether "
                f"the two runs covered the same market and categories. Re-run "
                f"with a current version, or pass --force if you know they "
                f"match."]

    out = []
    for key in SCOPE_KEYS:
        old, new = scopes["--old"].get(key), scopes["--new"].get(key)
        if old != new:
            out.append(f"the runs differ in {key}: {old!r} against {new!r}")
    old_grids = (scopes["--old"].get("grids") or {})
    new_grids = (scopes["--new"].get("grids") or {})
    if old_grids.get("digest") != new_grids.get("digest"):
        out.append(
            f"the runs covered different grids ({old_grids.get('count')} against "
            f"{new_grids.get('count')}, different contents). Rows present in one "
            f"and absent from the other would read as delisted products.")
    if out:
        out.append("Pass --force to compare them anyway, knowingly.")
    return out


def _check_comparable(args) -> bool:
    """Refuse a diff between runs that are not both complete, or that are
    different modes. See mediamarkt-scraper's diff_runs.py for the full
    rationale — unchanged here except that this repo's modes are named
    category/product.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode and mode not in UNIQUE_BY_SKU_MODES:
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which this tool does "
                f"not know how to diff.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        problems.append(
            f"the two runs are different modes ({modes}), so added/removed "
            f"would describe the mode change rather than the data.")

    problems += _scope_problems(args)
    if not problems:
        return True

    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare "
          "anyway (added/removed will include rows that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two bershka-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was "
                        "partial or failed, the modes differ, or the two runs "
                        "covered different markets, categories or schema "
                        "versions. Deliberate, not a default.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_rows(old, new)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
