#!/usr/bin/env bash
# waf_probe.sh — measure how often transfermarkt.com answers with an AWS WAF
# captcha instead of the page, FROM WHEREVER YOU RUN IT.
#
# Why this ships in the repo rather than living in a notebook: the README and
# .env.example both quote a figure (8 of 10 requests challenged from a bare
# datacentre address, 2026-09-16), and a number describing a living thing has
# to arrive with the command that reproduces it or not be written down at
# all. That figure is a property of an address on a day, not of the site —
# from a proxied exit the same day, the real page came back every time.
#
#   ./tools/waf_probe.sh                 # 10 requests to market-values
#   ./tools/waf_probe.sh 20              # 20 requests
#   ./tools/waf_probe.sh 10 transfers    # a different mode
#   ./tools/waf_probe.sh 10 market-values --direct   # ignore any proxy
#
# Modes: market-values | club-squad | transfers | player
#
# THE EXIT IT MEASURES
# --------------------
# By default this goes through BERSHKA_PROXY when .env sets one,
# because "is this address challenged" is a question about an EXIT, and
# measuring the machine you happen to be sitting at answers it for one exit
# only. With no proxy configured, or with --direct, it measures this machine.
#
# The proxy is resolved by env_config.py, the same loader every engine uses,
# so precedence is the documented one (a real environment variable beats
# .env) and only one place decides it. It reaches curl through
# `https_proxy`/`http_proxy` in the ENVIRONMENT -- never a command-line flag,
# because `ps` reads argv. Only the masked host and port are ever printed.
#
# Reading the output: a page the site actually served answers from
# `server: nginx`, its own origin. A refusal answers from `server: CloudFront`
# with `x-amzn-waf-action: captcha` — CloudFront generated it at the edge and
# the request never reached Transfermarkt at all.

set -u

N="${1:-10}"
MODE="${2:-market-values}"
DIRECT="${3:-}"

case "$MODE" in
  market-values) URL="https://www.transfermarkt.com/spieler-statistik/wertvollstespieler/marktwertetop" ;;
  club-squad)    URL="https://www.transfermarkt.com/x/startseite/verein/281" ;;
  transfers)     URL="https://www.transfermarkt.com/statistik/neuestetransfers" ;;
  player)        URL="https://www.transfermarkt.com/x/profil/spieler/418560" ;;
  *) echo "unknown mode: $MODE (expected market-values|club-squad|transfers|player)"; exit 2 ;;
esac

UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

# --- which exit --------------------------------------------------------------
# The python below prints two shell assignments and nothing else: a MASKED
# label, safe to show, and the export lines carrying the real URL. `eval`
# consumes the second, so the credential never reaches argv, a log or the
# terminal. proxy_pool.mask() is the repo's own masker and is documented
# never to raise -- it is the last thing between a password and a log.
PROXY_LABEL="direct (this machine)"
if [ "$DIRECT" != "--direct" ]; then
  eval "$(cd "$(dirname "$0")/.." && python3 - <<'ENVPY'
import shlex
import env_config
import proxy_pool

env_config.load_env()
url = env_config.env_value("BERSHKA_PROXY")
if url:
    print("PROXY_LABEL=%s" % shlex.quote(proxy_pool.mask(url)))
    for name in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
        print("export %s=%s" % (name, shlex.quote(url)))
else:
    print("PROXY_LABEL=%s" % shlex.quote("direct (no BERSHKA_PROXY in .env)"))
ENVPY
)"
fi
HDR="$(mktemp)"; BODY="$(mktemp)"
trap 'rm -f "$HDR" "$BODY"' EXIT

echo "URL:  $URL"
echo "exit: $PROXY_LABEL"
echo "when: $(date -u '+%Y-%m-%d %H:%M:%SZ')   requests: $N"
echo

challenged=0
unreachable=0
served=0
for i in $(seq 1 "$N"); do
  # -L matters: the player and club URLs answer 301 to their canonical slug,
  # and without it every redirect would be counted as a clean response.
  code=$(curl -sSL -o "$BODY" -D "$HDR" -w "%{http_code}" "$URL" \
           -H "User-Agent: $UA" --max-time 40 2>/dev/null)
  srv=$(grep -i '^server:' "$HDR" | tr -d '\r' | tail -1 | awk '{print $2}')
  waf=$(grep -i '^x-amzn-waf-action:' "$HDR" | tr -d '\r' | tail -1 | awk '{print $2}')
  title=$(grep -o -i '<title>[^<]*</title>' "$BODY" | head -1 \
            | sed 's/<[^>]*>//g' | cut -c1-46)
  [ -n "${waf:-}" ] && challenged=$((challenged + 1))
  # curl reports 000 when it never got an HTTP response at all -- a dead or
  # unauthenticated proxy, a DNS failure, a timeout. Counted SEPARATELY:
  # without this, ten failed connections and ten clean pages both print
  # "challenged: 0 of 10", which is the one thing this probe must never do.
  if [ "$code" = "000" ]; then
    unreachable=$((unreachable + 1))
  else
    served=$((served + 1))
  fi
  printf "%3d  http=%-4s server=%-11s waf=%-8s %s\n" \
    "$i" "$code" "${srv:-?}" "${waf:-none}" "$title"
  sleep 2
done

echo
echo "exit:        $PROXY_LABEL"
echo "requests:    $N"
echo "got an HTTP response: $served"
echo "never connected:      $unreachable"
echo "challenged:  $challenged of $served answered"
echo
if [ "$unreachable" -eq "$N" ]; then
  echo "INCONCLUSIVE: not one request reached the site, so this says nothing"
  echo "about the WAF. Every line above is a transport failure -- check the"
  echo "exit itself before reading anything into a challenge count of zero."
elif [ "$unreachable" -gt 0 ]; then
  echo "PARTIAL: $unreachable of $N never connected. The challenge rate below"
  echo "is over the $served that did answer; the rest measured nothing."
fi
if [ "$challenged" -gt 0 ]; then
  echo "This exit meets the WAF. A proxy or the Scraping Browser API is what"
  echo "gets the page from here; TWOCAPTCHA_KEY is what solves the challenge"
  echo "when one still appears (task type AmazonTaskProxyless)."
elif [ "$served" -gt 0 ]; then
  echo "This exit was not challenged in $served answered request(s). That is a"
  echo "fact about this address today, not about the site: on 2026-09-16 the"
  echo "same URL from one datacentre address returned 200 and 405 interleaved."
  echo "Run it again, and run at least 10 before concluding anything."
fi
