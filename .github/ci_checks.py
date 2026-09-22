#!/usr/bin/env python3
"""
CI checks that are too long to live inside the workflow YAML.

They started out as heredocs in `.github/workflows/tests.yml` and moved here
for one practical reason: a Python block nested inside YAML inside a shell
`run:` needs three levels of quoting to stay intact, and shell-quoted regexes
like '(ws|wss)://[^ "'"'"']+' do not survive being copied through a browser.
A separate .py file is copy-paste safe, runs locally, and can be read on its
own.

Run any of these from the repo root:

    python .github/ci_checks.py --help-check
    python .github/ci_checks.py --sample-check
    python .github/ci_checks.py --secret-check
    python .github/ci_checks.py --all

Each prints what it looked at and exits non-zero on failure.
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Engine libraries are deliberately absent in CI — the offline suite does not
# need them. An ImportError naming one of these is expected, not a failure.
ENGINE_LIBS = ("playwright", "pyppeteer", "selenium", "webdriver_manager")

CLIS = ["playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py",
        "scraper_api_client.py", "fingerprint_client.py", "env_config.py",
        "diff_runs.py", "tools/browser_profile_client.py"]

SAMPLE_FILES = ("sample_output.json", "sample_output.csv")

# Phrases that show up in hand-written or template sample data. The point of
# committing a sample is that it came from a real run; a placeholder teaches
# readers field names and value shapes that do not exist. Kept generic
# (rather than site-specific) on purpose: a real row here is a real
# player or transfer, and there is no fixed set of "fake-looking" names to
# check for the way an e-commerce sibling checks for "sample product".
FABRICATION_MARKERS = ("sample-product-", "example brand", "sample product",
                       "product description text", "lorem ipsum",
                       "your_api_key", "123456789")

# A URL carrying real credentials — the shape is scheme://something:something@host
# (deliberately not spelled out as an example here: this file scans itself, and
# an illustrative credential in a comment is a false positive that turns the
# build red for no reason. It happened on the first run, in this family's
# mediamarkt-scraper.)
CREDENTIALLED_URL = re.compile(r"(?:ws|wss|https?)://[^\s\"'/]+:[^\s\"'/]+@")

# Documented placeholders and test values, which are SUPPOSED to look like the
# real thing — that is the point of them. Each entry earns its place by being
# in a line whose job is to show the shape of a credential or to prove the
# masker removes one; a real secret matches none of these.
#
# Kept as an explicit list rather than a loose pattern so that adding one is a
# decision. The alternative — a regex broad enough to cover them all — would
# also cover a real login.
CREDENTIAL_ALLOWED = ("USER:PASS", "user:pass", "ACCOUNT:PASSWORD",
                      "{login}", "{user}", "{password}", "{profileId}", "{cc}",
                      "***", "password}@", "u:p@h", "LOGIN:PASSWORD",
                      "user:secret@",                  # the masking fixtures
                      "login:password@host:port",      # a refusal message
                      "u:supersecret@", "login:supersecret@",
                      "u:pass@h1", "u:pass@h2",
                      "a:b@",                          # env_config.py's own precedence fixtures
                      "a:b@from-dotenv")

# A 2captcha API key is a 32-character hex string.
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
HEX32_ALLOWED = ("sha", "hash", "nonce", "example", "md5", "digest", "checksum")

SCANNED_SUFFIXES = (".py", ".md", ".txt", ".yml", ".yaml", ".example")


def git_ignored(paths):
    """The subset of `paths` git would refuse to commit, or an empty set.

    ASK GIT rather than matching strings. `.gitignore` has patterns,
    negations and directory scoping, so a substring test proves nothing
    about what would actually be committed -- and this check's whole claim
    is about what IS committed.

    Two ways this must not break, because this project ships as a zip as
    often as it is cloned:
      * outside a git repository, and
      * with `git` absent from PATH
    it returns an empty set, so nothing is filtered and the scan behaves
    exactly as it did before. A guard that takes the check down is worse
    than the gap it closes.

    One subprocess for the whole list, not one per file: `--stdin` exists
    for this, and `-z` keeps filenames with spaces or newlines intact --
    this repo lives under a path with a space in it.
    """
    paths = list(paths)
    if not paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO), "check-ignore", "-z", "--stdin"],
            input="\0".join(str(p) for p in paths) + "\0",
            capture_output=True, text=True)
    except (OSError, ValueError):
        return set()               # no git on PATH
    # 0 = some ignored, 1 = none ignored, 128 = not a repository.
    if proc.returncode not in (0, 1):
        return set()
    return {p for p in proc.stdout.split("\0") if p}


def scanned_files():
    candidates = []
    for path in sorted(REPO.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if any(part in {".git", "__pycache__", ".venv", "venv"}
               for part in path.parts):
            continue
        candidates.append(path)

    # A gitignored file is by definition not committed, so flagging one is a
    # false positive -- and a permanently red check is one everybody learns
    # to ignore. The maintainer's own `proxylist.txt` sat on disk and turned
    # this red while CI, which never has that file, stayed green.
    ignored = git_ignored(candidates)
    skipped = 0
    for path in candidates:
        if str(path) in ignored:
            skipped += 1
            continue
        yield path
    if skipped:
        print(f"note     {skipped} gitignored file(s) not scanned — they are "
              f"not committed, so they cannot leak through this repo")


def help_check():
    failed = []
    for name in CLIS:
        script = REPO / name
        if not script.is_file():
            print(f"missing  {name}")
            failed.append(name)
            continue
        result = subprocess.run([sys.executable, str(script), "--help"],
                                capture_output=True, text=True, cwd=REPO)
        if result.returncode == 0:
            print(f"ok       {name}")
            continue
        blob = result.stdout + result.stderr
        if "ModuleNotFoundError" in blob and any(lib in blob for lib in ENGINE_LIBS):
            print(f"skipped  {name} (engine library not installed here)")
            continue
        print(f"FAILED   {name}\n{blob}")
        failed.append(name)
    return failed


def sample_check(prefix="sample_output"):
    """Check `{prefix}.json` and `{prefix}.csv` against the live schema.

    `prefix` exists so the canary workflow can point this at the output of a
    real run instead of carrying its own copy of these assertions inline.
    That is CLAUDE.md §11: a workflow step calls the shipped check. The
    sibling repo's first push went red because `tests.yml` had re-implemented
    a check instead, and the copy drifted.
    """
    failed = []
    names = (f"{prefix}.json", f"{prefix}.csv")
    for name in names:
        if not (REPO / name).is_file():
            failed.append(f"{name} is missing — regenerate it from a real run")
    if failed:
        return failed

    rows = json.loads((REPO / names[0]).read_text(encoding="utf-8"))
    if not rows:
        return [f"{names[0]} is empty — a run that found nothing is not a sample"]

    blob = json.dumps(rows).lower()
    hits = [m for m in FABRICATION_MARKERS if m in blob]
    if hits:
        failed.append(f"{names[0]} looks fabricated: {hits}")

    # The committed sample doubles as a schema test: rename a field in the code
    # and forget the sample, and this fails rather than the docs going stale.
    # It is a --mode category sample from a PRICED locale, so it exercises
    # every column a showcase-locale run would leave null.
    sys.path.insert(0, str(REPO))
    from dataclasses import asdict

    from output_writer import Product
    expected = list(asdict(Product()).keys())

    for i, row in enumerate(rows):
        if list(row.keys()) != expected:
            failed.append(f"{names[0]} row {i}: columns differ from "
                          f"output_writer.Product")
            break

    with (REPO / names[1]).open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    if header != expected:
        failed.append(f"{names[1]} header differs from output_writer.Product")

    if not failed:
        # Price coverage, plus the two figures that are specific to this
        # site: every price here comes from a size inside a bundle, and a
        # struck `oldPrice` is a separate field rather than a second reading
        # of the same one, so discount coverage is a real number and not a
        # derived guess.
        priced = sum(1 for r in rows if r.get("price") is not None)
        discounted = sum(1 for r in rows if r.get("discount_pct") is not None)
        skus = len({r.get("sku") for r in rows})
        print(f"ok       {len(rows)} rows, {len(expected)} columns, "
              f"{priced}/{len(rows)} priced, {discounted} discounted, "
              f"{skus} distinct sku, schema matches")
    return failed


def secret_check():
    failed = []
    scanned = 0
    for path in scanned_files():
        scanned += 1
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            rel = path.relative_to(REPO)

            if CREDENTIALLED_URL.search(line) and not any(
                    token in line for token in CREDENTIAL_ALLOWED):
                failed.append(f"{rel}:{lineno} looks like a URL with real "
                              f"credentials in it")

            for match in HEX32.findall(line):
                if any(token in line.lower() for token in HEX32_ALLOWED):
                    continue
                failed.append(f"{rel}:{lineno} contains {match[:6]}… — a "
                              f"32-char hex string, the shape of a 2captcha key")

    if not failed:
        print(f"ok       {scanned} files scanned, nothing credential-shaped")
    return failed


def run_check(prefix="sample_output"):
    """Assertions about a RUN, not about a schema — for the canary.

    `sample_check` answers "is this output shaped like our rows". This
    answers "did the run that produced it actually finish": the metadata
    sidecar, the row floor, and the two things a silently-degraded run gets
    wrong while still writing a well-formed file.

    Kept here rather than inline in canary.yml because CLAUDE.md §11 is
    explicit about it, and because a floor that lives in YAML is a floor
    nobody runs locally before pushing.
    """
    failed = []
    meta_path = REPO / f"{prefix}.meta.json"
    rows_path = REPO / f"{prefix}.json"
    if not meta_path.is_file():
        return [f"{prefix}.meta.json is missing — the run wrote no metadata"]
    if not rows_path.is_file():
        return [f"{prefix}.json is missing"]

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    rows = json.loads(rows_path.read_text(encoding="utf-8"))

    # Set below every live figure measured on 2026-09-19/20 (131 rows from
    # one category, 1,907 from three) so a merchandising change does not flap
    # the canary, while a parser that stopped expanding bundles does trip it.
    # A row is a SKU, and the smallest category measured still held dozens.
    MIN_ROWS = 20
    if len(rows) < MIN_ROWS:
        failed.append(f"only {len(rows)} rows, expected at least {MIN_ROWS}")

    if meta.get("status") != "complete":
        failed.append(f"run status is {meta.get('status')!r}, not 'complete'")

    sys.path.insert(0, str(REPO))
    from output_writer import SOURCE_DEFAULT
    if meta.get("source") != SOURCE_DEFAULT:
        failed.append(f"run source is {meta.get('source')!r}, not "
                      f"{SOURCE_DEFAULT!r}")

    # Every row is one SKU. Duplicates mean the bundle fold stopped working,
    # which is invisible in a row count and in every per-column check.
    skus = [r.get("sku") for r in rows]
    if len(set(skus)) != len(skus):
        failed.append(f"{len(skus) - len(set(skus))} duplicate sku(s) in the "
                      f"output — one row is meant to be one SKU")

    # A run that fetched but never expanded a colour tree still writes rows.
    if not any(r.get("color_name") and r.get("size_name") for r in rows):
        failed.append("no row carries both a colour and a size — the bundle "
                      "tree was not expanded")

    if not failed:
        print(f"ok       {len(rows)} rows, status={meta.get('status')}, "
              f"{len(set(skus))} distinct sku, "
              f"stop_reason={meta.get('stop_reason')!r}")
    return failed


CHECKS = {"help": help_check, "sample": sample_check, "secret": secret_check,
          "run": run_check}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--help-check", action="store_true",
                        help="Every shipped CLI answers --help")
    parser.add_argument("--sample-check", action="store_true",
                        help="sample_output.* exist, are real, match the schema")
    parser.add_argument("--output-prefix", default="sample_output",
                        help="check this prefix instead of sample_output "
                             "(the canary points it at a live run)")
    parser.add_argument("--run-check", action="store_true",
                        help="A run's metadata and rows say it finished "
                             "(use with --output-prefix)")
    parser.add_argument("--secret-check", action="store_true",
                        help="No credentials committed anywhere")
    parser.add_argument("--all", action="store_true", help="All of the above")
    args = parser.parse_args()

    selected = [name for name in CHECKS
                if args.all or getattr(args, f"{name}_check")]
    if not selected:
        parser.error("pick at least one check, or --all")

    failures = []
    for name in selected:
        print(f"--- {name} check")
        # Only the sample check takes a prefix; passing it blindly to the
        # others would break them the first time one grew an argument.
        result = (CHECKS[name](args.output_prefix) if name in ("sample", "run")
                  else CHECKS[name]())
        failures += [f"[{name}] {line}" for line in result]

    if failures:
        print()
        for line in failures:
            print("FAILED:", line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
