"""
scraper.py — Airbnb data collection via Apify
==============================================
Apify is a cloud platform that runs managed scrapers ("Actors").
We use the "tri_angle/airbnb-scraper" actor which handles:
  - JavaScript rendering (Airbnb is a React app)
  - CAPTCHA solving
  - IP rotation (so we don't get banned)
  - Calendar data extraction

HOW IT WORKS:
  1. We call Apify's REST API to start a scraper run
  2. We wait for it to finish (polling)
  3. We download the results as JSON
  4. We parse and clean the data
  5. The caller (main.py) stores it in SQLite

COST: Apify has a free tier (~$5/month of compute credits).
At 100 listings/day, you'll use roughly $1–2/month.
Sign up at https://apify.com and get your token from:
https://console.apify.com/account/integrations
"""

import os
import time
import json
import requests
import re
from datetime import datetime, timedelta, timezone
from typing import Generator

ACTOR_ID = "tri_angle~airbnb-scraper"

APIFY_BASE = "https://api.apify.com/v2"

MOCK_MODE = True

def fetch_listings(location: str, max_listings: int = 200, api_token: str = None) -> list[dict]:
    if MOCK_MODE: 
        with open("mock_data.json", "r", encoding="utf-8") as f: 
            raw_items = json.load(f)
        cleaned = []
        for item in raw_items: 
            try: 
                cleaned.append(_parse_listing(item))
            except Exception as e: 
                print(f"[scraper] Warning: {e}")
        print(f"[scraper] Successfully parsed {len(cleaned)} listings")
        return cleaned

    token = api_token or os.getenv("APIFY_API_TOKEN")
    if not token or token == "your_token_here":
        raise ValueError(
            "No Apify token found! Set APIFY_API_TOKEN in your .env file.\n"
            "Get one free at: https://apify.com"
        )

    print(f"[scraper] Starting Apify run for '{location}' (max {max_listings} listings)...")

    # Step 1: Start the Actor run
    run_id = _start_actor_run(token, location, max_listings)
    print(f"[scraper] Run started: {run_id}")

    # Step 2: Wait for it to finish
    _wait_for_run(token, run_id)

    # Step 3: Download raw results
    raw_items = _download_results(token, run_id)
    print(f"[scraper] Downloaded {len(raw_items)} raw items")

    # Step 4: Parse and clean each item
    cleaned = []
    for item in raw_items:
        try:
            cleaned.append(_parse_listing(item))
        except Exception as e:
            print(f"[scraper] Warning: failed to parse item {item.get('id', '?')}: {e}")

    print(f"[scraper] Successfully parsed {len(cleaned)} listings")
    return cleaned

# Apify API helpers 

def _start_actor_run(token: str, location: str, max_listings: int) -> str:

    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    end_date = today + timedelta(days=90)

    actor_input = {
        "locationQueries": location,      # where to search
        "maxListings": max_listings,        # result limit
        "currency": "EUR",                  # price currency
        "includeCalendar": True,            # ← crucial! gets availability data
        "calendarMonths": 3,                # fetch 3 months of calendar
        "startDate": str(today),
        "endDate": str(today + timedelta(days=90)),
        "addMoreHostInfo": False,           # saves API credits
    }

    resp = requests.post(
        f"{APIFY_BASE}/acts/{ACTOR_ID}/runs",
        params={"token": token},
        json=actor_input,
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()["data"]["id"]


def _wait_for_run(token: str, run_id: str, timeout_minutes: int = 20):

    deadline = time.time() + (timeout_minutes * 60)
    while time.time() < deadline:
        resp = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            params={"token": token},
            timeout=10
        )
        resp.raise_for_status()
        status = resp.json()["data"]["status"]

        if status == "SUCCEEDED":
            print(f"[scraper] Run finished successfully ✓")
            return
        elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {run_id} ended with status: {status}")
        else:
            print(f"[scraper] Run status: {status} — waiting...")
            time.sleep(15)

    raise TimeoutError(f"Apify run didn't finish within {timeout_minutes} minutes")


def _download_results(token: str, run_id: str) -> list[dict]:

    resp = requests.get(
        f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items",
        params={"token": token, "format": "json", "clean": "true"},
        timeout=60
    )
    resp.raise_for_status()
    items = resp.json()
    print(f"[debug] Raw items returned: {len(items)}")  
    if items:
        print(f"[debug] First item keys: {list(items[0].keys())}")  

    with open("mock_data.json", "w", encoding="utf-8") as f: # save for debugging
        json.dump(items, f)

    return items


