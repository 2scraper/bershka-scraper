#!/usr/bin/env python3
"""browser_profile_client.py — read and configure 2Captcha Browser API
profiles, and have the VENDOR build the CDP connection string.

Why this exists: the connection URL can carry a custom proxy as
`-proxy-{base64url}` in the browser login, and assembling that by hand did
not work here. Measured against the live endpoint on 2026-09-17, three
encodings of the same proxy URL:

    padding stripped   -> 401 "deny_no_user"            (not a decodable length)
    padding kept ('=') -> 401 "Wrong user name format"
    padding as '%3D'   -> 401 "Wrong user name format"

Two distinct errors, and the last two agree, so the complaint is about the
username as a whole rather than the base64 inside it. The vendor's own
documented example encodes a 39-byte URL -- a multiple of 3, so it needs no
padding and cannot demonstrate what to do with it. Rather than keep guessing
at a format, ask the API that builds the string itself:

    POST /browser/connection  ->  {"status":"OK","connectionUri":"ws://..."}

Commands:

    python3 tools/browser_profile_client.py accounts
    python3 tools/browser_profile_client.py proxy-accounts
    python3 tools/browser_profile_client.py profiles --account-id 1581
    python3 tools/browser_profile_client.py use --account-id 1581 --write-env
    python3 tools/browser_profile_client.py connection --account-id 1581 \
        --proxy-line 7 --write-env

`use` is the one to reach for first. `GET /browser/accounts` already carries a
ready `connectionUri` for every account and profile, so `use` takes that
string rather than building one -- and it refuses an account whose
`proxyMode` is `"none"`, because a Scraping Browser with no exit attached
cannot tunnel anywhere and answers ERR_TUNNEL_CONNECTION_FAILED to every
navigation. `connection` is the POST path, for when a custom proxy has to be
attached to the request itself.

Credentials come from `.env` via env_config (CLAUDE.md §3) -- never argv.
Nothing secret is printed: logins, passwords and the assembled connection URI
are shown as scheme://***:***@host:port, and `--write-env` puts the real URI
straight into `.env` without it passing through the terminal.

ONE TRAP WORTH NAMING, because this family has been bitten by it before:
the GET endpoints take the API key as a QUERY PARAMETER. `requests` puts the
full URL -- query string included -- into the text of HTTPError and of every
connection error, so any failure would otherwise print the key. Every call
here goes through `_call()`, which redacts before re-raising. The POST
endpoints carry the key in the body and are safe.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import env_config  # noqa: E402

BASE = "https://api.2captcha.com"
ENV = Path(__file__).resolve().parent.parent / ".env"
POOL = Path(__file__).resolve().parent.parent / "proxylist.txt"
VAR = "BERSHKA_CDP_ENDPOINT"
TIMEOUT = 30

_SECRET_RE = re.compile(r"(?i)(key|token|password)=([^&\s\"']+)")


def redact(text: str, *extra: str) -> str:
    """Mask query-string secrets AND any literal value handed in."""
    out = _SECRET_RE.sub(r"\1=***", str(text))
    for value in extra:
        if value and len(value) > 6:
            out = out.replace(value, "***")
    return out


_SECRET_FIELDS = {"password", "key", "token", "secret", "apikey", "api_key",
                  "browserpassword"}
_LOGIN_FIELDS = {"login", "username", "user", "browserlogin"}
_URI_FIELDS = {"uri", "connectionuri", "connection_uri", "url", "endpoint",
               "wsendpoint"}


def safe(obj):
    """A copy of any JSON shape with credentials removed.

    Written because guessing a response shape is what broke the first two
    commands here: `data` held plain strings where dicts were expected. This
    prints whatever actually arrives, so the parser can be written against
    the real thing instead of against a paraphrase of the docs.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            low = str(k).lower()
            if low in _SECRET_FIELDS:
                out[k] = f"*** ({len(str(v))} chars)"
            elif low in _LOGIN_FIELDS and isinstance(v, str):
                out[k] = (v[:6] + "…") if len(v) > 7 else "***"
            elif low in _URI_FIELDS and isinstance(v, str):
                out[k] = mask_url(v)
            else:
                out[k] = safe(v)
        return out
    if isinstance(obj, list):
        return [safe(v) for v in obj]
    if isinstance(obj, str) and "@" in obj and "://" in obj:
        return mask_url(obj)
    return obj


def mask_url(url: str) -> str:
    """A connection URI safe to print: credentials gone, host and port kept.

    THIS MUST NEVER RAISE, for the reason proxy_pool.mask()'s docstring
    spells out: it is the last thing standing between a password and a log,
    and it is called precisely when something is already wrong with the
    value. `urlparse` itself is lazy -- it does not parse the port until
    `.port` is read, and that read raises ValueError on a malformed
    authority. An earlier version of this function read `.port` bare and
    blew up on exactly the input that most needed masking; on a sibling repo
    the same bug put a live proxy login and password into a public CI log.
    The raw netloc is never a safe fallback, because the password is in it.
    """
    try:
        p = urlparse(url)
        host = p.hostname or "?"
        try:
            port = f":{p.port}" if p.port else ""
        except ValueError:
            port = ":?"
        creds = "***:***@" if (p.username or p.password) else ""
        scheme = p.scheme or "?"
        return f"{scheme}://{creds}{host}{port}"
    except Exception:  # noqa: BLE001 -- a masker that raises is worse than a
        # vague one. Say that it is unusable without repeating it.
        return "(unparseable URI, redacted)"


