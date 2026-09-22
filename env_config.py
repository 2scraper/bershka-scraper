"""
env_config.py
--------------
Reads a `.env` file sitting next to the scripts, so credentials live in one
place instead of being retyped into every command line.

Two reasons this is hand-rolled rather than `python-dotenv`:

1. **No new dependency.** Every engine here already refuses to be a heavy
   install, and a credential loader is about thirty lines. If `python-dotenv`
   *is* installed it gets used instead, so a project that already depends on it
   keeps its own behaviour.
2. **A secret in argv is visible to anything that can run `ps`.** Passing
   `--twocaptcha-key sk_live_...` leaks the key to every other process on the
   machine and into shell history. Reading it from the environment does not.

Precedence, highest first:

    explicit CLI flag  >  real environment variable  >  .env file  >  default

That order matters: a `.env` must never silently override something the caller
typed, and an already-exported variable (CI secret, `direnv`, a shell profile)
must never be clobbered by a file someone forgot to delete.

Nothing here is required. With no `.env` and no environment variables the
scripts behave exactly as before.
"""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Recognised keys, and which CLI destination each one backs.
# Keeping this explicit means a typo in .env is reported rather than ignored.
ENV_KEYS = {
    "TWOCAPTCHA_KEY": "twocaptcha_key",
    "BERSHKA_CDP_ENDPOINT": "cdp_endpoint",
    "BERSHKA_PROXY": "proxy",
    "BERSHKA_URL": "url",
}
# Deliberately NOT here: an output prefix. `--out` already carries a non-empty
# default, so `apply()` would never see it as unset and the variable would be
# silently ignored — a setting that looks configurable and is not.

# Values that look like a key but are the placeholder from .env.example.
# A placeholder that reaches the API produces a confusing auth error a long way
# from its cause, so it is caught here instead.
_PLACEHOLDERS = {
    "your_2captcha_api_key_here",
    "your_api_key_here",
    "changeme",
    "",
}

# .env.example documents BERSHKA_CDP_ENDPOINT and BERSHKA_PROXY
# the way the vendor itself documents them, with the parts you fill in in
# braces:
#
#     ws://{login}-zone-scraping_browser-country-{cc}-pid-{profileId}:{password}@cb.2captcha.com:9222
#     http://{user}:{password}@eu.proxy.2captcha.com:2334
#
# Neither of those matches a literal in _PLACEHOLDERS, so without this check
# a copied example reads as CONFIGURED: `cp .env.example .env` followed by a
# run connects to cb.2captcha.com with the string `{login}-zone-...` as its
# username and gets a 401 -- a confusing auth error a long way from its
# cause. This exact defect shipped in this family's etsy-scraper and
# mediamarkt-scraper (both of which use braced placeholders) and was absent
# from amazon-scraper/farfetch-scraper only because neither of those uses
# them -- see CLAUDE.md §17. Any `{...}` left in a value is unset here,
# named rather than quoted in the warning, since the value can be a
# credentialled URL.
_BRACED_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")

_loaded_from = None


