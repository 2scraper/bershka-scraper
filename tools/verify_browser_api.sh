#!/usr/bin/env bash
# verify_browser_api.sh — does the Scraping Browser API actually clear this
# site's AWS WAF challenge, or does it merely never meet one?
#
# Those two outcomes look identical from a single successful run, and telling
# them apart is the whole point of this script. A measurement with no negative
# case is an anecdote: if the exit you are testing from is not challenged in
# the first place, a green run through the Browser API proves nothing about
# auto-solve.
#
# So it runs three legs and compares them:
#
#   1. CONTROL   — a plain HTTP probe from THIS machine, to establish whether
#                  this network position gets challenged at all.
#   2. BASELINE  — a local Chromium run, no Browser API.
#   3. TEST      — the same run through --cdp-endpoint.
#
# Read the verdict at the end; it says which of the four possible worlds you
# are in, including the two where the answer is "inconclusive, and here is
# why".
#
# Needs: .env with TWOCAPTCHA_KEY and BERSHKA_CDP_ENDPOINT, and
# playwright installed. Prints no secrets.
#
#   bash tools/verify_browser_api.sh

set -u
cd "$(dirname "$0")/.." || exit 2
LOGS=$(mktemp -d)
trap 'echo; echo "logs kept in $LOGS"' EXIT

# `timeout` is GNU coreutils and is NOT on a stock macOS, where this script is
# most likely to be run (the machine with a browser on it). Homebrew installs
# it as `gtimeout`. Fall back to running the command unbounded rather than
# failing with "command not found" on the very line that was supposed to stop
# a run hanging.
if command -v timeout >/dev/null 2>&1;      then RUN_TIMEOUT="timeout"
elif command -v gtimeout >/dev/null 2>&1;   then RUN_TIMEOUT="gtimeout"
else RUN_TIMEOUT=""
     echo "note: no timeout(1) on this system — the two browser runs below are"
     echo "      unbounded. Ctrl-C if one hangs. (brew install coreutils adds it.)"
     echo
fi
bounded() {  # bounded <seconds> <command...>
  local secs="$1"; shift
  if [ -n "$RUN_TIMEOUT" ]; then "$RUN_TIMEOUT" "$secs" "$@"; else "$@"; fi
}

echo "=============================================================="
echo " Scraping Browser API — does it clear the challenge, or dodge it?"
echo "=============================================================="
echo

# ---------------------------------------------------------------- config
if [ ! -f .env ]; then
  echo "!! no .env next to the scripts. Copy .env.example to .env and put"
  echo "   TWOCAPTCHA_KEY and BERSHKA_CDP_ENDPOINT in it."
  exit 2
fi
echo "--- what the loader picks up (no secrets printed) ---"
python3 env_config.py || exit 2
have_cdp=$(python3 - <<'PY'
import env_config
class A: pass
a = A()
for d in set(env_config.ENV_KEYS.values()):
    setattr(a, d, None)
setattr(a, "out", "x")
env_config.apply(a)
print("yes" if getattr(a, "cdp_endpoint", None) else "no")
PY
)
echo

# ---------------------------------------------------------------- 1. control
echo "=== 1. CONTROL — is this exit challenged at all? ==="
bash tools/waf_probe.sh 10 market-values 2>&1 | tee "$LOGS/control.txt" | tail -4
challenged=$(grep -c "waf=captcha" "$LOGS/control.txt")
echo

# ---------------------------------------------------------------- 2. baseline
echo "=== 2. BASELINE — local Chromium, no Browser API ==="
bounded 300 python3 playwright_scraper.py --mode market-values --pages 1 \
    --out "$LOGS/baseline" --format json > "$LOGS/baseline.log" 2>&1
