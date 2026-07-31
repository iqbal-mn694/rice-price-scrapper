"""
Automated daily rice price scraper for PIHPS (Bank Indonesia) -> Supabase.

Scheduling contract (enforced by modal_scraper.py, not this script):
  - Run #1 at 13:00 WIB : primary attempt, allow_fallback=False.
  - Run #2 at 16:00 WIB : retry attempt, allow_fallback=True.

Behaviour:
  1. If today's price already exists in Supabase, do nothing.
  2. Otherwise, scrape the PIHPS report for today's date, with the date
     range explicitly set (not relying on the site's default view, which
     does not reliably include the current date).
  3. If today's price is found on the site, upsert it.
  4. If today's price is NOT found (e.g. BI has not published yet):
       - Primary run (13:00)  -> exit quietly, let the 16:00 retry handle it.
       - Retry run (16:00)    -> if allow_fallback is set, forward-fill using the
                                  most recent known price per rice type, so the table
                                  is never left empty for a given date (weekends,
                                  national holidays, or any other reporting gap).

Column matching note:
  Grid column headers are matched by PARSING a date out of the header text
  (via regex) and comparing it to the target date object, rather than by
  exact string equality. This avoids failures caused by invisible formatting
  differences (extra/non-breaking spaces, different separators, etc.) between
  what the site renders and what the code expects.
"""

import argparse
import re
import shutil
import sys
import time
from datetime import date, datetime

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from supabase import Client, create_client
from webdriver_manager.chrome import ChromeDriverManager

from config import (
    COMMODITY,
    PROVINCE,
    REGENCY_CITY,
    RICE_TYPE_MAP,
    SOURCE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_URL,
    TABLE_RICE_PRICES,
)


# ==========================================
# Logging helper
# ==========================================
def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}")