def api_key() -> str:
    env_config.load_env()
    key = env_config.env_value("TWOCAPTCHA_KEY")
    if not key:
        sys.exit("!! TWOCAPTCHA_KEY is not set in .env (or is still the "
                 "placeholder). Run `python3 env_config.py` to see what the "
                 "loader picks up.")
    return key


def _call(method: str, path: str, key: str, *, params=None, body=None):
    """One request, with the key redacted out of anything that can raise."""
    url = f"{BASE}{path}"
    try:
        r = requests.request(method, url, params=params, json=body, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        # The key rides in the query string on GET, and `requests` puts the
        # whole URL into this message. Redact before it reaches a log.
        sys.exit(f"!! {method} {path} failed: {redact(e, key)}")
    try:
        data = r.json()
    except ValueError:
        sys.exit(f"!! {method} {path}: response was not JSON: "
                 f"{redact(r.text[:200], key)}")
    if isinstance(data, dict) and data.get("status") not in (None, "OK", 1):
        sys.exit(f"!! {method} {path} returned "
                 f"{redact(json.dumps(data), key)}")
    return data


def pick_proxy(line: int | None):
    if not POOL.is_file():
        sys.exit(f"!! no proxy pool at {POOL.name}")
    pool = [l.strip() for l in POOL.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]
    if not pool:
        sys.exit(f"!! {POOL.name} has no usable lines.")
    if line is None:
        chosen, which = pool[0], f"line 1 of {len(pool)}"
    else:
        if not 1 <= line <= len(pool):
            sys.exit(f"!! --proxy-line must be 1..{len(pool)}")
        chosen, which = pool[line - 1], f"line {line} of {len(pool)}"
    p = urlparse(chosen)
    try:
        port = p.port
    except ValueError:
        port = None
    if not (p.scheme and p.hostname and port and p.username and p.password):
        sys.exit(f"!! {POOL.name} {which} is not scheme://login:password@host:port")
    return {
        "type": p.scheme,
        "host": p.hostname,
        "port": port,
        "login": p.username,
        "password": p.password,
    }, which, chosen


def profile_id_from_env() -> str | None:
    """The pid already in .env, so an existing profile can be reused."""
    if not ENV.is_file():
        return None
    for raw in ENV.read_text(encoding="utf-8").splitlines():
        if raw.strip().startswith(VAR + "="):
            login = urlparse(raw.split("=", 1)[1].strip()).username or ""
            m = re.search(r"-pid-([A-Za-z0-9_]+)", login)
            return m.group(1) if m else None
    return None


def write_env(uri: str) -> None:
    if not ENV.is_file():
        sys.exit(f"!! no .env at {ENV}")
    lines = ENV.read_text(encoding="utf-8").splitlines()
    for i, raw in enumerate(lines):
        if raw.strip().startswith(VAR + "="):
            lines[i] = f"{VAR}={uri}"
            break
    else:
        lines.append(f"{VAR}={uri}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dicts_in(obj):
    """Every dict anywhere in a JSON shape, so a table can be built without
    knowing which key the list hides under."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from dicts_in(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from dicts_in(v)


def table(data, columns, empty):
    """Print the dicts that carry `columns[0]`; fall back to the raw shape."""
    rows = [d for d in dicts_in(data) if columns[0] in d]
    if not rows:
        print(f"  ({empty}) — the response did not have the expected shape, "
              f"so here it is in full, with credentials removed:")
        print(json.dumps(safe(data), indent=2, ensure_ascii=False))
        return
    for d in rows:
        print("  " + "  ".join(f"{c}={safe({c: d.get(c)})[c]}" for c in columns
                               if c in d))


def cmd_accounts(args, key):
    data = _call("GET", "/browser/accounts", key, params={"key": key})
    if args.raw:
        print(json.dumps(safe(data), indent=2, ensure_ascii=False))
        return
    table(data, ["id", "login", "name"], "no accounts")
    print("\nUse the id as --account-id.")


def cmd_proxy_accounts(args, key):
    data = _call("GET", "/browser/proxy_accounts", key, params={"key": key})
    if args.raw:
        print(json.dumps(safe(data), indent=2, ensure_ascii=False))
        return
    table(data, ["id", "zone", "type", "host", "port", "sessionTime", "status"],
          "no proxy accounts")
    print("\nA status other than 1 is the first thing to suspect when the "
          "browser's own exit refuses to connect.")


def cmd_profiles(args, key):
    data = _call("GET", "/browser/profiles", key,
                 params={"key": key, "accountId": args.account_id,
                         "page": 1, "limit": 50})
    if args.raw:
        print(json.dumps(safe(data), indent=2, ensure_ascii=False))
        return
    table(data, ["profileId", "proxyMode", "proxyAccountId", "country", "name"],
          "no profiles")


def cmd_use(args, key):
    """Take the connectionUri the accounts listing already carries.

    `GET /browser/accounts` returns a ready connectionUri for every account
    and every profile, so the string never has to be assembled by hand --
    which is what three failed encodings of `-proxy-{base64url}` were about.
    """
    data = _call("GET", "/browser/accounts", key, params={"key": key})

    account = next((d for d in dicts_in(data)
                    if d.get("id") == args.account_id and "connectionUri" in d), None)
    if account is None:
        sys.exit(f"!! no account with id {args.account_id} in the listing. "
                 f"Run `accounts` to see the ids.")

    mode = account.get("proxyMode")
    print(f"account          : {args.account_id}  "
          f"name={account.get('name', '')!r}  proxyMode={mode!r}")
    if mode == "none":
        print("   !! proxyMode is 'none' -- this account has NO exit, and its")
        print("      profile inherits that. A Scraping Browser with no proxy")
        print("      cannot tunnel anywhere: that is ERR_TUNNEL_CONNECTION_FAILED.")
        print("      Pick an account whose proxyMode is 'our_proxy', or set one.")
        if not args.force:
            sys.exit("   refusing to write it; pass --force to do it anyway.")

    profile = account.get("profile") or {}
    uri = profile.get("connectionUri") or account.get("connectionUri")
    if not uri or "://" not in uri:
        sys.exit("!! the listing carried no usable connectionUri for that account.")
    print(f"profile          : {profile.get('profileId', '?')}")
    print(f"connectionUri    : {mask_url(uri)}   ({len(uri)} chars)")

    if args.write_env:
        write_env(uri)
        print(f"\nwritten to .env as {VAR}. Now run:")
        print("  python3 playwright_scraper.py --category 'WOMEN / SALE' \\")
        print("      --out /tmp/tm-cdp --format json")
    else:
        print("\nNot written. Add --write-env to put it in .env without the "
              "URI passing through this terminal.")


def cmd_connection(args, key):
    body = {"key": key, "accountId": args.account_id}
    pid = args.profile_id or profile_id_from_env()
    if pid:
        body["profileId"] = pid
        print(f"profile          : {pid}")
    if not args.no_proxy:
        proxy, which, raw = pick_proxy(args.proxy_line)
        body["customProxy"] = proxy
        print(f"custom proxy     : {mask_url(raw)}  ({which})")
    else:
        print("custom proxy     : none (the profile's own setting is used)")

    data = _call("POST", "/browser/connection", key, body=body)
    uri = data.get("connectionUri") or data.get("uri")
    if not uri:
        sys.exit(f"!! no connectionUri in the response: "
                 f"{redact(json.dumps(data), key)}")
    print(f"connectionUri    : {mask_url(uri)}   ({len(uri)} chars)")

    if args.write_env:
        write_env(uri)
        print(f"\nwritten to .env as {VAR}. Now run:")
        print("  python3 playwright_scraper.py --category 'WOMEN / SALE' \\")
        print("      --out /tmp/tm-cdp --format json")
    else:
        print("\nNot written. Re-run with --write-env to put it in .env "
              "without the URI passing through this terminal.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("accounts", help="list browser logins and their ids")
    a.add_argument("--raw", action="store_true", help="dump the whole response")
    pa = sub.add_parser("proxy-accounts", help="list 2Captcha proxy accounts")
    pa.add_argument("--raw", action="store_true", help="dump the whole response")

    p = sub.add_parser("profiles", help="list profiles for an account")
    p.add_argument("--account-id", type=int, required=True,
                   help="the browser account whose profiles to list "
                        "(see `accounts`)")
    p.add_argument("--raw", action="store_true", help="dump the whole response")

    u = sub.add_parser("use", help="put an account's ready connectionUri into .env")
    u.add_argument("--account-id", type=int, required=True,
                   help="the browser account whose ready connectionUri "
                        "to take (see `accounts`)")
    u.add_argument("--write-env", action="store_true",
                   help=f"write the URI into .env as {VAR}, without it "
                        f"passing through this terminal")
    u.add_argument("--force", action="store_true",
                   help="write it even when the account has proxyMode 'none'")

    c = sub.add_parser("connection", help="have the API build a connection URI")
    c.add_argument("--account-id", type=int, required=True,
                   help="the browser account to build a URI for "
                        "(see `accounts`)")
    c.add_argument("--profile-id", default=None,
                   help="default: the pid already in .env")
    c.add_argument("--proxy-line", type=int, default=None,
                   help="1-based line in proxylist.txt (default: the first)")
    c.add_argument("--no-proxy", action="store_true",
                   help="ask for a URI without a custom proxy, as a control")
    c.add_argument("--write-env", action="store_true",
                   help=f"write the URI into .env as {VAR}")
    args = ap.parse_args()

    key = api_key()
    return {
        "accounts": cmd_accounts,
        "proxy-accounts": cmd_proxy_accounts,
        "profiles": cmd_profiles,
        "use": cmd_use,
        "connection": cmd_connection,
    }[args.cmd](args, key) or 0


if __name__ == "__main__":
    raise SystemExit(main())
