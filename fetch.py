"""
Cook County, Illinois — Motivated Seller Lead Scraper
Targets: Cook County Recorder of Deeds (iCRIS portal)
Lookback: 30 days
Lead types: LP, NOFC, TAXDEED, JUD, CCJ, DRJUD, LNCORPTX, LNIRS, LNFED,
            LN, LNMECH, LNHOA, MEDLN, PRO, NOC, RELLP
"""

import asyncio
import csv
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── Optional heavy imports (fail gracefully) ────────────────────────────────
try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    logging.warning("Playwright not installed — browser scraping disabled.")

try:
    from dbfread import DBF
    HAS_DBFREAD = True
except ImportError:
    HAS_DBFREAD = False
    logging.warning("dbfread not installed — parcel lookup disabled.")

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "30"))
BASE_DIR = Path(__file__).parent.parent
OUTPUT_PATHS = [BASE_DIR / "dashboard" / "records.json", BASE_DIR / "data" / "records.json"]
GHL_CSV_PATH = BASE_DIR / "dashboard" / "ghl_export.csv"

# Cook County Recorder of Deeds iCRIS portal
CLERK_BASE = "https://i2.ccrd.us"
CLERK_SEARCH_URL = f"{CLERK_BASE}/d4dccrs/d4dccrs.aspx"

# Cook County Assessor bulk parcel data
PARCEL_DBF_URL = "https://datacatalog.cookcountyil.gov/api/views/tx2p-k2g9/rows.csv?accessType=DOWNLOAD"
PARCEL_CSV_FALLBACK = BASE_DIR / "data" / "parcels.csv"
PARCEL_DBF_PATH = BASE_DIR / "data" / "parcels.dbf"

# Document type → category mapping
DOC_TYPE_MAP = {
    # Foreclosure / Lis Pendens
    "LP": ("foreclosure", "Lis Pendens"),
    "NOFC": ("foreclosure", "Notice of Foreclosure"),
    "RELLP": ("foreclosure", "Release Lis Pendens"),
    # Tax
    "TAXDEED": ("tax", "Tax Deed"),
    "LNCORPTX": ("tax", "Corp Tax Lien"),
    "LNIRS": ("tax", "IRS Lien"),
    "LNFED": ("tax", "Federal Lien"),
    # Judgments
    "JUD": ("judgment", "Judgment"),
    "CCJ": ("judgment", "Certified Judgment"),
    "DRJUD": ("judgment", "Domestic Judgment"),
    # Liens
    "LN": ("lien", "Lien"),
    "LNMECH": ("lien", "Mechanic Lien"),
    "LNHOA": ("lien", "HOA Lien"),
    "MEDLN": ("lien", "Medicaid Lien"),
    # Other
    "PRO": ("probate", "Probate Document"),
    "NOC": ("notice", "Notice of Commencement"),
}

TARGET_DOC_TYPES = list(DOC_TYPE_MAP.keys())

