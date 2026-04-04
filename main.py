import os
import sys
import json
import argparse
import logging
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
import db
import scraper
import occupancy
import math

# ── Logging setup ─────────────────────────────────────────────────────────────
# Logs go to both the console AND a file, so you can check what happened
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/tracker_{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y%m')}.log")
    ]
)
log = logging.getLogger(__name__)

def distance_kms(lat1, lon1, lat2, lon2): 
    R = 6371  # Earth radius km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat/2)**2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon/2)**2
    )
    return 2 * R * math.asin(math.sqrt(a))

def matches_filters(listing):
    lat = listing.get("latitude")
    lon = listing.get("longitude")

    if lat is None or lon is None:
        return False

    center_lat = 41.161597
    center_lon = -8.632763
    dist = distance_kms(lat, lon, center_lat, center_lon) #  Avenida de Bosvista comom referência

    # keywords = [
     #   "nature", "forest", "river", "cabin", "bungalow",
     #   "peaceful", "quiet", "rustic", "retreat",
     #   "natureza", "campo", "tranquilo", "nature", "unplug", "relax", "calmo"
    #]
    #text = (listing.get("name", "") + " " + listing.get("amenities", "")).lower()
    #has_keywords = any(word in text for word in keywords)

    return (
        listing.get("room_type") == "Entire home/apt"
        and (listing.get("bedrooms") in [0, 1, 2] or listing.get("bedrooms") is None)
        and dist <= 40 
     #   and has_keywords
    )


def run_pipeline(dry_run: bool = False):
    start_time = datetime.now(timezone.utc).replace(tzinfo=None)
    log.info("=" * 60)
    log.info(f"Pipeline started at {start_time.isoformat()}")

    # ── Load config
    load_dotenv()
    api_token = os.getenv("APIFY_API_TOKEN")

    if not api_token:
        raise ValueError("APIFY_API_TOKEN not set")
    
    location = json.loads(os.getenv("TARGET_LOCATION", "[]"))
    max_listings = int(os.getenv("MAX_LISTINGS", "200"))
    db_path      = os.getenv("DB_PATH", "data/airbnb.db")

    log.info(f"Target location : {location}")
    log.info(f"Max listings    : {max_listings}")
    log.info(f"Database        : {db_path}")
    log.info(f"Dry run         : {dry_run}")

    # Step 1: Initialize database 
    if not dry_run:
        db.init_db(db_path)

    # Step 2: Search
    try:
        listings = scraper.fetch_listings(
            location=location,
            max_listings=max_listings,
            api_token=api_token
        )
    except ValueError as e:
        log.error(f"Configuration error: {e}")
        log.error("→ Copy .env.example to .env and add your Apify token")
        log.error("→ Get a free token at: https://apify.com")
        sys.exit(1)
    except Exception as e:
        log.error(f"Scrape failed: {e}")
        raise

    if not listings:
        log.warning("No listings returned — check your location or Apify token")
        return

    #  Step 3: Store + detect occupancy
    scraped_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn = db.get_connection(db_path) if not dry_run else None
    stats = {"new": 0, "updated": 0, "bookings_detected": 0}

    for i, listing in enumerate(listings, 1):
        #print(listing.get("room_type"), "|", listing.get("bedrooms"), "|", listing.get("latitude"))
        if not matches_filters(listing):
            continue  # skip unwanted listings

        listing_id = listing["listing_id"]
        log.info(f"  [{i:03d}/{len(listings)}] {listing_id}  "
                 f"{listing.get('listing_type','?')}  "
                 f"{listing.get('bedrooms','?')}BR  "
                 f"€{listing.get('price_per_night','?')}/night")

        if dry_run:
            continue  # just print, don't write

        # 3a. Upsert listing metadata
        db.upsert_listing(conn, listing) # update previously seen listing inside conn, or add 

        # 3b. Store snapshot (price + availability at this point in time)
        snapshot = {
            "listing_id":       listing_id,
            "scraped_at":       scraped_at,
            "price_per_night":  listing.get("price_per_night"),
            "currency":         listing.get("currency", "EUR"),
            "min_nights":       listing.get("min_nights"),
            "available_dates":  listing.get("available_dates", "[]"),
            "unavailable_dates": listing.get("unavailable_dates", "[]"),
        }
        db.insert_snapshot(conn, snapshot)
        conn.commit() # put the values in the dataset

        # 3c. Detect occupancy changes vs previous snapshot
        avail = json.loads(listing.get("available_dates") or "[]")
        unavail = json.loads(listing.get("unavailable_dates") or "[]")
        cal_prices = listing.get("calendar_prices") or {}

        occupancy.detect_and_store_occupancy( # call occupancy.py
            conn = conn, 
            listing_id=listing_id,
            new_available=avail,
            new_unavailable=unavail,
            calendar_prices=cal_prices
        )

    if conn: # if it's not a dry run
        conn.close()

    # Step 4: Summary 
    elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - start_time).total_seconds()
    log.info("-" * 60)
    log.info(f"  Pipeline complete in {elapsed:.1f}s")
    log.info(f"  Listings processed : {len(listings)}")
    log.info(f"  Mode               : {'DRY RUN (nothing saved)' if dry_run else 'LIVE (saved to DB)'}")
    log.info("=" * 60)