# ==========================================
# Selenium driver setup
# ==========================================
def build_driver() -> webdriver.Chrome:
    """Build a headless Chrome driver suitable for CI environments."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # Auto-detect Chrome binary location across environments.
    chrome_path = (
        shutil.which("google-chrome-stable")
        or shutil.which("google-chrome")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )
    if chrome_path:
        options.binary_location = chrome_path

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


# ==========================================
# PIHPS scraping helpers
# ==========================================
def select_treelist_item(driver, container_id: str, target_text: str, timeout: int = 10) -> bool:
    """Select an item inside a DevExtreme TreeList filter (e.g. commodity, province, city)."""
    try:
        xpath = (
            f"//div[@id='{container_id}']"
            f"//div[contains(@class, 'dx-treelist-text-content') "
            f"and normalize-space(text())='{target_text}']"
        )
        text_element = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )
        row_element = text_element.find_element(By.XPATH, "./ancestor::tr")
        checkbox = row_element.find_element(By.CSS_SELECTOR, ".dx-select-checkbox")

        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", checkbox)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", checkbox)
        log(f"Selected '{target_text}' in #{container_id}")
        time.sleep(2.5)
        return True
    except Exception as error:
        log(f"Failed to select '{target_text}' in #{container_id}: {error}")
        return False


def set_devextreme_date(driver, container_id: str, target_date: datetime) -> bool:
    """
    Set a DevExtreme DateBox widget's value via its JS API.
    The start/end date fields (dboDateMulai / dboDateSelesai) are dx-datebox
    widgets (a <div>), not plain <input> elements, so send_keys() does not work.
    """
    year, month_zero_indexed, day = target_date.year, target_date.month - 1, target_date.day
    js_script = f"""
    try {{
        var picker = $('#{container_id}').dxDateBox('instance');
        picker.option('value', new Date({year}, {month_zero_indexed}, {day}));
        return true;
    }} catch (err) {{
        return false;
    }}
    """
    success = driver.execute_script(js_script)
    time.sleep(1)
    return success


def extract_price_grid(driver) -> pd.DataFrame | None:
    """
    Read the DevExpress data grid directly from its JS instance.
    This avoids DOM/HTML column-alignment issues seen with table scraping.

    Date columns are identified by EXCLUDING the known identifier columns
    ("No" and "Komoditas (Rp)"), rather than by requiring a specific
    character (like '/') in the caption. This is more robust to formatting
    differences the site might use for date headers.
    """
    js_script = """
    try {
        var grid = $("#grid1").dxDataGrid("instance");
        var visibleColumns = grid.getVisibleColumns();
        var visibleRows = grid.getVisibleRows();

        var headers = ["No", "Komoditas (Rp)"];
        var dateColumns = [];

        visibleColumns.forEach(function (col) {
            var isIdentifierColumn = (col.caption === 'No' || col.caption === 'Komoditas (Rp)');
            if (col.dataField && !isIdentifierColumn) {
                headers.push(col.caption || col.dataField);
                dateColumns.push(col.dataField);
            }
        });

        var rows = [];
        visibleRows.forEach(function (row) {
            if (row.rowType === "data") {
                var rowValues = [row.data.no || "", row.data.name || ""];
                dateColumns.forEach(function (field) {
                    var value = row.data[field];
                    rowValues.push((value !== undefined && value !== null && value !== "") ? value : "-");
                });
                rows.push(rowValues);
            }
        });

        return { headers: headers, rows: rows };
    } catch (err) {
        return null;
    }
    """
    result = driver.execute_script(js_script)
    if result and result.get("rows"):
        return pd.DataFrame(result["rows"], columns=result["headers"])
    return None


def scrape_price_grid(driver, target_date: datetime) -> pd.DataFrame | None:
    """Run the full filter-and-extract flow on the PIHPS report page for a specific date."""
    log(f"Opening {SOURCE_URL} ...")
    driver.get(SOURCE_URL)
    time.sleep(5)  # initial JS render

    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    if iframes:
        driver.switch_to.frame(iframes[0])

    log("Filling in report filters ...")
    select_treelist_item(driver, "CommodityTree", COMMODITY)
    select_treelist_item(driver, "cboProvince", PROVINCE)
    select_treelist_item(driver, "cboRegency", REGENCY_CITY)

    log(f"Setting date range to {target_date:%d/%m/%Y} ...")
    set_devextreme_date(driver, "dboDateMulai", target_date)
    set_devextreme_date(driver, "dboDateSelesai", target_date)

    log("Clicking 'Lihat Laporan' (#btnReport) ...")
    report_button = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.ID, "btnReport"))
    )
    driver.execute_script("arguments[0].click();", report_button)

    log("Waiting for the grid to finish loading ...")
    WebDriverWait(driver, 20).until(
        EC.invisibility_of_element_located((By.ID, "loadingDiv"))
    )
    time.sleep(3)

    return extract_price_grid(driver)


# ==========================================
# Data transformation helpers
# ==========================================
def clean_price(raw_value) -> float | None:
    """Convert a raw grid cell (e.g. '14,600') into a float, or None if unavailable."""
    text = str(raw_value).strip()
    if text in ("-", "", "None", "nan"):
        return None
    digits_only = re.sub(r"[^\d]", "", text)
    return float(digits_only) if digits_only else None


def parse_date_from_header(header_text: str) -> date | None:
    """
    Extract a date (day, month, year) from a column header, regardless of the
    exact separator or spacing used (e.g. '31/07/2026', '31 / 07 / 2026').
    Returns None if no date-like pattern is found.
    """
    match = re.search(r"(\d{1,2})\D+(\d{1,2})\D+(\d{4})", str(header_text))
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_todays_price_column(grid: pd.DataFrame, target_date: date) -> str | None:
    """Return the grid column whose header date matches target_date, if present."""
    for column_name in grid.columns:
        column_date = parse_date_from_header(column_name)
        if column_date == target_date:
            return column_name
    return None


def build_records_from_grid(
    grid: pd.DataFrame, price_column: str, today_iso: str
) -> list[dict]:
    """Turn today's price column into long-format records ready for Supabase upsert."""
    records = []
    for _, row in grid.iterrows():
        rice_type_name = str(row["Komoditas (Rp)"]).strip()
        rice_type_id = RICE_TYPE_MAP.get(rice_type_name)
        if rice_type_id is None:
            continue  # skips the aggregate "Beras" parent row and any unmapped entries

        price = clean_price(row[price_column])
        if price is None:
            continue

        records.append(
            {
                "date": today_iso,
                "rice_type_id": rice_type_id,
                "price": price,
            }
        )
    return records