# iCRIS document-type codes used in their search form
ICRIS_TYPE_CODES = {
    "LP": "LIS PENDENS",
    "NOFC": "FORECLOSURE NOTICE",
    "RELLP": "RELEASE LIS PENDENS",
    "TAXDEED": "TAX DEED",
    "JUD": "JUDGMENT",
    "CCJ": "CERTIFIED COPY OF JUDG",
    "DRJUD": "DOMESTIC RELATIONS JUDG",
    "LNCORPTX": "CORP TAX LIEN",
    "LNIRS": "IRS LIEN",
    "LNFED": "FEDERAL TAX LIEN",
    "LN": "LIEN",
    "LNMECH": "MECHANICS LIEN",
    "LNHOA": "HOA LIEN",
    "MEDLN": "MEDICAID LIEN",
    "PRO": "PROBATE",
    "NOC": "NOTICE OF COMMENCEMENT",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def date_range():
    end = datetime.today()
    start = end - timedelta(days=LOOKBACK_DAYS)
    return start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")


def parse_amount(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def safe_str(val) -> str:
    if val is None:
        return ""
    return str(val).strip()


def week_ago() -> datetime:
    return datetime.today() - timedelta(days=7)


def compute_score(record: dict) -> tuple[int, list[str]]:
    """Return (score 0-100, flags list)."""
    flags = []
    score = 30
    cat = record.get("cat", "")
    doc_type = record.get("doc_type", "")
    amount = record.get("amount")
    filed_str = record.get("filed", "")
    owner = record.get("owner", "").upper()

    # Distress flags
    if cat == "foreclosure" or doc_type in ("LP", "NOFC"):
        flags.append("Lis pendens" if doc_type == "LP" else "Pre-foreclosure")
        score += 10
    if cat == "judgment":
        flags.append("Judgment lien")
        score += 10
    if cat == "tax" or doc_type in ("LNIRS", "LNFED", "LNCORPTX", "TAXDEED"):
        flags.append("Tax lien")
        score += 10
    if doc_type == "LNMECH":
        flags.append("Mechanic lien")
        score += 10
    if cat == "probate":
        flags.append("Probate / estate")
        score += 10
    if doc_type == "LNHOA":
        flags.append("HOA lien")
        score += 10

    # LP + FC combo
    if doc_type in ("LP", "NOFC") and cat == "foreclosure":
        score += 20

    # Amount bonuses
    if amount:
        if amount > 100_000:
            flags.append("High debt (>$100K)")
            score += 15
        elif amount > 50_000:
            flags.append("Significant debt (>$50K)")
            score += 10

    # LLC / Corp owner
    if any(kw in owner for kw in ("LLC", "INC", "CORP", "LTD", "LP ", "TRUST")):
        flags.append("LLC / corp owner")
        score += 5

    # Filed this week
    try:
        filed_dt = datetime.strptime(filed_str, "%Y-%m-%d")
        if filed_dt >= week_ago():
            flags.append("New this week")
            score += 5
    except (ValueError, TypeError):
        pass

    return min(score, 100), flags


# ── Parcel / Address Lookup ──────────────────────────────────────────────────

class ParcelLookup:
    """
    Builds an owner-name → address lookup from the Cook County Assessor
    parcel data (CSV via Socrata open data portal, or DBF fallback).
    """

    def __init__(self):
        self._index: dict[str, dict] = {}

    def _normalize(self, name: str) -> str:
        return re.sub(r"\s+", " ", name.upper().strip())

    def _add(self, key: str, record: dict):
        key = self._normalize(key)
        if key:
            self._index[key] = record

    def load_csv(self, path: Path):
        log.info("Loading parcel CSV from %s", path)
        try:
            import csv as _csv
            with open(path, newline="", encoding="utf-8", errors="replace") as f:
                reader = _csv.DictReader(f)
                for row in reader:
                    self._ingest_row(row)
            log.info("Loaded %d parcel records", len(self._index))
        except Exception as e:
            log.error("Parcel CSV load error: %s", e)

    def load_dbf(self, path: Path):
        if not HAS_DBFREAD:
            log.warning("dbfread not available; skipping DBF load")
            return
        log.info("Loading parcel DBF from %s", path)
        try:
            for row in DBF(str(path), encoding="latin-1", ignore_missing_memofile=True):
                self._ingest_row(dict(row))
            log.info("Loaded %d parcel records", len(self._index))
        except Exception as e:
            log.error("Parcel DBF load error: %s", e)

    def _ingest_row(self, row: dict):
        # Normalize column names (handle both upper and lower-case field names)
        r = {k.upper(): v for k, v in row.items()}

        owner = safe_str(r.get("OWNER") or r.get("OWN1") or r.get("TAXPAYER_NAME") or "")
        site_addr = safe_str(r.get("SITE_ADDR") or r.get("SITEADDR") or r.get("PROPERTY_ADDRESS") or "")
        site_city = safe_str(r.get("SITE_CITY") or r.get("PROPERTY_CITY") or "")
        site_zip = safe_str(r.get("SITE_ZIP") or r.get("PROPERTY_ZIP") or "")
        mail_addr = safe_str(r.get("ADDR_1") or r.get("MAILADDR") or r.get("MAILING_ADDRESS") or "")
        mail_city = safe_str(r.get("CITY") or r.get("MAILCITY") or r.get("MAILING_CITY") or "")
        mail_state = safe_str(r.get("STATE") or r.get("MAILSTATE") or r.get("MAILING_STATE") or "")
        mail_zip = safe_str(r.get("ZIP") or r.get("MAILZIP") or r.get("MAILING_ZIP") or "")

        record = {
            "prop_address": site_addr,
            "prop_city": site_city,
            "prop_state": "IL",
            "prop_zip": site_zip,
            "mail_address": mail_addr,
            "mail_city": mail_city,
            "mail_state": mail_state,
            "mail_zip": mail_zip,
        }

        if owner:
            parts = owner.split(",")
            last = parts[0].strip()
            first = parts[1].strip() if len(parts) > 1 else ""
            # Index by "FIRST LAST", "LAST FIRST", "LAST, FIRST", full
            for variant in [
                owner,
                f"{first} {last}",
                f"{last} {first}",
                f"{last}, {first}",
            ]:
                self._add(variant, record)

    def lookup(self, owner: str) -> Optional[dict]:
        key = self._normalize(owner)
        return self._index.get(key)

    def download_and_load(self):
        """Try to download the Cook County Assessor CSV via Socrata."""
        csv_path = PARCEL_CSV_FALLBACK
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        if not csv_path.exists():
            log.info("Downloading parcel data from Cook County open data portal…")
            try:
                r = requests.get(PARCEL_DBF_URL, timeout=120, stream=True)
                r.raise_for_status()
                with open(csv_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                log.info("Parcel CSV saved to %s", csv_path)
            except Exception as e:
                log.error("Could not download parcel data: %s", e)
                return

        if csv_path.exists():
            self.load_csv(csv_path)
        elif PARCEL_DBF_PATH.exists():
            self.load_dbf(PARCEL_DBF_PATH)


# ── iCRIS Scraper (Playwright) ───────────────────────────────────────────────

class ICRISScraper:
    """
    Scrapes the Cook County Recorder of Deeds iCRIS portal.
    URL: https://i2.ccrd.us/d4dccrs/d4dccrs.aspx
    Uses Playwright for JS-heavy POST-back navigation.
    """

    BASE = "https://i2.ccrd.us"
    SEARCH_URL = f"{BASE}/d4dccrs/d4dccrs.aspx"

    def __init__(self):
        self.records: list[dict] = []

    async def _fetch_type(self, page, doc_code: str, doc_label: str, start_date: str, end_date: str):
        """Search one document type and collect all results."""
        log.info("Searching iCRIS for %s (%s)…", doc_code, doc_label)
        retries = 3
        for attempt in range(1, retries + 1):
            try:
                await page.goto(self.SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_load_state("networkidle", timeout=30_000)

                # Fill the date-range fields (field names observed in iCRIS)
                for sel in ["#txtDateFrom", "#ctl00_cphMain_txtDateFrom", "input[name*='DateFrom']"]:
                    try:
                        await page.fill(sel, start_date, timeout=3000)
                        break
                    except Exception:
                        pass

                for sel in ["#txtDateTo", "#ctl00_cphMain_txtDateTo", "input[name*='DateTo']"]:
                    try:
                        await page.fill(sel, end_date, timeout=3000)
                        break
                    except Exception:
                        pass

                # Select document type in dropdown
                for sel in ["#ddlDocType", "#ctl00_cphMain_ddlDocType", "select[name*='DocType']"]:
                    try:
                        await page.select_option(sel, label=doc_label, timeout=3000)
                        break
                    except Exception:
                        try:
                            await page.select_option(sel, value=doc_code, timeout=3000)
                            break
                        except Exception:
                            pass

                # Click Search
                for sel in ["#btnSearch", "#ctl00_cphMain_btnSearch", "input[value='Search']", "button:has-text('Search')"]:
                    try:
                        await page.click(sel, timeout=5000)
                        break
                    except Exception:
                        pass

                await page.wait_for_load_state("networkidle", timeout=30_000)
                await self._parse_results_pages(page, doc_code)
                break  # success

            except PWTimeout:
                log.warning("Timeout on attempt %d for %s", attempt, doc_code)
                if attempt == retries:
                    log.error("Giving up on %s after %d attempts", doc_code, retries)
            except Exception as e:
                log.warning("Error attempt %d for %s: %s", attempt, doc_code, e)
                if attempt == retries:
                    log.error("Giving up on %s: %s", doc_code, e)

    async def _parse_results_pages(self, page, doc_code: str):
        """Iterate result pages and extract records."""
        page_num = 0
        while True:
            page_num += 1
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            rows = self._extract_rows(soup, doc_code)
            self.records.extend(rows)
            log.info("  Page %d: %d rows (total so far: %d)", page_num, len(rows), len(self.records))

            # Look for "Next" pagination link / button
            next_btn = (
                soup.find("a", string=re.compile(r"Next", re.I))
                or soup.find("input", {"value": re.compile(r"Next", re.I)})
                or soup.find("a", id=re.compile(r"Next", re.I))
            )
            if not next_btn:
                break

            # Try clicking Next
            try:
                href = next_btn.get("href", "")
                if "__doPostBack" in href:
                    match = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
                    if match:
                        et, ea = match.group(1), match.group(2)
                        await page.evaluate(f"__doPostBack('{et}','{ea}')")
                        await page.wait_for_load_state("networkidle", timeout=20_000)
                        continue
                for sel in [
                    "a:has-text('Next')",
                    "input[value='Next']",
                    f"#{next_btn.get('id', '_')}",
                ]:
                    try:
                        await page.click(sel, timeout=5000)
                        await page.wait_for_load_state("networkidle", timeout=20_000)
                        break
                    except Exception:
                        pass
                else:
                    break
            except Exception:
                break

    def _extract_rows(self, soup: BeautifulSoup, doc_code: str) -> list[dict]:
        """Parse HTML table rows into record dicts."""
        records = []
        # iCRIS renders results in a table; grab all <tr> with ≥5 <td> cells
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            texts = [td.get_text(" ", strip=True) for td in tds]

            # Heuristic column mapping for iCRIS layout:
            # [0]=Doc#, [1]=Type, [2]=Date, [3]=Grantor, [4]=Grantee, [5]=Legal, [6]=Amount
            try:
                doc_num = safe_str(texts[0])
                if not doc_num or not re.search(r"\d", doc_num):
                    continue  # skip header / non-data rows

                filed_raw = safe_str(texts[2])
                try:
                    filed = datetime.strptime(filed_raw, "%m/%d/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    filed = filed_raw

                # Build direct link
                # iCRIS document viewer URL pattern
                doc_link = tds[0].find("a")
                if doc_link and doc_link.get("href"):
                    href = doc_link["href"]
                    clerk_url = href if href.startswith("http") else f"{self.BASE}{href}"
                else:
                    clerk_url = (
                        f"{self.BASE}/d4dccrs/DocView.aspx?doc={doc_num}"
                    )

                amount_text = texts[6] if len(texts) > 6 else ""
                amount = parse_amount(amount_text)

                record = {
                    "doc_num": doc_num,
                    "doc_type": doc_code,
                    "filed": filed,
                    "owner": safe_str(texts[3]) if len(texts) > 3 else "",
                    "grantee": safe_str(texts[4]) if len(texts) > 4 else "",
                    "legal": safe_str(texts[5]) if len(texts) > 5 else "",
                    "amount": amount,
                    "clerk_url": clerk_url,
                    # address fields filled in later from parcel lookup
                    "prop_address": "",
                    "prop_city": "",
                    "prop_state": "IL",
                    "prop_zip": "",
                    "mail_address": "",
                    "mail_city": "",
                    "mail_state": "",
                    "mail_zip": "",
                }
                records.append(record)
            except Exception as e:
                log.debug("Row parse error: %s — %s", e, texts[:6])
        return records

    async def run(self, start_date: str, end_date: str) -> list[dict]:
        if not HAS_PLAYWRIGHT:
            log.error("Playwright not available — cannot scrape iCRIS")
            return []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()

            for doc_code, doc_label in ICRIS_TYPE_CODES.items():
                await self._fetch_type(page, doc_code, doc_label, start_date, end_date)

            await browser.close()

        log.info("iCRIS scrape complete. Raw records: %d", len(self.records))
        return self.records


# ── Fallback: Static / requests-based scraper ────────────────────────────────

class StaticScraper:
    """
    Fallback scraper using requests + BeautifulSoup for any static pages
    or __doPostBack endpoints that can be driven without a full browser.
    Also attempts the Cook County Assessor property search as a secondary
    address-lookup path.
    """

    SESSION_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.SESSION_HEADERS)
        self.records: list[dict] = []

    def _get_with_retry(self, url: str, retries=3, **kwargs) -> Optional[requests.Response]:
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(url, timeout=30, **kwargs)
                r.raise_for_status()
                return r
            except Exception as e:
                log.warning("GET attempt %d/%d for %s: %s", attempt, retries, url, e)
                time.sleep(2 ** attempt)
        return None

    def _post_with_retry(self, url: str, data: dict, retries=3) -> Optional[requests.Response]:
        for attempt in range(1, retries + 1):
            try:
                r = self.session.post(url, data=data, timeout=30)
                r.raise_for_status()
                return r
            except Exception as e:
                log.warning("POST attempt %d/%d for %s: %s", attempt, retries, url, e)
                time.sleep(2 ** attempt)
        return None

    def fetch_icris_static(self, start_date: str, end_date: str) -> list[dict]:
        """
        Attempt a direct GET/POST to iCRIS search for each document type.
        iCRIS is ASP.NET WebForms — we grab __VIEWSTATE from the initial GET
        then POST back with the search parameters.
        """
        log.info("Attempting static iCRIS scrape (requests fallback)…")
        records = []

        # Load the search page to grab ASP.NET state tokens
        resp = self._get_with_retry(CLERK_SEARCH_URL)
        if not resp:
            log.error("Could not reach iCRIS search page")
            return records

        soup = BeautifulSoup(resp.text, "lxml")
        viewstate = soup.find("input", {"id": "__VIEWSTATE"})
        vs_gen = soup.find("input", {"id": "__VIEWSTATEGENERATOR"})
        event_valid = soup.find("input", {"id": "__EVENTVALIDATION"})

        base_payload = {
            "__VIEWSTATE": viewstate["value"] if viewstate else "",
            "__VIEWSTATEGENERATOR": vs_gen["value"] if vs_gen else "",
            "__EVENTVALIDATION": event_valid["value"] if event_valid else "",
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
        }

        for doc_code, doc_label in ICRIS_TYPE_CODES.items():
            log.info("  Static fetch: %s", doc_code)
            payload = dict(base_payload)
            # These field names are typical for iCRIS; adjust if portal changes
            payload.update({
                "ctl00$cphMain$txtDateFrom": start_date,
                "ctl00$cphMain$txtDateTo": end_date,
                "ctl00$cphMain$ddlDocType": doc_label,
                "ctl00$cphMain$btnSearch": "Search",
            })

            r = self._post_with_retry(CLERK_SEARCH_URL, payload)
            if not r:
                continue

            soup2 = BeautifulSoup(r.text, "lxml")
            batch = self._parse_result_table(soup2, doc_code)
            records.extend(batch)
            log.info("    → %d records", len(batch))
            time.sleep(1)

        return records

    def _parse_result_table(self, soup: BeautifulSoup, doc_code: str) -> list[dict]:
        records = []
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            texts = [td.get_text(" ", strip=True) for td in tds]
            try:
                doc_num = safe_str(texts[0])
                if not doc_num or not re.search(r"\d", doc_num):
                    continue
                filed_raw = safe_str(texts[2])
                try:
                    filed = datetime.strptime(filed_raw, "%m/%d/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    filed = filed_raw

                doc_link = tds[0].find("a")
                if doc_link and doc_link.get("href"):
                    href = doc_link["href"]
                    clerk_url = href if href.startswith("http") else f"{CLERK_BASE}{href}"
                else:
                    clerk_url = f"{CLERK_BASE}/d4dccrs/DocView.aspx?doc={doc_num}"

                amount = parse_amount(texts[6]) if len(texts) > 6 else None
                records.append({
                    "doc_num": doc_num,
                    "doc_type": doc_code,
                    "filed": filed,
                    "owner": safe_str(texts[3]) if len(texts) > 3 else "",
                    "grantee": safe_str(texts[4]) if len(texts) > 4 else "",
                    "legal": safe_str(texts[5]) if len(texts) > 5 else "",
                    "amount": amount,
                    "clerk_url": clerk_url,
                    "prop_address": "", "prop_city": "",
                    "prop_state": "IL", "prop_zip": "",
                    "mail_address": "", "mail_city": "",
                    "mail_state": "", "mail_zip": "",
                })
            except Exception as e:
                log.debug("Static row parse error: %s", e)
        return records


# ── GHL CSV Export ────────────────────────────────────────────────────────────

GHL_COLUMNS = [
    "First Name", "Last Name", "Mailing Address", "Mailing City",
    "Mailing State", "Mailing Zip", "Property Address", "Property City",
    "Property State", "Property Zip", "Lead Type", "Document Type",
    "Date Filed", "Document Number", "Amount / Debt Owed",
    "Seller Score", "Motivated Seller Flags", "Source", "Public Records URL",
]


def split_name(full_name: str) -> tuple[str, str]:
    """Attempt to split 'LAST, FIRST' or 'FIRST LAST' into (first, last)."""
    if "," in full_name:
        parts = full_name.split(",", 1)
        return parts[1].strip().title(), parts[0].strip().title()
    parts = full_name.rsplit(" ", 1)
    if len(parts) == 2:
        return parts[0].strip().title(), parts[1].strip().title()
    return full_name.title(), ""


def write_ghl_csv(records: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GHL_COLUMNS)
        w.writeheader()
        for r in records:
            first, last = split_name(r.get("owner", ""))
            _, cat_label = DOC_TYPE_MAP.get(r.get("doc_type", ""), ("", r.get("doc_type", "")))
            flags = r.get("flags", [])
            w.writerow({
                "First Name": first,
                "Last Name": last,
                "Mailing Address": r.get("mail_address", ""),
                "Mailing City": r.get("mail_city", ""),
                "Mailing State": r.get("mail_state", ""),
                "Mailing Zip": r.get("mail_zip", ""),
                "Property Address": r.get("prop_address", ""),
                "Property City": r.get("prop_city", ""),
                "Property State": r.get("prop_state", "IL"),
                "Property Zip": r.get("prop_zip", ""),
                "Lead Type": r.get("cat", "").title(),
                "Document Type": cat_label,
                "Date Filed": r.get("filed", ""),
                "Document Number": r.get("doc_num", ""),
                "Amount / Debt Owed": r.get("amount", ""),
                "Seller Score": r.get("score", 0),
                "Motivated Seller Flags": "; ".join(flags),
                "Source": r.get("source", "Cook County Recorder of Deeds"),
                "Public Records URL": r.get("clerk_url", ""),
            })
    log.info("GHL CSV written: %s (%d rows)", path, len(records))


# ── Main Orchestrator ─────────────────────────────────────────────────────────

async def main():
    start_date, end_date = date_range()
    log.info("=== Cook County Motivated Seller Scraper ===")
    log.info("Date range: %s → %s", start_date, end_date)

    # 1. Load parcel data for address enrichment
    parcel = ParcelLookup()
    parcel.download_and_load()

    # 2. Scrape records
    raw_records: list[dict] = []

    if HAS_PLAYWRIGHT:
        scraper = ICRISScraper()
        raw_records = await scraper.run(start_date, end_date)
    
    # Always also attempt static as supplement / fallback
    if not raw_records:
        log.info("Falling back to static scraper…")
        static = StaticScraper()
        raw_records = static.fetch_icris_static(start_date, end_date)

    log.info("Total raw records collected: %d", len(raw_records))

    # 3. Deduplicate by doc_num
    seen = set()
    deduped = []
    for r in raw_records:
        key = r.get("doc_num", "")
        if key and key not in seen:
            seen.add(key)
            deduped.append(r)
    log.info("After dedup: %d records", len(deduped))

    # 4. Enrich with parcel data + scoring
    with_address = 0
    output_records = []

    for r in deduped:
        doc_code = r.get("doc_type", "")
        cat, cat_label = DOC_TYPE_MAP.get(doc_code, ("other", doc_code))
        r["cat"] = cat
        r["cat_label"] = cat_label
        r["source"] = "Cook County Recorder of Deeds"

        # Address enrichment
        owner = r.get("owner", "")
        if owner and parcel._index:
            parcel_rec = parcel.lookup(owner)
            if parcel_rec:
                r.update(parcel_rec)
                with_address += 1

        # Scoring
        score, flags = compute_score(r)
        r["score"] = score
        r["flags"] = flags

        output_records.append(r)

    # Sort by score descending
    output_records.sort(key=lambda x: x.get("score", 0), reverse=True)

    # 5. Build output payload
    payload = {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "source": "Cook County Recorder of Deeds — iCRIS portal",
        "date_range": {"start": start_date, "end": end_date},
        "total": len(output_records),
        "with_address": with_address,
        "records": output_records,
    }

    # 6. Save JSON
    for p in OUTPUT_PATHS:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        log.info("Saved: %s", p)

    # 7. GHL CSV export
    write_ghl_csv(output_records, GHL_CSV_PATH)

    log.info("=== Done. Total: %d | With address: %d ===", len(output_records), with_address)
    return payload


if __name__ == "__main__":
    asyncio.run(main())