def _parse_line(line):
    """Return (key, value) or None. Understands `export K=V`, quotes, # comments."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    # Strip one matching pair of quotes; leave inner ones alone.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    else:
        # Unquoted values may carry a trailing comment.
        value = value.split(" #")[0].strip()
    if not key:
        return None
    return key, value


def _is_secret(key, value=""):
    """Should this value be kept out of a log line?

    The same rule `python3 env_config.py` already prints by: a key whose name
    says it holds a credential, or a value carrying `user:pass@`. Erring
    towards hiding -- a duplicate warning names the KEY and the line numbers,
    which is all an operator needs to go and delete the extra lines.
    """
    name = str(key).upper()
    if any(w in name for w in ("KEY", "TOKEN", "PASSWORD", "SECRET")):
        return True
    return "@" in str(value)


def duplicate_keys(path=None):
    """{key: [(line number, value), ...]} for every key set more than once.

    A duplicate is not a style problem here, it is an ambiguity that used to
    resolve differently depending on what happened to be installed. Measured
    2026-09-17 on a fixture with three consecutive BERSHKA_PROXY lines,
    through `load_env()` with its default `override=False`:

        hand-rolled parser  ->  the FIRST value
        python-dotenv       ->  the LAST value

    Same file, same command, a different exit, and no error either way --
    because `python-dotenv` is not in requirements.txt, so which branch runs
    is an accident of the environment. The neighbours were checked at the
    same time and do agree: an empty value, a quoted value and an unquoted
    value with a trailing comment all parse identically in both.
    """
    if path is None:
        path = Path(__file__).resolve().parent / ".env"
    path = Path(path)
    if not path.is_file():
        return {}
    seen = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parsed = _parse_line(raw)
        if parsed:
            seen.setdefault(parsed[0], []).append((lineno, parsed[1]))
    return {k: v for k, v in seen.items() if len(v) > 1}


def _report_duplicates(path):
    """Say which line won, rather than silently picking one.

    The same shape as `unknown_keys()`: the loader does not get to decide
    what the operator meant, but it must not hide that there was a choice.
    """
    for key, occurrences in duplicate_keys(path).items():
        where = ", ".join(str(lineno) for lineno, _ in occurrences)
        winner_line, winner_value = occurrences[-1]
        shown = "(hidden)" if _is_secret(key, winner_value) else repr(winner_value)
        logger.warning(
            "%s is set %d times in %s (lines %s) — the LAST one, line %d, "
            "wins: %s. Delete the others; a duplicate here used to resolve "
            "differently depending on whether python-dotenv was installed.",
            key, len(occurrences), path, where, winner_line, shown)


def load_env(path=None, override=False):
    """Load `.env` into os.environ. Returns the path used, or None.

    `override=False` (the default) means an already-set environment variable
    wins over the file.
    """
    global _loaded_from

    if path is None:
        path = Path(__file__).resolve().parent / ".env"
    path = Path(path)

    if not path.is_file():
        return None

    # Reported before either parser runs, so the warning does not depend on
    # which branch is taken -- the branch is exactly what was unreliable.
    _report_duplicates(path)

    # Defer to python-dotenv when the user already has it: their version may
    # support syntax this parser does not (multi-line values, interpolation).
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(path, override=override)
        _loaded_from = str(path)
        logger.debug("Loaded %s via python-dotenv", path)
        return str(path)
    except ImportError:
        pass

    # LAST occurrence wins, matching python-dotenv and the shell convention.
    # This loop used to assign straight into os.environ and skip any key
    # already there, which with the default `override=False` made the FIRST
    # line win as soon as the first assignment had happened -- the divergence
    # documented on `duplicate_keys`. Collapsing the file first, and only
    # then consulting the pre-existing environment, keeps the documented
    # precedence (a real environment variable beats the file) while giving
    # the same answer as the other branch.
    preset = set() if override else set(os.environ)
    collapsed = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(raw)
        if parsed:
            collapsed[parsed[0]] = parsed[1]

    count = 0
    for key, value in collapsed.items():
        if key in preset:
            continue
        os.environ[key] = value
        count += 1

    _loaded_from = str(path)
    logger.debug("Loaded %d value(s) from %s", count, path)
    return str(path)


def unknown_keys(path=None):
    """Keys present in .env that nothing in this project reads.

    Usually a typo — `TWO_CAPTCHA_KEY`, `BERSHKA_CDP` — which otherwise fails
    silently as "the key just isn't being picked up".
    """
    if path is None:
        path = Path(__file__).resolve().parent / ".env"
    path = Path(path)
    if not path.is_file():
        return []
    found = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(raw)
        if parsed and parsed[0] not in ENV_KEYS:
            found.append(parsed[0])
    return found


def env_value(name):
    """Read one recognised variable, treating .env.example placeholders as unset."""
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        # Empty is not the same as "you left the example text in". An unset
        # GitHub Actions secret arrives as an empty string, so warning here
        # would print "put your real value in .env" on every canary run that
        # deliberately has no proxy — and a warning that fires when nothing is
        # wrong teaches people to ignore warnings.
        return None
    if stripped.lower() in _PLACEHOLDERS:
        logger.warning(
            "%s is still set to the placeholder from .env.example — treating it "
            "as unset. Put your real value in .env.", name)
        return None
    if _BRACED_PLACEHOLDER_RE.search(stripped):
        logger.warning(
            "%s still has an unfilled {placeholder} from .env.example — "
            "treating it as unset. Put your real value in .env.", name)
        return None
    return stripped


def apply(args, keys=None, quiet=False):
    """Fill unset argparse destinations from the environment.

    Call once, right after `parse_args()`. Only fills a destination that the
    parser actually defines and that is still falsy, so an explicit flag always
    wins.
    """
    load_env()

    for env_name, dest in (keys or ENV_KEYS).items():
        if not hasattr(args, dest):
            continue
        if getattr(args, dest):
            continue
        value = env_value(env_name)
        if value:
            setattr(args, dest, value)
            if not quiet:
                # Never log the value. Credentials end up in CI logs, pasted
                # terminal output and bug reports.
                logger.info("Using %s from the environment for --%s",
                            env_name, dest.replace("_", "-"))

    if not quiet:
        for key in unknown_keys():
            logger.warning("Ignoring unrecognised key in .env: %s", key)

    return args


if __name__ == "__main__":
    # `python3 env_config.py` — report what is configured, without printing
    # any secret. Useful as a first step when a key "isn't being picked up".
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    where = load_env()
    print(f".env file:      {where or 'not found (this is fine — env vars still work)'}")
    for env_name, dest in ENV_KEYS.items():
        value = env_value(env_name)
        if value is None:
            state = "not set"
        elif "KEY" in env_name or "@" in value:
            state = f"set ({len(value)} chars, hidden)"
        else:
            state = f"set ({value})"
        print(f"  {env_name:<24} -> --{dest.replace('_', '-'):<16} {state}")
    extras = unknown_keys()
    if extras:
        print("\nUnrecognised keys in .env (typo?): " + ", ".join(extras))
