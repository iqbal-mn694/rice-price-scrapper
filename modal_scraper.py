"""
Modal deployment for the daily rice price scraper.

This defines two scheduled functions:
  - scrape_primary : runs at 13:00 WIB, does NOT forward-fill missing data.
  - scrape_retry   : runs at 16:00 WIB, forward-fills if data is still missing.

Deploy with:
    modal deploy modal_scraper.py

Test a single function immediately (without waiting for its schedule):
    modal run modal_scraper.py::scrape_primary
    modal run modal_scraper.py::scrape_retry
"""

import modal

# ==========================================
# Container image: Python + Chrome + project dependencies
# ==========================================
scraper_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "wget",
        "gnupg",
        "unzip",
        "libnss3",
        "libgconf-2-4",
        "libxss1",
        "libasound2",
        "libatk-bridge2.0-0",
        "libgtk-3-0",
    )
    .run_commands(
        "wget -q -O /tmp/chrome.deb "
        "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb",
        "apt-get update && apt-get install -y /tmp/chrome.deb",
    )
    .pip_install("selenium", "webdriver-manager", "pandas", "supabase")
    .add_local_python_source("scrape_rice_price", "config")
)

app = modal.App("rice-price-scraper")

# Supabase credentials, created once via:
#   modal secret create supabase-credentials \
#       SUPABASE_URL=https://xxxxx.supabase.co \
#       SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
supabase_secret = modal.Secret.from_name("supabase-credentials")


# ==========================================
# Scheduled functions
# ==========================================
@app.function(
    image=scraper_image,
    schedule=modal.Cron("0 6 * * *"),  # 13:00 WIB (cron runs in UTC)
    secrets=[supabase_secret],
    timeout=600,
)
def scrape_primary() -> None:
    """Primary daily scrape attempt. Does not forward-fill missing data."""
    from scrape_rice_price import main

    main(allow_fallback=False)


@app.function(
    image=scraper_image,
    schedule=modal.Cron("0 9 * * *"),  # 16:00 WIB (cron runs in UTC)
    secrets=[supabase_secret],
    timeout=600,
)
def scrape_retry() -> None:
    """Retry attempt for the day. Forward-fills if data is still unavailable."""
    from scrape_rice_price import main

    main(allow_fallback=True)