base_rc=$?
base_rows=$(python3 -c "
import json,sys
try: print(len(json.load(open('$LOGS/baseline.json'))))
except Exception: print(0)")
echo "   exit=$base_rc rows=$base_rows"
grep -iE "captcha|waf|blocked" "$LOGS/baseline.log" | head -3 | sed 's/^/   /'
echo

# ---------------------------------------------------------------- 3. test
if [ "$have_cdp" != "yes" ]; then
  echo "=== 3. TEST — SKIPPED: no BERSHKA_CDP_ENDPOINT in .env ==="
  echo
  echo "VERDICT: cannot answer the question without the Browser API leg."
  exit 0
fi

echo "=== 3. TEST — through the Scraping Browser API ==="
echo "   (a profile allows ONE live connection; if this 500s, another run"
echo "    still holds that pid. A 401 here is a malformed or superseded"
echo "    login, not an expired one: the vendor documents profiles as stored"
echo "    90 days from creation or until deleted, and the credentials"
echo "    themselves do not expire on a timer. Fetch a fresh connectionUri"
echo "    with tools/browser_profile_client.py rather than editing the"
echo "    string.)"
bounded 300 python3 playwright_scraper.py --mode market-values --pages 1 \
    --out "$LOGS/viacdp" --format json > "$LOGS/viacdp.log" 2>&1
cdp_rc=$?
cdp_rows=$(python3 -c "
import json,sys
try: print(len(json.load(open('$LOGS/viacdp.json'))))
except Exception: print(0)")
echo "   exit=$cdp_rc rows=$cdp_rows"
autosolve_on=$(grep -c "setAutoSolve enabled" "$LOGS/viacdp.log")
solved=$(grep -c "CAPTCHA solved automatically" "$LOGS/viacdp.log")
detected=$(grep -c "CAPTCHA detected on page" "$LOGS/viacdp.log")
grep -iE "Scraping Browser|setAutoSolve" "$LOGS/viacdp.log" | head -4 | sed 's/^/   /'
echo

# ---------------------------------------------------------------- verdict
echo "=============================================================="
echo " VERDICT"
echo "=============================================================="
printf "  control: %s of 10 plain requests challenged\n" "$challenged"
printf "  baseline (local browser):   exit=%s rows=%s\n" "$base_rc" "$base_rows"
printf "  test (Browser API):         exit=%s rows=%s\n" "$cdp_rc" "$cdp_rows"
printf "  auto-solve enabled: %s | challenge detected: %s | solved: %s\n" \
       "$([ "$autosolve_on" -gt 0 ] && echo yes || echo NO)" \
       "$([ "$detected" -gt 0 ] && echo yes || echo no)" \
       "$([ "$solved" -gt 0 ] && echo yes || echo no)"
echo

if [ "$autosolve_on" -eq 0 ]; then
  echo "  INCONCLUSIVE — auto-solve never switched on. Either this endpoint is"
  echo "  not a Scraping Browser, or it does not carry the Captcha CDP domain."
  echo "  Check the log line 'Captcha.setAutoSolve not available'."
elif [ "$challenged" -eq 0 ]; then
  echo "  INCONCLUSIVE, and this is the trap this script exists for: your exit"
  echo "  was NOT challenged even once without the Browser API, so a green run"
  echo "  through it shows only that it fetches the page — not that it cleared"
  echo "  anything. Re-run from an address that IS challenged (a cloud VM or a"
  echo "  CI runner), or the answer stays unknown."
elif [ "$solved" -gt 0 ]; then
  echo "  ANSWERED: the exit is challenged ($challenged/10), the Browser API met"
  echo "  a challenge and reported solving it, and the run returned rows. That"
  echo "  is auto-solve doing the work, with a negative case behind it."
elif [ "$cdp_rc" -eq 0 ] && [ "$base_rc" -ne 0 ]; then
  echo "  ANSWERED (weaker form): the exit is challenged ($challenged/10), the"
  echo "  local browser failed and the Browser API succeeded. It cleared the"
  echo "  challenge, though no explicit solve event was logged — its own exit"
  echo "  may simply not be challenged. Useful either way."
else
  echo "  MIXED — read the logs. Both legs behaved the same way, so the Browser"
  echo "  API made no measurable difference from this address."
fi
