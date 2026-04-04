import streamlit as st
import sqlite3
import pandas as pd
import json
from datetime import date, timedelta

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Airbnb Tracker",
    page_icon="🏠",
    layout="wide",
)

# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    [data-testid="stMetricValue"] { font-size: 2rem; font-weight: 700; }
    [data-testid="stMetricLabel"] { font-size: 0.85rem; opacity: 0.7; }
    .stDataFrame { border-radius: 8px; }
    div[data-testid="column"] { padding: 0 8px; }
</style>
""", unsafe_allow_html=True)

DB_PATH = "data/airbnb.db"

# ── Load data ─────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)  # refresh every 5 minutes
def load_data():
    conn = sqlite3.connect(DB_PATH)

    df = pd.read_sql_query("""
        SELECT
            l.listing_id,
            l.name,
            l.listing_type,
            l.room_type,
            l.bedrooms,
            l.max_guests,
            l.latitude,
            l.longitude,
            l.neighborhood,
            l.city,
            l.rating_overall,
            l.first_seen,
            l.description,
            l.url,
            s.price_per_night,
            s.min_nights,
            s.scraped_at
        FROM listings l
        JOIN (
            SELECT listing_id, price_per_night, min_nights, scraped_at,
                   ROW_NUMBER() OVER (PARTITION BY listing_id ORDER BY scraped_at DESC) as rn
            FROM snapshots
        ) s ON l.listing_id = s.listing_id AND s.rn = 1
    """, conn)

    # Calendar stats
    calendar_df = pd.read_sql_query("""
        SELECT
            listing_id,
            SUM(CASE WHEN status = 'booked' THEN 1 ELSE 0 END) as booked_days,
            SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) as available_days,
            COUNT(*) as total_days
        FROM calendar_days
        GROUP BY listing_id
    """, conn)

    conn.close()

    if not calendar_df.empty:
        calendar_df["occupancy_rate"] = (
            calendar_df["booked_days"] / calendar_df["total_days"].replace(0, None)
        ).round(3)
        df = df.merge(calendar_df[["listing_id", "booked_days", "occupancy_rate"]], 
                      on="listing_id", how="left")
    else:
        df["booked_days"] = 0
        df["occupancy_rate"] = None

    return df

# ── Header ────────────────────────────────────────────────────────────────────
st.title("Airbnb Market Tracker")
st.caption("Porto & área")

# ── Load ──────────────────────────────────────────────────────────────────────
try:
    df = load_data()
except Exception as e:
    st.error(f"Could not load database: {e}")
    st.stop()

if df.empty:
    st.warning("No listings in the database yet. Run `python main.py` first.")
    st.stop()

# ── Sidebar filters ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filtros")

    bedrooms = st.multiselect(
        "Nº quartos",
        options=sorted(df["bedrooms"].dropna().unique().astype(int).tolist()),
        default=[],
        format_func=lambda x: "Studio" if x == 0 else f"{x} BR"
    )

    price_min, price_max = st.slider(
        "Preço por noite (€)",
        min_value=0,
        max_value=int(df["price_per_night"].dropna().max()) + 1 if not df["price_per_night"].dropna().empty else 500,
        value=(0, int(df["price_per_night"].dropna().max()) + 1 if not df["price_per_night"].dropna().empty else 500),
    )

    min_rating = st.slider("Rating", 0.0, 5.0, 0.0, step=0.1)

    room_types = st.multiselect(
        "Tipo",
        options=df["room_type"].dropna().unique().tolist(),
        default=[]
    )

    st.divider()
    st.caption(f"Last scraped: {df['scraped_at'].max()[:10] if not df.empty else '—'}")
    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()

# ── Apply filters ─────────────────────────────────────────────────────────────
filtered = df.copy()

if bedrooms:
    filtered = filtered[filtered["bedrooms"].isin(bedrooms)]

filtered = filtered[
    (filtered["price_per_night"].fillna(0) >= price_min) &
    (filtered["price_per_night"].fillna(0) <= price_max)
]

if min_rating > 0:
    filtered = filtered[filtered["rating_overall"].fillna(0) >= min_rating]

if room_types:
    filtered = filtered[filtered["room_type"].isin(room_types)]

# ── KPI metrics ───────────────────────────────────────────────────────────────
col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    st.metric("Anúncios", f"{len(filtered):,}")
with col2:
    avg_price = filtered["price_per_night"].mean()
    st.metric("Preço médio pr noite", f"€{avg_price:.0f}" if not pd.isna(avg_price) else "—")
with col3:
    avg_rating = filtered["rating_overall"].mean()
    st.metric("Avaliação média", f"{avg_rating:.2f} ⭐" if not pd.isna(avg_rating) else "—")
with col4:
    avg_occ = filtered["occupancy_rate"].mean()
    st.metric("Ocupação média", f"{avg_occ*100:.1f}%" if not pd.isna(avg_occ) else "Not enough data yet")
with col5:
    studios = (filtered["bedrooms"] == 0).sum()
    st.metric("Estudios", f"{studios:,}")

st.divider()

# ── Charts + Map ──────────────────────────────────────────────────────────────
col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("Price distribution")
    price_data = filtered["price_per_night"].dropna()
    if not price_data.empty:
        import numpy as np
        counts, bin_edges = np.histogram(price_data, bins=20)
        labels = [f"€{int(bin_edges[i])}" for i in range(len(counts))]
        hist = pd.DataFrame({"listings": counts}, index=labels)
        st.bar_chart(hist, y="listings")
    else:
        st.info("No price data available")

with col_right:
    st.subheader("Informação dos quartos")
    if "bedrooms" in filtered.columns:
        bed_counts = (
            filtered["bedrooms"]
            .fillna(-1)
            .astype(int)
            .map(lambda x: "Studio" if x == 0 else ("Unknown" if x == -1 else f"{x} BR"))
            .value_counts()
        )
        st.bar_chart(bed_counts)

st.divider()

# ── Map ───────────────────────────────────────────────────────────────────────
st.subheader("📍 Localizações")
map_df = filtered[["latitude", "longitude"]].dropna().rename(
    columns={"latitude": "lat", "longitude": "lon"}
)
if not map_df.empty:
    st.map(map_df, size=20)
else:
    st.info("No location data to display")

st.divider()

# ── Table ─────────────────────────────────────────────────────────────────────
st.subheader(f"Todos os anúncios ({len(filtered):,})")

display_cols = {
    "name": "Name",
    "room_type": "Type",
    "bedrooms": "BR",
    "price_per_night": "€/night",
    "rating_overall": "Rating",
    "occupancy_rate": "Occupancy",
    "max_guests": "Guests",
    "neighborhood": "Area",
    "url": "Link",
}

table = filtered[[c for c in display_cols if c in filtered.columns]].copy()
table = table.rename(columns=display_cols)

if "Occupancy" in table.columns:
    table["Occupancy"] = table["Occupancy"].apply(
        lambda x: f"{x*100:.1f}%" if pd.notna(x) else "—"
    )
if "€/night" in table.columns:
    table["€/night"] = table["€/night"].apply(
        lambda x: f"€{x:.0f}" if pd.notna(x) else "—"
    )
if "Rating" in table.columns:
    table["Rating"] = table["Rating"].apply(
        lambda x: f"{x:.2f}" if pd.notna(x) else "—"
    )
if "BR" in table.columns:
    table["BR"] = table["BR"].apply(
        lambda x: "Studio" if x == 0 else (str(int(x)) if pd.notna(x) else "—")
    )

st.dataframe(
    table,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Link": st.column_config.LinkColumn("Link", display_text="Open ↗"),
        "Name": st.column_config.TextColumn("Name", width="large"),
    }
)

# ── Price by bedrooms table ───────────────────────────────────────────────────
st.divider()
st.subheader("📊 Preço médio por quarto")

summary = (
    filtered.groupby("bedrooms")
    .agg(
        count=("listing_id", "count"),
        avg_price=("price_per_night", "mean"),
        avg_rating=("rating_overall", "mean"),
        avg_occupancy=("occupancy_rate", "mean"),
    )
    .reset_index()
)
summary["bedrooms"] = summary["bedrooms"].apply(
    lambda x: "Studio" if x == 0 else f"{int(x)} BR" if pd.notna(x) else "Unknown"
)
summary = summary.rename(columns={
    "bedrooms": "Type",
    "count": "Listings",
    "avg_price": "Avg €/night",
    "avg_rating": "Avg Rating",
    "avg_occupancy": "Avg Occupancy",
})
summary["Avg €/night"] = summary["Avg €/night"].apply(lambda x: f"€{x:.0f}" if pd.notna(x) else "—")
summary["Avg Rating"] = summary["Avg Rating"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "—")
summary["Avg Occupancy"] = summary["Avg Occupancy"].apply(
    lambda x: f"{x*100:.1f}%" if pd.notna(x) else "Sem informação sufeciente ainda"
)

st.dataframe(summary, use_container_width=True, hide_index=True)