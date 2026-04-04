"""
Infer bookings from calendar changes
This is the "secret sauce" of the whole pipeline.

PROBLEM: Airbnb doesn't tell us when a booking happens.
SOLUTION: Compare today's availability calendar with yesterday's.
          If a date was available before and isn't now → it was booked!

This gives us an estimated occupancy rate over time, which is the 
strongest predictor of a property's revenue.

LIMITATIONS:
  - We can't distinguish "booked by guest" vs "blocked by host"
  - We miss bookings that happen and are cancelled between our daily runs
  - Prices for booked dates may not reflect what the guest actually paid
    (Airbnb can show different prices to different users)
"""

import json
import sqlite3
from datetime import datetime
from db import (
    get_previous_available_dates,
    upsert_calendar_day,
    get_connection
)


def detect_and_store_occupancy(conn: str, listing_id: str,
                                new_available: list[str],
                                new_unavailable: list[str],
                                calendar_prices: dict):
    """
    Compare the new calendar snapshot to the previous one.
    Detect newly-booked dates and update the calendar_days table.

    Args:
        db_path:         Path to the SQLite file
        listing_id:      The Airbnb listing ID
        new_available:   Dates available in THIS scrape
        new_unavailable: Dates unavailable in THIS scrape
        calendar_prices: {date: price} for available dates
    """
    
    # Get what was available LAST TIME we scraped this listing
    prev_available = get_previous_available_dates(conn, listing_id)
    new_available_set = set(new_available)
    new_unavailable_set = set(new_unavailable)

    newly_booked = []
    still_available = []
    still_unavailable = []

    # ── Compare old vs new ────────────────────────────────────────────
    for date in new_unavailable_set:
        if date in prev_available:
            # Was available → now unavailable = BOOKING DETECTED
            newly_booked.append(date)
            upsert_calendar_day(conn, listing_id, date, "booked",
                                price=calendar_prices.get(date))
        else:
            # Was unavailable before too — host blocked or already booked
            upsert_calendar_day(conn, listing_id, date,
                                "blocked_by_host_or_prior_booking",
                                price=None)

    for date in new_available_set:
        upsert_calendar_day(conn, listing_id, date, "available",
                            price=calendar_prices.get(date))
        still_available.append(date)

    conn.commit()

    if newly_booked:
        print(f"  [occupancy] {listing_id}: detected {len(newly_booked)} "
                f"new booking(s) on: {', '.join(sorted(newly_booked)[:5])}"
                + (" ..." if len(newly_booked) > 5 else ""))



def compute_occupancy_rate(db_path: str, listing_id: str,
                            start_date: str, end_date: str) -> dict:
    """
    Calculate occupancy statistics for a listing over a date range.

    Returns a dict with:
      - occupancy_rate: 0.0–1.0 (fraction of days that were booked)
      - booked_days: count
      - available_days: count
      - estimated_revenue: sum of prices on booked days (rough estimate)
    """
    conn = get_connection(db_path)
    rows = conn.execute("""
        SELECT status, price, COUNT(*) as cnt, SUM(COALESCE(price, 0)) as revenue
        FROM calendar_days
        WHERE listing_id = ?
          AND calendar_date BETWEEN ? AND ?
        GROUP BY status
    """, (listing_id, start_date, end_date)).fetchall()
    conn.close()

    booked = 0
    available = 0
    est_revenue = 0.0

    for row in rows:
        if row["status"] == "booked":
            booked += row["cnt"]
            est_revenue += row["revenue"] or 0
        elif row["status"] == "available":
            available += row["cnt"]

    total = booked + available
    return {
        "listing_id": listing_id,
        "start_date": start_date,
        "end_date": end_date,
        "booked_days": booked,
        "available_days": available,
        "total_tracked_days": total,
        "occupancy_rate": round(booked / total, 4) if total > 0 else None,
        "estimated_revenue_eur": round(est_revenue, 2),
    }


def get_market_summary(db_path: str, start_date: str, end_date: str) -> list[dict]:
    """
    Aggregate occupancy stats by listing type and bedroom count.
    This is the table you'll eventually feed into your ML model.

    Returns rows like:
      listing_type | bedrooms | avg_occupancy | avg_price | avg_revenue | count
    """
    conn = get_connection(db_path)

    rows = conn.execute("""
        SELECT
            l.listing_type,
            l.bedrooms,
            COUNT(DISTINCT l.listing_id)                    AS listing_count,
            AVG(daily.occ_rate)                             AS avg_occupancy_rate,
            AVG(s.price_per_night)                          AS avg_price_per_night,
            AVG(daily.est_rev)                              AS avg_estimated_revenue
        FROM listings l
        -- Latest snapshot per listing (for price)
        JOIN (
            SELECT listing_id, price_per_night,
                   ROW_NUMBER() OVER (PARTITION BY listing_id ORDER BY scraped_at DESC) as rn
            FROM snapshots
        ) s ON s.listing_id = l.listing_id AND s.rn = 1
        -- Occupancy rate per listing
        JOIN (
            SELECT
                listing_id,
                SUM(CASE WHEN status = 'booked' THEN 1 ELSE 0 END) * 1.0
                    / NULLIF(COUNT(*), 0)                   AS occ_rate,
                SUM(CASE WHEN status = 'booked'
                         THEN COALESCE(price, 0) ELSE 0 END) AS est_rev
            FROM calendar_days
            WHERE calendar_date BETWEEN ? AND ?
            GROUP BY listing_id
        ) daily ON daily.listing_id = l.listing_id
        WHERE l.listing_type IS NOT NULL
        GROUP BY l.listing_type, l.bedrooms
        ORDER BY avg_estimated_revenue DESC
    """, (start_date, end_date)).fetchall()

    conn.close()
    return [dict(r) for r in rows]
