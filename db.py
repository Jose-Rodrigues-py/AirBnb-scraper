"""
db.py — Database layer
======================
We use SQLite (a single local file) — no server needed, perfect for a
personal dataset. The schema has 3 tables:

  listings      → one row per unique Airbnb listing (static info)
  snapshots     → one row every time we scrape a listing (changes over time)
  calendar_days → one row per (listing, date) pair — tracks availability

The key insight: we can't see who booked what. BUT if a date was
"available" yesterday and "unavailable" today → it was probably booked.
We detect that by comparing consecutive snapshots.
"""

import sqlite3
import os
from datetime import datetime, timezone


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite database and return a connection."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    conn.execute("PRAGMA journal_mode=WAL")  # safer for concurrent reads
    return conn


def init_db(db_path: str):
    """Create all tables if they don't exist yet. Safe to call repeatedly."""
    conn = get_connection(db_path)
    conn.executescript("""
        -- ── LISTINGS ──────────────────────────────────────────────────────
        -- One row per listing. We upsert (update if exists) on each scrape
        -- so static fields stay fresh if the host changes them.
        CREATE TABLE IF NOT EXISTS listings (
            listing_id      TEXT PRIMARY KEY,   -- Airbnb's own ID
            url             TEXT,
            name            TEXT,
            listing_type    TEXT,               -- "Entire home", "Private room", etc.
            room_type       TEXT,               -- internal Airbnb category
            bedrooms        INTEGER,
            max_guests      INTEGER,
            amenities       TEXT,               -- JSON array stored as text
            latitude        REAL,
            longitude       REAL,
            neighborhood    TEXT,
            city            TEXT,
            host_id         TEXT,
            rating_overall  REAL,
            first_seen      TEXT,               -- ISO datetime, set once
            last_seen       TEXT,                -- ISO datetime, updated every scrape
            description     TEXT, 
            local_description   TEXT
        );

        -- ── SNAPSHOTS ─────────────────────────────────────────────────────
        -- One row per (listing, scrape run). Captures price & availability
        -- as they were at that moment in time.
        CREATE TABLE IF NOT EXISTS snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id      TEXT NOT NULL,
            scraped_at      TEXT NOT NULL,      -- ISO datetime of this scrape
            price_per_night REAL,               -- in EUR (or local currency)
            currency        TEXT,
            min_nights      INTEGER,
            available_dates TEXT,               -- JSON list of available YYYY-MM-DD
            unavailable_dates TEXT,             -- JSON list of blocked YYYY-MM-DD
            FOREIGN KEY (listing_id) REFERENCES listings(listing_id)
        );

        -- ── CALENDAR_DAYS ─────────────────────────────────────────────────
        -- Granular: one row per (listing, calendar date).
        -- status: "available" | "booked" | "blocked_by_host"
        -- We update status when we detect a change between snapshots.
        CREATE TABLE IF NOT EXISTS calendar_days (
            listing_id      TEXT NOT NULL,
            calendar_date   TEXT NOT NULL,      -- YYYY-MM-DD
            status          TEXT NOT NULL,      -- available / booked / blocked
            price           REAL,               -- price for that specific night
            detected_at     TEXT,               -- when we detected this status
            PRIMARY KEY (listing_id, calendar_date),
            FOREIGN KEY (listing_id) REFERENCES listings(listing_id)
        );

        -- ── INDEXES for fast queries later ────────────────────────────────
        CREATE INDEX IF NOT EXISTS idx_snapshots_listing
            ON snapshots(listing_id, scraped_at);

        CREATE INDEX IF NOT EXISTS idx_calendar_listing
            ON calendar_days(listing_id, calendar_date);

        CREATE INDEX IF NOT EXISTS idx_listings_city
            ON listings(city, listing_type, bedrooms);
    """)
    conn.commit()
    conn.close()
    print(f"[db] Database ready at {db_path}")


def upsert_listing(conn: sqlite3.Connection, listing: dict):
    """Insert a new listing or update it if we've seen it before."""
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    # Check if it already exists so we can preserve first_seen
    existing = conn.execute(
        "SELECT first_seen FROM listings WHERE listing_id = ?",
        (listing["listing_id"],)
    ).fetchone()

    first_seen = existing["first_seen"] if existing else now

    conn.execute("""
        INSERT INTO listings (
            listing_id, url, name, listing_type, room_type,
            bedrooms, max_guests, amenities,
            latitude, longitude, neighborhood, city,
            host_id, 
            rating_overall,
            first_seen, last_seen, 
            description, local_description
        ) VALUES (
            :listing_id, :url, :name, :listing_type, :room_type,
            :bedrooms, :max_guests, :amenities,
            :latitude, :longitude, :neighborhood, :city,
            :host_id,
            :rating_overall,
            :first_seen, :last_seen, 
            :description, :local_description
        )
        ON CONFLICT(listing_id) DO UPDATE SET
            name            = excluded.name,
            listing_type    = excluded.listing_type,
            bedrooms        = excluded.bedrooms,
            max_guests      = excluded.max_guests,
            amenities       = excluded.amenities,
            rating_overall  = excluded.rating_overall,
            last_seen       = excluded.last_seen,
            description     = excluded.description,
            local_description = excluded.local_description
    """, {**listing, "first_seen": first_seen, "last_seen": now})


def insert_snapshot(conn: sqlite3.Connection, snapshot: dict):
    """Save a price/availability snapshot for a listing."""
    conn.execute("""
        INSERT INTO snapshots (
            listing_id, scraped_at, price_per_night, currency,
            min_nights, available_dates, unavailable_dates
        ) VALUES (
            :listing_id, :scraped_at, :price_per_night, :currency,
            :min_nights, :available_dates, :unavailable_dates
        )
    """, snapshot)


def upsert_calendar_day(conn: sqlite3.Connection, listing_id: str,
                         date: str, status: str, price: float = None):
    """Update the status of a specific night for a listing."""
    conn.execute("""
        INSERT INTO calendar_days (listing_id, calendar_date, status, price, detected_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(listing_id, calendar_date) DO UPDATE SET
            status      = excluded.status,
            price       = excluded.price,
            detected_at = excluded.detected_at
    """, (listing_id, date, status, price, datetime.now(timezone.utc).replace(tzinfo=None).isoformat()))


def get_previous_available_dates(conn: sqlite3.Connection, listing_id: str) -> set:
    """Return the set of dates that were available in the last snapshot."""
    row = conn.execute("""
        SELECT available_dates FROM snapshots
        WHERE listing_id = ?
        ORDER BY scraped_at DESC
        LIMIT 1 OFFSET 1
    """, (listing_id,)).fetchone()

    if not row or not row["available_dates"]:
        return set()

    import json
    return set(json.loads(row["available_dates"]))
