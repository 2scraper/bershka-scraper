# Builds the Playwright engine (the one the README recommends) into a
# container with its own Chromium -- for a CI canary run or a scheduled job,
# not required for local development (`pip install` directly is simpler
# there).
#
#   docker build -t bershka-scraper .
#   docker run --rm -v "$PWD/out:/out" bershka-scraper \
#     --mode market-values --pages 2 --out /out/market_values
#
# Pass --proxy/--twocaptcha-key the same way as running locally, or mount a
# .env at /app/.env -- nothing here bakes in a credential.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    # Playwright's own apt-get for Chromium's shared-library dependencies --
    # not pip packages, so this has to run as a separate, explicit step.
    && playwright install --with-deps chromium

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a useful companion in the same image. smoke_test.py's
# test_dockerfile_copies_what_it_runs checks this list against the
# entrypoint's real import graph -- this family has shipped a broken image
# from a missing COPY line three times before (see that test's docstring),
# because nothing else in a repo like this ever builds the image.
COPY api_scraper.py browser_bridge.py captcha_solver.py catalog_walk.py \
     diff_runs.py env_config.py fingerprint_client.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py proxy_pool.py \
     robots.snapshot.txt ./

ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
