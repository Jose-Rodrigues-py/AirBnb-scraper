# 🏠 Airbnb Occupancy Tracker

A data pipeline that scrapes Airbnb daily, tracks listing availability over
time to infer bookings, and builds a dataset for ML analysis of rental profitability.

---

## How it works

```
[Apify Scraper] → [Raw Listing Data] → [SQLite DB] → [Occupancy Detection] → [ML Dataset CSV]
      ↑                                      ↑
  (cloud, handles                    (runs locally,
   anti-bot stuff)                    your machine)
```

The core trick: Airbnb doesn't show who booked what.
But if we check every listing's availability calendar daily, we can detect
when a previously-available date becomes unavailable → that's a booking.

---

## Project structure

```
airbnb_tracker/
│
├── .env.example        ← Copy to .env and fill in your token
├── main.py             ← ENTRY POINT: run this
├── scraper.py          ← Calls Apify API, parses raw data
├── db.py               ← SQLite schema + read/write helpers
├── occupancy.py        ← Booking detection logic
├── export_for_ml.py    ← Exports clean CSV for pandas/sklearn
│
├── data/
│   └── airbnb.db       ← Created automatically on first run
└── logs/
    └── tracker_YYYYMM.log
```

---

## Setup (one-time)

### 1. Get a free Apify account & token

1. Go to https://apify.com and create a free account
2. Go to https://console.apify.com/account/integrations
3. Copy your **Personal API Token**
4. Apify free tier gives you $5/month of compute credits
   → At 100 listings/day, you'll use roughly $1–2/month

### 2. Configure

```bash
cp .env.example .env
# Edit .env and paste your token:
# APIFY_API_TOKEN=apify_api_xxxxxxxxxxxxxxxx
```

### 3. Install dependencies

```bash
pip install requests python-dotenv schedule sqlite-utils
```

---

## Running

```bash
# Test run (scrapes but doesn't save anything)
python main.py --dry-run

# One real run (saves to DB)
python main.py

# Run forever, every 24 hours
python main.py --schedule

# See what's in your database
python main.py --report

# Export a CSV for ML
python export_for_ml.py
```

---

## Automating with cron (Linux/Mac)

Run once a day at 8am automatically, without keeping a terminal open:

```bash
# Open cron editor
crontab -e

# Add this line (adjust the path!):
0 8 * * * cd /path/to/airbnb_tracker && python main.py >> logs/cron.log 2>&1
```

---

## Database tables

| Table           | What's in it                                           |
|-----------------|--------------------------------------------------------|
| `listings`      | One row per listing. Static info: bedrooms, type, etc. |
| `snapshots`     | One row per (listing × scrape day). Price + calendar.  |
| `calendar_days` | One row per (listing × date). Tracks available/booked. |

Query example (in any SQLite browser or Python):
```sql
-- Most profitable listing types in last 30 days
SELECT listing_type, bedrooms,
       AVG(price_per_night) as avg_price,
       COUNT(*) as n_listings
FROM listings l
JOIN snapshots s ON s.listing_id = l.listing_id
GROUP BY listing_type, bedrooms
ORDER BY avg_price DESC;
```

---

## After 2–3 months: ML features

Your `airbnb_ml_dataset.csv` will have these columns:

**Input features (X):**
- `listing_type`, `room_type`
- `bedrooms`, `bathrooms`, `max_guests`
- `latitude`, `longitude`, `neighborhood`
- `avg_price_per_night`
- `has_wifi`, `has_pool`, `has_parking`, `has_ac`, `has_balcony`, ...
- `host_is_superhost`, `rating_overall`, `rating_count`

**Target variables (y):**
- `occupancy_rate` → regression target
- `estimated_revenue` → regression target
- `is_top_quartile_revenue` → binary classification target (add this yourself)

Suggested first model:
```python
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

df = pd.read_csv("data/airbnb_ml_dataset.csv")
df = df[df['occupancy_rate'].notna()]

features = ['bedrooms', 'bathrooms', 'max_guests', 'avg_price_per_night',
            'has_wifi', 'has_pool', 'has_parking', 'host_is_superhost']
X = df[features].fillna(0)
y = df['occupancy_rate']

model = RandomForestRegressor(n_estimators=100)
model.fit(X, y)

# Feature importance — tells you what actually drives occupancy
import matplotlib.pyplot as plt
pd.Series(model.feature_importances_, index=features).sort_values().plot(kind='barh')
plt.title("What drives occupancy rate?")
plt.show()
```

---

## Extending to other cities / platforms

The scraper module is the only part that touches Airbnb.
To add a new city: just update `TARGET_LOCATION` in `.env`.
To scrape a different platform: swap out `scraper.py` — the DB and
occupancy logic stay the same.
