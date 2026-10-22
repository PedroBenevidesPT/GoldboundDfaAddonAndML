"""Non-destructive database inspection for the FarmAnalysis review."""
import sqlite3
import sys
from pathlib import Path


def inspect(db_path: str, label: str):
    print(f"\n{'='*70}")
    print(f"  {label}: {db_path}")
    print(f"{'='*70}")
    p = Path(db_path)
    if not p.exists():
        print(f"  FILE DOES NOT EXIST")
        return
    print(f"  size: {p.stat().st_size:,} bytes")
    # WAL/SHM siblings
    for ext in ["-wal", "-shm"]:
        wal = Path(str(p) + ext)
        if wal.exists():
            print(f"  {ext[1:]}: {wal.stat().st_size:,} bytes  mtime={wal.stat().st_mtime}")
    
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        jm = con.execute("PRAGMA journal_mode").fetchone()[0]
        print(f"  journal_mode: {jm}")
        
        tables = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        idxs = con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        views = con.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
        ).fetchall()
        
        print(f"  tables ({len(tables)}): {[t[0] for t in tables]}")
        print(f"  indexes ({len(idxs)}): {[i[0] for i in idxs]}")
        if views:
            print(f"  views ({len(views)}): {[v[0] for v in views]}")
        
        for t in tables:
            name = t[0]
            try:
                cols = con.execute(f"PRAGMA table_info({name})").fetchall()
                col_str = ", ".join(f"{c[1]}({c[2]})" for c in cols)
                cnt = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                print(f"\n  --- {name} ---  ({cnt} rows)")
                print(f"    columns: {col_str}")
                if cnt > 0:
                    row = con.execute(f"SELECT * FROM {name} LIMIT 1").fetchone()
                    print(f"    sample: {dict(row)}")
            except Exception as e:
                print(f"\n  --- {name} ---  ERROR: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    inspect(r"C:\FarmAnalysis\tsm_markets.db", "tsm_markets.db")
    inspect(r"C:\FarmAnalysis\data\intel.sqlite", "data/intet.sqlite")
    inspect(r"C:\FarmAnalysis\dff.db", "dff.db")
    inspect(r"C:\FarmAnalysis\tsm_markets.sqlite", "tsm_markets.sqlite")
    inspect(r"C:\FarmAnalysis\woweconomy\tsm_markets.db", "woweconomy/tsm_markets.db")