# Data cleaning - transform raw output into clean schema

def _parse_listing(raw: dict) -> dict:
    # Basic listing info
    listing_id = str(raw.get("id") or raw.get("listingId", ""))
    if not listing_id:
        raise ValueError("No listing ID found")

    #  Price
    price = raw.get("pricePerNight")
    if price is None:
        base_desc = _deep_get(raw, "price", "breakDown", "basePrice", "description") or ""
        # "5 nights x €\xa085.40" → extract the number after €
        match = re.search(r"€[\xa0\s]*([\d,.]+)", base_desc)
        if match:
            price = float(match.group(1).replace(",", "."))
    if isinstance(price, str):
        # Remove currency symbols and commas
        price = float(price.replace("€", "").replace("$", "")
                          .replace(",", ".").strip() or 0) or None

    #  Location 
    lat = _deep_get(raw, "coordinates", "latitude")
    lng = _deep_get(raw, "coordinates", "longitude")
    neighborhood = (raw.get("neighborhoodOverview") or
                    _deep_get(raw, "location", "neighborhood", "name"))
    city = raw.get("city") or _deep_get(raw, "location", "city")

    # Host 
    host = raw.get("host") or raw.get("primaryHost") or {}
    host_id = str(host.get("id") or raw.get("hostId") or "")

    # Ratings
    rating_raw = raw.get("rating")
    rating = _deep_get(rating_raw, "guestSatisfaction") if isinstance(rating_raw, dict) else rating_raw

    calendar = raw.get("calendar") or []
    available_dates = []
    unavailable_dates = []
    calendar_prices = {}

    for day in calendar:
        date_str = day.get("date") or day.get("calendarDate")
        if not date_str:
            continue
        # Normalize to YYYY-MM-DD
        if "T" in str(date_str):
            date_str = str(date_str)[:10]

        if day.get("available", False):
            available_dates.append(date_str)
            if day.get("price"):
                calendar_prices[date_str] = day["price"]
        else:
            unavailable_dates.append(date_str)

    # ── Amenities
    amenities = raw.get("amenities") or []
    if isinstance(amenities, list) and amenities and isinstance(amenities[0], dict):
        amenities = [a.get("name", "") for a in amenities]

    # ___ Description
    description = raw.get("description")
    local_descrp = raw.get("locationDescription")

    # Extract bedrooms from subDescription items
    sub_items = _deep_get(raw, "subDescription", "items") or []
    bedrooms = None
    max_guests = raw.get("personCapacity")
    for item in sub_items:
        item_lower = str(item).lower()
        if "guest" in item_lower:
            match = re.search(r"(\d+)", item)
            if match:
                max_guests = int(match.group(1))
        elif "studio" in item_lower:
            bedrooms = 0
        elif "bedroom" in item_lower:
            match = re.search(r"(\d+)", item)
            if match:
                bedrooms = int(match.group(1))

    return {
        # Listing identity
        "listing_id": listing_id,
        "url": f"https://www.airbnb.com/rooms/{listing_id}",
        "name": raw.get("name") or raw.get("title") or "",

        # Type
        "listing_type": raw.get("propertyType") or "",
        "room_type": raw.get("roomType") or "",

        # Physical
        "bedrooms": bedrooms,
        "max_guests": _to_int(max_guests),
        "amenities": json.dumps(amenities),

        # Location
        "latitude": _to_float(lat),
        "longitude": _to_float(lng),
        "neighborhood": neighborhood or "",
        "city": city or "",

        # Host
        "host_id": host_id,

        # Ratings
        "rating_overall": _to_float(rating),

        # Snapshot-level data (also stored separately in snapshots table)
        "price_per_night": _to_float(price),
        "description": description,
        "local_description": local_descrp,
        "currency": raw.get("currency") or "EUR",
        "min_nights": _to_int(raw.get("minNights") or raw.get("minStay")),
        "available_dates": json.dumps(sorted(available_dates)),
        "unavailable_dates": json.dumps(sorted(unavailable_dates)),
        "calendar_prices": calendar_prices,  # used by occupancy detection, not stored directly
    }


# Utilities

def _deep_get(d: dict, *keys):
    """Safely navigate nested dicts: _deep_get(d, 'a', 'b') == d['a']['b']"""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _to_int(v) -> int | None:
    try:
        return int(float(str(v))) if v is not None else None
    except (ValueError, TypeError):
        return None


def _to_float(v) -> float | None:
    try:
        return float(str(v)) if v is not None else None
    except (ValueError, TypeError):
        return None
