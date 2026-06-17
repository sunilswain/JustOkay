#!/usr/bin/env python3
import sqlite3, sys
db = sys.argv[1]
c = sqlite3.connect(db)
tables = [t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("tables:", tables)
count = c.execute("SELECT COUNT(*) FROM khatiyans").fetchone()[0]
print("count:", count)
row = c.execute("SELECT data_json FROM khatiyans LIMIT 1").fetchone()
print("sample_len:", len(row[0]) if row and row[0] else 0)
c.close()
print("OK - DB is valid")
