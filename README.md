# Airbnb Occupancy Tracker

A data pipeline that scrapes Airbnb two times a week and tracks listing availability over
time to infer bookings, and builds a dataset for ML analysis of rental profitability.

This project was inspired by a challenge from an architect who wanted to understand which property characteristics drive Airbnb profitability. It explores how publicly available Airbnb data can be transformed into occupancy and investment insights.

You can see how the app looks like in the two files named "airbnb_project_image1" and "airbnb_project_image2".

---

## How it works

```
[Apify Scraper] → [Raw Listing Data] → [SQLite DB] → [Occupancy Detection] → [ML Dataset CSV]
      ↑                                      ↑
  (cloud, handles                    (runs locally)
   anti-bot stuff)                  
```

The core trick: Airbnb doesn't show who booked what.
But if we check every listing's availability calendar daily, we can detect
when a previously-available date becomes unavailable → that's a booking.

---
## Project structure
```
airbnb_tracker/
│
├── .env.example    
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
** Important note: 
Apify is a "Freemium" platform, meaning that it is free to use, but limited. 
In the free tier, I could only run once a month, which makes it impossible for the logic to work seamlessly.
In this way, although the app works, it has never been fully tested (no ML was ever conducted).