# ── Scheduled mode ────────────────────────────────────────────────────────────

def run_scheduled(interval_hours: float = 96):
    """
    Run the pipeline repeatedly on a fixed interval.
    This is the simplest possible scheduler — just sleep between runs.

    For production, consider using:
      - cron (Linux/Mac): `0 8 * * * cd /path/to/project && python main.py`
      - Task Scheduler (Windows)
      - GitHub Actions (free, runs in the cloud)
    """
    log.info(f"Scheduler started — running every {interval_hours}h")
    while True:
        try:
            run_pipeline()
        except Exception as e:
            log.error(f"Pipeline run failed: {e}", exc_info=True)
            log.info("Will retry at next scheduled time")

        next_run = datetime.now(timezone.utc).replace(tzinfo=None).timestamp() + (interval_hours * 3600)
        next_run_str = datetime.fromtimestamp(next_run, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        log.info(f"Next run scheduled at: {next_run_str}")
        time.sleep(interval_hours * 3600)


# ── CLI 

def main():
    parser = argparse.ArgumentParser(
        description="Airbnb occupancy tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                  # run once, save to DB
  python main.py --dry-run        # run once, only print (no DB writes)
  python main.py --schedule       # run every 24h forever
  python main.py --report         # print market summary from existing data
        """
    )
    parser.add_argument("--dry-run",  action="store_true",
                        help="Scrape but don't write to database")
    parser.add_argument("--schedule", action="store_true",
                        help="Run on a repeating schedule")
    parser.add_argument("--report",   action="store_true",
                        help="Print a market summary from existing data")
    parser.add_argument("--hours",    type=float, default=96.0,
                        help="Hours between runs in schedule mode (default: 84)")
    args = parser.parse_args()

    load_dotenv()
    db_path = os.getenv("DB_PATH", "data/airbnb.db")

    if args.report:
        _print_report(db_path)
    elif args.schedule:
        run_scheduled(interval_hours=args.hours)
    else:
        run_pipeline(dry_run=args.dry_run)


def _print_report(db_path: str):
    """Quick CLI report — run after you have a few weeks of data."""
    from datetime import date, timedelta

    if not os.path.exists(db_path):
        print(f"No database found at {db_path}. Run a scrape first.")
        return

    conn = db.get_connection(db_path)

    # Count what we have
    n_listings = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    n_snapshots = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    n_days = conn.execute("SELECT COUNT(*) FROM calendar_days").fetchone()[0]

    print("\n" + "=" * 55)
    print("  AIRBNB TRACKER — DATABASE SUMMARY")
    print("=" * 55)
    print(f"  Listings tracked : {n_listings}")
    print(f"  Snapshots taken  : {n_snapshots}")
    print(f"  Calendar days    : {n_days}")
    print()

    # Occupancy by type
    start = str(date.today() - timedelta(days=30))
    end   = str(date.today())
    summary = occupancy.get_market_summary(db_path, start, end)

    if summary:
        print(f"  OCCUPANCY BY LISTING TYPE (last 30 days)")
        print(f"  {'Type':<25} {'BR':>3} {'Count':>5} {'Occ%':>6} {'Avg€/night':>10} {'Est Rev':>10}")
        print("  " + "-" * 65)
        for row in summary:
            occ = f"{row['avg_occupancy_rate']*100:.1f}%" if row['avg_occupancy_rate'] else "—"
            price = f"€{row['avg_price_per_night']:.0f}" if row['avg_price_per_night'] else "—"
            rev = f"€{row['avg_estimated_revenue']:.0f}" if row['avg_estimated_revenue'] else "—"
            print(f"  {str(row['listing_type']):<25} "
                  f"{str(row['bedrooms'] or '?'):>3} "
                  f"{row['listing_count']:>5} "
                  f"{occ:>6} "
                  f"{price:>10} "
                  f"{rev:>10}")
    else:
        print("  No occupancy data yet. Run for a few weeks first.")

    print("=" * 55 + "\n")
    conn.close()

if __name__ == "__main__":
    main()