#!/usr/bin/env python3
"""
fingerprint_client.py
---------------------
Client for 2Captcha's Fingerprint API (https://2captcha.com/fingerprints/api),
plus the builders that turn one response into settings for a Chromium this
project LAUNCHED ITSELF.

Never applied over `--cdp-endpoint`: the Scraping Browser API brings its own
fingerprint, and a second one stacked on a remote browser is a contradiction,
not better cover. The engine refuses a typed `--cdp-endpoint` together with
`--fingerprint`, and ignores (with a notice) an endpoint that only came from
`.env`.

API surface used (docs re-read 2026-11-05)
------------------------------------------
  GET https://api.2captcha.com/fingerprint/random
  GET https://api.2captcha.com/fingerprint/generate
    key       the API key, as a query parameter
    format    "chromium" (default) | "raw"
    tags      OS/browser filter, e.g. "Windows"
    country   ISO 3166-1 alpha-2
    min_browser_version / browser_version / build_version (generate only)
  Errors: {"errorId", "errorCode", "errorDescription"}, e.g. 403
  ERROR_FINGERPRINT_PLAN_REQUIRED — fingerprints are a separate subscription,
  and a key with a working captcha balance is refused without one.

`tags` is sent as ONE value. The docs show repeated `tags=` parameters as
well; that form has not been verified from this repository, and the list
forms tried earlier (comma, pipe) were answered with HTTP 400. Some single
tags contain a space ("Microsoft Windows", "Apple Mac").

Where a `chromium` response keeps each value (field names read from real
responses of 2026-09-21 and 2026-09-30, kept trimmed in
fixtures/fingerprint_chromium.json; the docs' examples are abridged and do
not agree with each other on `userAgent`):

  user agent        userAgent.userAgent            (docs example: userAgent.value)
  client hints      userAgent.{brandVersionList, brandFullVersionList,
                    fullVersion, platform, platformVersion, architecture,
                    bitness, model, mobile, wow64, formFactors}
  locale/languages  intl.contentLocale, intl.languages
  timezone          intl.timeZone
  screen            screen.{width, height, outerWidth, outerHeight,
                    deviceScaleFactor}
  navigator         navigator.{platform, hardwareConcurrency, deviceMemory,
                    maxTouchPoints}
  WebGL strings     webgl.glParameters["37445"/"37446"]  (UNMASKED_VENDOR /
                    UNMASKED_RENDERER; docs example: webgl.vendor/renderer)

Every field this module cannot find is reported by name (`describe()`,
`--explain`), never silently skipped: a fingerprint that was billed and then
applied to nothing looks exactly like one that worked.

What is applied, and how deep
-----------------------------
* Context options (native in Playwright): user agent, locale, timezone,
  viewport, screen, device scale factor.
* `Emulation.setUserAgentOverride` over a CDP session: the same user agent
  WITH its client hints, so `Sec-CH-UA*` headers and
  `navigator.userAgentData` agree with the `User-Agent` header. A bare UA
  override leaves the real Chromium's hints in place, which is a
  contradiction any server can read.
* An init script for what neither can set: navigator.platform /
  hardwareConcurrency / deviceMemory / maxTouchPoints and the two WebGL
  debug strings. Shallow by construction: it changes what is REPORTED, not
  what the GPU renders, and the engine underneath is still the installed
  Chromium (a fingerprint may name another Chrome version).

Usage
-----
    python3 fingerprint_client.py --tags Windows --country ru --explain

    # in an engine (see playwright_scraper.py):
    fp = get_fingerprint(key, tags="Windows", country="ru")
    context = browser.new_context(**playwright_context_kwargs(fp))
    context.add_init_script(playwright_init_script(fp))
    page = context.new_page()
    override = user_agent_override(fp)
    if override:
        context.new_cdp_session(page).send("Emulation.setUserAgentOverride", override)
    ... page.goto(url) ...
    mismatches = readback_mismatches(fp, page.evaluate(READBACK_JS))

Requires: pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional

import requests

# This repo's exit-5 type: FingerprintError subclasses it so every engine's
# entry point maps a refusal to EXIT_REMOTE_API_ERROR, not a crash (1).
from output_writer import RemoteAPIError

logger = logging.getLogger("fingerprint_client")

API_BASE = "https://api.2captcha.com"
RANDOM_URL = f"{API_BASE}/fingerprint/random"
GENERATE_URL = f"{API_BASE}/fingerprint/generate"

# Resolved at CALL time, not import time, so a test that points HOME at a
# temporary directory really gets an empty cache (a cached fingerprint would
# otherwise hide the refusal path).
def default_cache_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".cache", "2captcha-fingerprints")


DEFAULT_TAGS = "Windows"

# The key rides in the query string, and `requests` copies the full URL into
# the text of every error it raises. Everything raised from here goes through
# this first; the endpoint and status survive, the key does not.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact(text) -> str:
    return _KEY_IN_TEXT_RE.sub(r"\1=***", str(text))


class FingerprintError(RemoteAPIError):
    """The Fingerprint API refused or failed. A remote-API error (exit 5 in
    the engines), not a crash: the message says why, already redacted."""


def _cache_path(cache_dir: str, params: dict, generate: bool) -> str:
    key = json.dumps({"generate": generate, **params}, sort_keys=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return os.path.join(cache_dir, f"{digest}.json")


def _api_error(resp) -> str:
    try:
        body = resp.json()
    except ValueError:
        return _redact(resp.text[:200])
    if isinstance(body, dict):
        code = body.get("errorCode") or ""
        desc = body.get("errorDescription") or ""
        return _redact(("%s %s" % (code, desc)).strip() or str(body)[:200])
    return _redact(str(body)[:200])


def get_fingerprint(api_key: str, *, tags: Optional[str] = DEFAULT_TAGS,
                    country: Optional[str] = None,
                    min_browser_version: Optional[int] = None,
                    browser_version: Optional[int] = None,
                    build_version: Optional[str] = None,
                    fmt: str = "chromium", generate: bool = False,
                    cache_dir: Optional[str] = "default",
                    refresh: bool = False, timeout: int = 30) -> dict:
    """Fetch one fingerprint. Cached on disk unless cache_dir is None.

    Cached because both endpoints are billed per successful response and
    capped per minute: an uncached call per run turns into a bill, and per
    page into rate-limit errors. The cache key is the request, so the same
    flags give the same identity until --refresh.
    """
    if not api_key:
        raise FingerprintError("no API key: the Fingerprint API uses TWOCAPTCHA_KEY")
    if tags and any(sep in tags for sep in (",", "|")):
        raise FingerprintError(
            "tags=%r is a list; send ONE value such as Windows (lists were "
            "answered with HTTP 400)" % tags)
    params: Dict[str, Any] = {"format": fmt}
    if tags:
        params["tags"] = tags
    if country:
        params["country"] = country.lower()
    if min_browser_version:
        params["min_browser_version"] = min_browser_version
    if browser_version:
        params["browser_version"] = browser_version
    if build_version:
        if not generate:
            raise FingerprintError("build_version is only accepted by /fingerprint/generate")
        params["build_version"] = build_version

    if cache_dir == "default":
        cache_dir = default_cache_dir()
    if cache_dir:
        path = _cache_path(cache_dir, params, generate)
        if os.path.exists(path) and not refresh:
            with open(path, encoding="utf-8") as f:
                fp = json.load(f)
            logger.info("Using cached fingerprint %s (%s)", fp.get("id"), path)
            return fp

    url = GENERATE_URL if generate else RANDOM_URL
    logger.info("GET %s %s", url, params)
    try:
        resp = requests.get(url, params={**params, "key": api_key}, timeout=timeout)
    except requests.RequestException as exc:
        raise FingerprintError("Fingerprint API request failed: %s" % _redact(exc)) from None

    if resp.status_code in (401, 403):
        # Measured 2026-11-05 with a key that has a captcha balance:
        # 403 ERROR_FINGERPRINT_PLAN_REQUIRED.
        raise FingerprintError(
            "Fingerprint API refused the key (%s: %s). Fingerprints are a "
            "separate subscription from captcha solving." % (resp.status_code, _api_error(resp)))
    if resp.status_code == 400:
        raise FingerprintError("Fingerprint API rejected the request (400: %s); sent %s"
                               % (_api_error(resp), params))
    if resp.status_code == 429:
        raise FingerprintError("Fingerprint API rate limit (429: %s). The per-minute cap "
                               "depends on the plan; the disk cache avoids refetching."
                               % _api_error(resp))
    if resp.status_code != 200:
        raise FingerprintError("Fingerprint API answered %s: %s"
                               % (resp.status_code, _api_error(resp)))
    try:
        fp = resp.json()
    except ValueError:
        raise FingerprintError("Fingerprint API answered 200 with a body that is not JSON") from None
    if not isinstance(fp, dict) or not fingerprint_user_agent(fp):
        raise FingerprintError("Fingerprint API answered 200 without a user agent; "
                               "nothing could be applied (keys: %s)"
                               % (sorted(fp)[:12] if isinstance(fp, dict) else type(fp).__name__))

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        with open(_cache_path(cache_dir, params, generate), "w", encoding="utf-8") as f:
            json.dump(fp, f, indent=2)
    logger.info("Fingerprint %s (%s)", fp.get("id"), fp.get("country"))
    return fp


# --- reading a response -----------------------------------------------------

def _walk(fp, path):
    node = fp
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and isinstance(key, int) and -len(node) <= key < len(node):
            node = node[key]
        else:
            return None
    return node


def _pick(fp: dict, *paths):
    """First non-empty scalar at any of `paths`, and the path it came from."""
    for path in paths:
        node = _walk(fp, path)
        if isinstance(node, bool) or (isinstance(node, (str, int, float)) and str(node).strip()):
            return node, ".".join(str(p) for p in path)
    return None, None


FIELDS = {
    "user_agent": [("userAgent", "userAgent"), ("userAgent", "value"), ("userAgent",),
                   ("navigator", "userAgent"), ("data", "ua")],
    "locale": [("intl", "contentLocale"), ("intl", "languages", 0)],
    "timezone": [("intl", "timeZone")],
    "screen_width": [("screen", "width"), ("screen", "screenRect", "width")],
    "screen_height": [("screen", "height"), ("screen", "screenRect", "height")],
    "outer_width": [("screen", "outerWidth")],
    "outer_height": [("screen", "outerHeight")],
    "device_scale_factor": [("screen", "deviceScaleFactor")],
    "platform": [("navigator", "platform")],
    "hardware_concurrency": [("navigator", "hardwareConcurrency")],
    "device_memory": [("navigator", "deviceMemory")],
    "max_touch_points": [("navigator", "maxTouchPoints")],
    "webgl_vendor": [("webgl", "glParameters", "37445"), ("webgl", "vendor")],
    "webgl_renderer": [("webgl", "glParameters", "37446"), ("webgl", "renderer")],
    "ch_full_version": [("userAgent", "fullVersion")],
    "ch_platform": [("userAgent", "platform")],
}


def describe(fp: dict) -> Dict[str, Dict[str, Any]]:
    """Every field this module applies: its value and the response path it
    came from (None, None when the response has no such field)."""
    out = {}
    for name, paths in FIELDS.items():
        value, source = _pick(fp, *paths)
        out[name] = {"value": value, "from": source}
    out["languages"] = {"value": languages(fp), "from": "intl.languages" if languages(fp) else None}
    return out


def fingerprint_user_agent(fp: dict) -> Optional[str]:
    value, _ = _pick(fp, *FIELDS["user_agent"])
    return str(value) if value else None


def languages(fp: dict) -> List[str]:
    langs = _walk(fp, ("intl", "languages"))
    return [str(x) for x in langs if x] if isinstance(langs, list) else []


def missing_fields(fp: dict) -> List[str]:
    return [name for name, v in describe(fp).items() if v["value"] in (None, [], "")]


# --- builders ---------------------------------------------------------------

def playwright_context_kwargs(fp: dict) -> dict:
    """Options for Browser.new_context taken from the fingerprint itself.

    Nothing is derived: no locale invented from a country (a German
    fingerprint is not `en-DE`), no viewport equal to the screen when the
    response states its own outer window size.
    """
    d = describe(fp)
    kwargs: Dict[str, Any] = {}
    if d["user_agent"]["value"]:
        kwargs["user_agent"] = str(d["user_agent"]["value"])
    if d["locale"]["value"]:
        kwargs["locale"] = str(d["locale"]["value"])
    if d["timezone"]["value"]:
        kwargs["timezone_id"] = str(d["timezone"]["value"])
    w, h = d["screen_width"]["value"], d["screen_height"]["value"]
    if w and h:
        w, h = int(w), int(h)
        kwargs["screen"] = {"width": w, "height": h}
        ow = int(d["outer_width"]["value"] or w)
        oh = int(d["outer_height"]["value"] or max(400, h - 120))
        kwargs["viewport"] = {"width": min(ow, w), "height": max(400, min(oh, h))}
    dsf = d["device_scale_factor"]["value"]
    if dsf:
        try:
            kwargs["device_scale_factor"] = float(dsf)
        except (TypeError, ValueError):
            pass
    return kwargs


def _brands(items) -> List[Dict[str, str]]:
    out = []
    for b in items or []:
        if isinstance(b, dict) and b.get("brand") is not None and b.get("version") is not None:
            out.append({"brand": str(b["brand"]), "version": str(b["version"])})
    return out


def user_agent_override(fp: dict) -> Optional[dict]:
    """Parameters for CDP `Emulation.setUserAgentOverride`: the user agent
    together with its client hints, so the `Sec-CH-UA*` request headers and
    `navigator.userAgentData` describe the same browser as `User-Agent`.

    None when the response carries no hint metadata: sending the UA alone
    would add nothing over the context option.
    """
    ua = fingerprint_user_agent(fp)
    meta_src = fp.get("userAgent") if isinstance(fp.get("userAgent"), dict) else None
    if not ua or not meta_src or not meta_src.get("brandVersionList"):
        return None
    meta: Dict[str, Any] = {
        "brands": _brands(meta_src.get("brandVersionList")),
        "fullVersionList": _brands(meta_src.get("brandFullVersionList")),
        "fullVersion": str(meta_src.get("fullVersion") or ""),
        "platform": str(meta_src.get("platform") or ""),
        "platformVersion": str(meta_src.get("platformVersion") or ""),
        "architecture": str(meta_src.get("architecture") or ""),
        "model": str(meta_src.get("model") or ""),
        "mobile": bool(meta_src.get("mobile")),
        "bitness": str(meta_src.get("bitness") or ""),
        "wow64": bool(meta_src.get("wow64")),
    }
    if isinstance(meta_src.get("formFactors"), list):
        meta["formFactors"] = [str(x) for x in meta_src["formFactors"]]
    params: Dict[str, Any] = {"userAgent": ua, "userAgentMetadata": meta}
    langs = languages(fp)
    if langs:
        params["acceptLanguage"] = ",".join(langs)
    platform, _ = _pick(fp, *FIELDS["platform"])
    if platform:
        params["platform"] = str(platform)
    return params


def playwright_init_script(fp: dict) -> str:
    """JS run before page scripts for what no context option or CDP override
    sets. Values are embedded as JSON, so a string from the API cannot end
    the script it sits in."""
    d = describe(fp)
    payload = json.dumps({
        "platform": d["platform"]["value"],
        "hardwareConcurrency": d["hardware_concurrency"]["value"],
        "deviceMemory": d["device_memory"]["value"],
        "maxTouchPoints": d["max_touch_points"]["value"],
        "webglVendor": d["webgl_vendor"]["value"],
        "webglRenderer": d["webgl_renderer"]["value"],
    })
    return """