# ==========================================
# Supabase helpers
# ==========================================
def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY environment variable."
        )
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def has_data_for_today(supabase: Client, today_iso: str) -> bool:
    """Check whether today's price has already been stored, regardless of source."""
    response = (
        supabase.table(TABLE_RICE_PRICES)
        .select("id")
        .eq("date", today_iso)
        .limit(1)
        .execute()
    )
    return len(response.data) > 0


def get_last_known_prices(supabase: Client, rice_type_ids: list[int]) -> dict[int, float]:
    """Fetch the most recent known price for each rice type (used for forward-fill fallback)."""
    last_known_prices: dict[int, float] = {}
    for rice_type_id in rice_type_ids:
        response = (
            supabase.table(TABLE_RICE_PRICES)
            .select("price")
            .eq("rice_type_id", rice_type_id)
            .order("date", desc=True)
            .limit(1)
            .execute()
        )
        if response.data:
            last_known_prices[rice_type_id] = response.data[0]["price"]
    return last_known_prices


def build_fallback_records(today_iso: str, last_known_prices: dict[int, float]) -> list[dict]:
    """Build forward-filled records for rice types with no data published today."""
    return [
        {
            "date": today_iso,
            "rice_type_id": rice_type_id,
            "price": price,
        }
        for rice_type_id, price in last_known_prices.items()
    ]


def upsert_rice_prices(supabase: Client, records: list[dict]) -> None:
    if not records:
        log("No records to upsert.")
        return
    supabase.table(TABLE_RICE_PRICES).upsert(
        records, on_conflict="date,rice_type_id"
    ).execute()
    log(f"Upserted {len(records)} record(s) into '{TABLE_RICE_PRICES}'.")


# ==========================================
# Main orchestration
# ==========================================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape today's rice price from PIHPS into Supabase.")
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="If today's price is not yet published, forward-fill using the last known price.",
    )
    return parser.parse_args()


def main(allow_fallback: bool = False) -> None:
    """
    Run the daily scrape-and-store routine.

    Args:
        allow_fallback: if True and today's price is not yet published,
            forward-fill using the most recent known price per rice type.
    """
    now = datetime.now()
    today_date = now.date()
    today_iso = now.strftime("%Y-%m-%d")  # format used by the database

    supabase = get_supabase_client()

    if has_data_for_today(supabase, today_iso):
        log(f"Data for {today_iso} already stored. Nothing to do.")
        return

    grid = None
    driver = None
    try:
        driver = build_driver()
        grid = scrape_price_grid(driver, now)
    except Exception as error:
        log(f"Scraping failed with an error: {error}")
    finally:
        if driver is not None:
            driver.quit()

    # --- Diagnostics: make failures visible instead of a silent "no data" ---
    if grid is None:
        log("Grid extraction returned None (JS extraction failed or grid had no rows).")
    else:
        log(f"Grid extracted successfully. Columns found: {grid.columns.tolist()}")
    # -------------------------------------------------------------------------

    price_column = find_todays_price_column(grid, today_date) if grid is not None else None

    if price_column is None and grid is not None:
        log(f"No column matched today's date ({today_date:%d/%m/%Y}) among the columns above.")

    if price_column is not None:
        records = build_records_from_grid(grid, price_column, today_iso)
        if records:
            upsert_rice_prices(supabase, records)
            log(f"Successfully scraped and stored data for {today_iso}.")
            return
        log("Today's column was found but contained no usable prices.")

    # Today's data is not available on the site yet.
    if not allow_fallback:
        log(f"No data published for {today_iso} yet. Will retry on the next scheduled run.")
        return

    log(f"No data published for {today_iso}. Applying forward-fill fallback ...")
    last_known_prices = get_last_known_prices(supabase, list(RICE_TYPE_MAP.values()))
    fallback_records = build_fallback_records(today_iso, last_known_prices)

    if not fallback_records:
        log("No historical prices available to forward-fill from. Nothing was stored.")
        sys.exit(1)

    upsert_rice_prices(supabase, fallback_records)
    log(f"Stored forward-filled data for {today_iso}.")


if __name__ == "__main__":
    #   python scrape_rice_price.py                  -> primary run behavior
    #   python scrape_rice_price.py --allow-fallback  -> retry/fallback behavior
    args = parse_args()
    main(allow_fallback=args.allow_fallback)