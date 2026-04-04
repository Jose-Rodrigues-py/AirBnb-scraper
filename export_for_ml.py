"""
export_for_ml.py — Export dataset for machine learning
=======================================================
After a few months of data, run this to generate a clean CSV
that's ready for pandas / scikit-learn / any ML framework.

Each row represents one listing with:
  - Static features (bedrooms, type, location, amenities...)
  - Derived performance metrics (occupancy rate, avg price, est. revenue)

Usage:
  python export_for_ml.py                        # exports last 90 days
  python export_for_ml.py --start 2024-01-01     # custom start date
  python export_for_ml.py --output my_data.csv   # custom output file
"""

import os
import csv
import json
import argparse
from datetime import date, timedelta
from dotenv import load_dotenv
import db


def export(db_path: str, start_date: str, end_date: str, output_path: str):
    """Export a flat ML-ready CSV from the database."""
    conn = db.get_connection(db_path)

    rows = conn.execute("""
        SELECT
            -- ── LISTING IDENTITY ─────────────────────────────────────────
            l.listing_id,
            l.listing_type,
            l.room_type,
            l.bedrooms,
            l.bathrooms,
            l.max_guests,
            l.latitude,
            l.longitude,
            l.neighborhood,
            l.city,
            l.host_is_superhost,
            l.rating_overall,
            l.rating_count,

            -- ── PRICING (average across all snapshots) ───────────────────
            AVG(s.price_per_night)                          AS avg_price_per_night,
            MIN(s.price_per_night)                          AS min_price,
            MAX(s.price_per_night)                          AS max_price,
            AVG(s.min_nights)                               AS avg_min_nights,
            COUNT(DISTINCT s.id)                            AS snapshot_count,

            -- ── OCCUPANCY ─────────────────────────────────────────────────
            SUM(CASE WHEN c.status = 'booked'    THEN 1 ELSE 0 END) AS booked_days,
            SUM(CASE WHEN c.status = 'available' THEN 1 ELSE 0 END) AS available_days,
            COUNT(c.calendar_date)                          AS total_tracked_days,
            SUM(CASE WHEN c.status = 'booked'    THEN 1 ELSE 0 END) * 1.0
                / NULLIF(COUNT(c.calendar_date), 0)         AS occupancy_rate,

            -- ── REVENUE ESTIMATE ──────────────────────────────────────────
            -- For booked days: use calendar price if we have it,
            -- else fall back to avg listed price
            SUM(CASE WHEN c.status = 'booked'
                THEN COALESCE(c.price, s_avg.avg_p, 0)
                ELSE 0 END)                                 AS estimated_revenue,

            -- ── AMENITY FLAGS (useful features for ML) ───────────────────
            CASE WHEN l.amenities LIKE '%WiFi%'        THEN 1 ELSE 0 END AS has_wifi,
            CASE WHEN l.amenities LIKE '%Pool%'        THEN 1 ELSE 0 END AS has_pool,
            CASE WHEN l.amenities LIKE '%parking%'     THEN 1 ELSE 0 END AS has_parking,
            CASE WHEN l.amenities LIKE '%washer%'
                           OR l.amenities LIKE '%Washer%' THEN 1 ELSE 0 END AS has_washer,
            CASE WHEN l.amenities LIKE '%kitchen%'
                           OR l.amenities LIKE '%Kitchen%' THEN 1 ELSE 0 END AS has_kitchen,
            CASE WHEN l.amenities LIKE '%Air conditioning%'
                           OR l.amenities LIKE '%AC%'    THEN 1 ELSE 0 END AS has_ac,
            CASE WHEN l.amenities LIKE '%Gym%'         THEN 1 ELSE 0 END AS has_gym,
            CASE WHEN l.amenities LIKE '%Balcony%'
                           OR l.amenities LIKE '%balcony%' THEN 1 ELSE 0 END AS has_balcony

        FROM listings l
        LEFT JOIN snapshots s
            ON s.listing_id = l.listing_id
        LEFT JOIN calendar_days c
            ON c.listing_id = l.listing_id
           AND c.calendar_date BETWEEN ? AND ?
        LEFT JOIN (
            SELECT listing_id, AVG(price_per_night) AS avg_p
            FROM snapshots
            GROUP BY listing_id
        ) s_avg ON s_avg.listing_id = l.listing_id
        GROUP BY l.listing_id
        HAVING snapshot_count > 0
        ORDER BY estimated_revenue DESC
    """, (start_date, end_date)).fetchall()

    conn.close()

    if not rows:
        print("No data to export. Run the scraper for a few weeks first.")
        return

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows([dict(r) for r in rows])

    print(f"✓ Exported {len(rows)} listings to {output_path}")
    print(f"  Date range: {start_date} → {end_date}")
    print(f"\nColumns in your dataset:")
    for col in rows[0].keys():
        print(f"  - {col}")
    print(f"\nNext step: load with pandas:")
    print(f"  import pandas as pd")
    print(f"  df = pd.read_csv('{output_path}')")
    print(f"  df[df['occupancy_rate'].notna()].sort_values('estimated_revenue', ascending=False)")


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(description="Export Airbnb dataset for ML")
    parser.add_argument("--start",  default=str(date.today() - timedelta(days=90)))
    parser.add_argument("--end",    default=str(date.today()))
    parser.add_argument("--output", default="data/airbnb_ml_dataset.csv")
    args = parser.parse_args()

    db_path = os.getenv("DB_PATH", "data/airbnb.db")
    export(db_path, args.start, args.end, args.output)
