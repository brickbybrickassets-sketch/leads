# Cook County Motivated Seller Lead Scraper

Automated daily scraper for Cook County, Illinois public records — targeting distressed property owners via the Recorder of Deeds iCRIS portal.

## Lead Types Collected

| Code | Type |
|------|------|
| LP | Lis Pendens |
| NOFC | Notice of Foreclosure |
| TAXDEED | Tax Deed |
| JUD / CCJ / DRJUD | Judgment |
| LNCORPTX / LNIRS / LNFED | Tax / Federal Liens |
| LN / LNMECH / LNHOA | Liens |
| MEDLN | Medicaid Lien |
| PRO | Probate |
| NOC | Notice of Commencement |
| RELLP | Release Lis Pendens |

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_ORG/cook-county-leads.git
cd cook-county-leads
```

### 2. Install dependencies

```bash
pip install -r scraper/requirements.txt
python -m playwright install --with-deps chromium
```

### 3. Run locally

```bash
python scraper/fetch.py
```

Output files:
- `dashboard/records.json` — full dataset
- `data/records.json` — backup copy
- `dashboard/ghl_export.csv` — GoHighLevel import CSV

### 4. GitHub Actions (automated)

The workflow runs daily at **7:00 AM UTC** and on manual dispatch.

Enable GitHub Pages on the `dashboard/` folder to get a live dashboard.

**Required GitHub settings:**
- Settings → Pages → Source: **GitHub Actions**
- Settings → Actions → Workflow permissions: **Read and write**

## Seller Score (0–100)

| Condition | Points |
|-----------|--------|
| Base | 30 |
| Per distress flag | +10 |
| LP + Foreclosure combo | +20 |
| Amount > $100K | +15 |
| Amount > $50K | +10 |
| Filed this week | +5 |

## Data Sources

- **Records:** [Cook County Recorder of Deeds — iCRIS](https://i2.ccrd.us/d4dccrs/d4dccrs.aspx)
- **Parcel/Address data:** [Cook County Assessor Open Data](https://datacatalog.cookcountyil.gov/api/views/tx2p-k2g9/rows.csv)

## File Structure

```
├── scraper/
│   ├── fetch.py              # Main scraper
│   └── requirements.txt
├── dashboard/
│   ├── index.html            # Live dashboard (GitHub Pages)
│   ├── records.json          # Latest records output
│   └── ghl_export.csv        # GHL import CSV
├── data/
│   └── records.json          # Backup copy
└── .github/
    └── workflows/
        └── scrape.yml        # Daily automation
```
