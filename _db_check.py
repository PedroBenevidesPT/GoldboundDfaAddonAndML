"""Inspect the DB schema and sample data."""
import sqlite3

conn = sqlite3.connect(r"C:\FarmAnalysis\tsm_markets.db")
cur = conn.cursor()

# Tables
tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("Tables:", tables)

for t in tables:
    name = t[0]
    cols = cur.execute(f"PRAGMA table_info({name})").fetchall()
    print(f"\n--- {name} ---")
    for c in cols:
        print(f"  {c[1]:20s} {c[2]:10s} nullable={c[3]} default={c[4]} pk={c[5]}")
    # sample 3 rows
    sample = cur.execute(f"SELECT * FROM {name} LIMIT 3").fetchall()
    print(f"  Sample rows:")
    for s in sample:
        print(f"    {s}")

conn.close()