(() => {
  const fp = %s;
  const def = (obj, prop, value) => {
    if (value === null || value === undefined) return;
    try { Object.defineProperty(obj, prop, {get: () => value, configurable: true}); }
    catch (e) { /* non-configurable: leave it rather than throw */ }
  };
  def(Navigator.prototype, 'platform', fp.platform);
  def(Navigator.prototype, 'hardwareConcurrency', fp.hardwareConcurrency);
  def(Navigator.prototype, 'deviceMemory', fp.deviceMemory);
  def(Navigator.prototype, 'maxTouchPoints', fp.maxTouchPoints);
  // 37445 = UNMASKED_VENDOR_WEBGL, 37446 = UNMASKED_RENDERER_WEBGL; WebGL1 and 2.
  for (const ctor of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
    if (!ctor) continue;
    const original = ctor.prototype.getParameter;
    ctor.prototype.getParameter = function (p) {
      if (p === 37445 && fp.webglVendor) return fp.webglVendor;
      if (p === 37446 && fp.webglRenderer) return fp.webglRenderer;
      return original.apply(this, arguments);
    };
  }
})();
""" % payload


# --- reading it back from a live page ----------------------------------------

# A function expression, evaluated as one (Playwright's page.evaluate).
READBACK_JS = """() => ({
  userAgent: navigator.userAgent,
  platform: navigator.platform,
  language: navigator.language,
  languages: Array.from(navigator.languages || []),
  timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  hardwareConcurrency: navigator.hardwareConcurrency,
  deviceMemory: navigator.deviceMemory,
  screen: [screen.width, screen.height],
  uaDataBrands: navigator.userAgentData
      ? navigator.userAgentData.brands.map(b => b.brand + '/' + b.version) : null,
  uaDataPlatform: navigator.userAgentData ? navigator.userAgentData.platform : null,
})"""


def readback_mismatches(fp: dict, seen: dict) -> List[str]:
    """What the live page reports that differs from the fingerprint.
    Empty means every checked field was applied."""
    d = describe(fp)
    want = {
        "userAgent": d["user_agent"]["value"],
        "platform": d["platform"]["value"],
        "language": d["locale"]["value"],
        "timeZone": d["timezone"]["value"],
        "hardwareConcurrency": d["hardware_concurrency"]["value"],
        "deviceMemory": d["device_memory"]["value"],
        "uaDataPlatform": d["ch_platform"]["value"],
    }
    if d["screen_width"]["value"] and d["screen_height"]["value"]:
        want["screen"] = [int(d["screen_width"]["value"]), int(d["screen_height"]["value"])]
    brands = _brands(_walk(fp, ("userAgent", "brandVersionList")))
    if brands:
        want["uaDataBrands"] = ["%s/%s" % (b["brand"], b["version"]) for b in brands]
    out = []
    for key, value in want.items():
        if value is None:
            continue
        got = seen.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            ok = got is not None and float(got) == float(value)
        else:
            ok = got == value
        if not ok:
            out.append("%s: page %r, fingerprint %r" % (key, got, value))
    return out


# --- engine glue: flags -----------------------------------------------------

def add_arguments(parser) -> None:
    """The engine flags. Defined here once so a port copies one function."""
    g = parser.add_argument_group("2Captcha fingerprint (local browser only)")
    g.add_argument("--fingerprint", action="store_true",
                   help="Fetch a fingerprint from the 2Captcha Fingerprint API "
                        "(TWOCAPTCHA_KEY; a separate subscription) and apply it "
                        "to the locally launched Chromium. Not combinable with "
                        "a typed --cdp-endpoint; an endpoint from .env is "
                        "ignored for the run.")
    g.add_argument("--fp-tags", default=DEFAULT_TAGS, metavar="TAG",
                   help="ONE OS-family tag (default: %(default)s).")
    g.add_argument("--fp-country", default=None, metavar="CC",
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "the proxy's exit country.")
    g.add_argument("--fp-refresh", action="store_true",
                   help="Fetch a new fingerprint instead of the cached one "
                        "for the same flags (each fetch is billed).")


# --- CLI ----------------------------------------------------------------------

def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="Fetch a browser fingerprint from 2Captcha")
    p.add_argument("--key", default=None,
                   help="Prefer TWOCAPTCHA_KEY in .env or the environment — argv is readable by `ps`.")
    p.add_argument("--tags", default=DEFAULT_TAGS, metavar="TAG", help="ONE OS-family tag")
    p.add_argument("--country", default=None, help="ISO 3166-1 alpha-2, e.g. ru")
    p.add_argument("--min-browser-version", type=int, default=None)
    p.add_argument("--browser-version", type=int, default=None)
    p.add_argument("--build-version", default=None, help="/generate only, e.g. 145.0.7632.162")
    p.add_argument("--format", dest="fmt", choices=["chromium", "raw"], default="chromium")
    p.add_argument("--generate", action="store_true",
                   help="Use /fingerprint/generate instead of /fingerprint/random")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--refresh", action="store_true", help="Bypass a cached copy")
    p.add_argument("--explain", action="store_true",
                   help="Print, per applied setting, the value and the response "
                        "field it came from, instead of the whole response")
    args = p.parse_args(argv)
    import env_config  # CLI only: the library functions above need nothing local
    env_config.apply(args, keys={"TWOCAPTCHA_KEY": "key"})
    if not args.key:
        logger.error("No API key: put TWOCAPTCHA_KEY in .env or export it.")
        return 2
    try:
        fp = get_fingerprint(
            args.key, tags=args.tags, country=args.country,
            min_browser_version=args.min_browser_version,
            browser_version=args.browser_version, build_version=args.build_version,
            fmt=args.fmt, generate=args.generate,
            cache_dir=None if args.no_cache else "default", refresh=args.refresh)
    except FingerprintError as exc:
        logger.error("%s", exc)
        return 5
    if args.explain:
        for name, v in describe(fp).items():
            print("%-22s %-40s %s" % (name, str(v["from"] or "-- NOT IN RESPONSE --"),
                                      str(v["value"])[:80]))
        print("%-22s %s" % ("client hints", "yes" if user_agent_override(fp) else "NO"))
    else:
        print(json.dumps(fp, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
