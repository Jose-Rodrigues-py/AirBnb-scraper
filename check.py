
import sqlite3

conn = sqlite3.connect("data/airbnb.db")

print("Listings:", conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0])
print("Snapshots:", conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])

conn.close()