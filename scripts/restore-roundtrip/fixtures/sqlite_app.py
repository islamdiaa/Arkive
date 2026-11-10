"""A stand-in application that keeps SQLite databases open while Arkive backs them up.

app.db is in WAL mode with automatic checkpoints disabled, so all rows written
after the first seconds live only in app.db-wal while the "app" runs: a copy of
the main file alone would silently lose them. other.sqlite3 uses a rollback
journal. Both keep receiving writes until the container stops.
"""

import os
import sqlite3
import sys
import time

DATA = sys.argv[1] if len(sys.argv) > 1 else "/data"

wal = sqlite3.connect(os.path.join(DATA, "app.db"), isolation_level=None)
wal.execute("PRAGMA journal_mode=WAL")
wal.execute("PRAGMA wal_autocheckpoint=0")
wal.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, body TEXT NOT NULL, payload BLOB)")
wal.execute("CREATE TABLE IF NOT EXISTS ticks (id INTEGER PRIMARY KEY, at REAL)")
wal.execute("BEGIN")
for i in range(2000):
    wal.execute(
        "INSERT INTO items (body, payload) VALUES (?, ?)",
        (f"item {i} 🎉 漢字", bytes(range(256)) * 4),
    )
wal.execute("COMMIT")

rollback = sqlite3.connect(os.path.join(DATA, "other.sqlite3"), isolation_level=None)
rollback.execute("PRAGMA journal_mode=DELETE")
rollback.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")
rollback.execute("INSERT OR REPLACE INTO settings VALUES ('theme', 'dark'), ('lang', 'ar')")

with open(os.path.join(DATA, "ready"), "w") as marker:
    marker.write("ok\n")

while True:
    wal.execute("INSERT INTO ticks (at) VALUES (?)", (time.time(),))
    rollback.execute("INSERT OR REPLACE INTO settings VALUES ('last_tick', ?)", (str(time.time()),))
    time.sleep(0.5)
